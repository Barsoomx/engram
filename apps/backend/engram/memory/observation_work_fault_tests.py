from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django_celery_outbox.models import CeleryOutbox
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks

from engram.access.services import AccessDeniedError
from engram.context import services as context_services
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    ObservationSource,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    Team,
    WorkflowWork,
)
from engram.hooks import services as hook_services
from engram.hooks.hook_ingest_tests import (
    create_project_scope,
    enable_realtime_candidates,
    hook_event_input,
)
from engram.imports import services as import_services

_WORK_IMMUTABLE_FIELDS = tuple(field_name for field_name, _field in WorkflowWork._IMMUTABLE_FIELDS)
_OUTBOX_TASK_FIELDS = (
    'task_id',
    'task_name',
    'args',
    'kwargs',
    'redacted_args',
    'redacted_kwargs',
    'options',
    'sentry_trace_id',
    'sentry_baggage',
    'structlog_context',
    'schema_version',
)
_OUTBOX_CONTENT_FIELDS = (
    'args',
    'kwargs',
    'options',
    'redacted_args',
    'redacted_kwargs',
    'sentry_trace_id',
    'sentry_baggage',
    'structlog_context',
)


def _counts() -> tuple[int, int, int, int, int, int, int]:
    return (
        AgentSession.objects.count(),
        Agent.objects.count(),
        RawEventEnvelope.objects.count(),
        Observation.objects.count(),
        ObservationSource.objects.count(),
        WorkflowWork.objects.count(),
        CeleryOutbox.objects.count(),
    )


def _assert_only_outbox_callbacks(callbacks: list[object]) -> None:
    assert callbacks
    for callback in callbacks:
        module = inspect.getmodule(callback)
        module_name = getattr(module, '__name__', '')
        code = getattr(callback, '__code__', None)
        code_filename = str(getattr(code, 'co_filename', ''))
        assert module_name == 'django_celery_outbox.app'
        assert Path(code_filename).name == 'app.py'
        assert 'django_celery_outbox' in Path(code_filename).parts
        closure = inspect.getclosurevars(callback)
        closure_symbols = set(closure.nonlocals) | set(closure.globals)
        closure_symbols.update(
            name
            for value in (*closure.nonlocals.values(), *closure.globals.values())
            if (name := getattr(value, '__name__', None)) is not None
        )
        assert '_emit_enqueued_metric_safe' in set(code.co_names) | closure_symbols


def _call_path(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _is_legacy_observation_call(node: ast.Call) -> bool:
    return 'process_observation_recorded' in _call_path(node.func)


def _production_trees() -> tuple[Path, list[tuple[Path, ast.Module]]]:
    package_root = Path(inspect.getfile(hook_services)).parents[1]
    trees = [
        (path, ast.parse(path.read_text(encoding='utf-8')))
        for path in sorted(package_root.rglob('*.py'))
        if not path.name.endswith('_tests.py')
    ]
    return package_root, trees


@pytest.mark.django_db
def test_fault_after_outbox_insert_rolls_back_all_evidence_and_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    enable_realtime_candidates(_organization)
    data = hook_event_input(project, team)

    class AfterOutboxError(RuntimeError):
        pass

    original_dispatch = hook_services.dispatch_work_task
    reached_after_outbox_insert = False

    def dispatch_then_fail(*args: object, **kwargs: object) -> object:
        nonlocal reached_after_outbox_insert
        original_dispatch(*args, **kwargs)
        work_id = args[1]
        queued = CeleryOutbox.objects.get(
            task_id=f'workflow-work:{work_id}',
            task_name='engram.memory.process_observation_work_v1',
        )
        assert queued.args == [str(work_id)]
        assert queued.kwargs == {}
        reached_after_outbox_insert = True
        raise AfterOutboxError('fault after outbox insertion')

    monkeypatch.setattr(hook_services, 'dispatch_work_task', dispatch_then_fail)
    with pytest.raises(AfterOutboxError, match='after outbox'):
        with transaction.atomic():
            hook_services.IngestHookEvent().execute(data)

    assert reached_after_outbox_insert is True
    assert _counts() == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.django_db
def test_required_work_and_id_only_outbox_are_durable_before_and_after_commit(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    enable_realtime_candidates(_organization)
    data = hook_event_input(project, team)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        with transaction.atomic():
            result = hook_services.IngestHookEvent().execute(data)
            assert _counts() == (1, 1, 1, 1, 1, 1, 1)
            work = WorkflowWork.objects.get()
            queued = CeleryOutbox.objects.get()
            assert queued.task_name == 'engram.memory.process_observation_work_v1'
            assert queued.args == [str(work.id)]
            assert queued.kwargs == {}
            work_id = work.id
            queued_id = queued.id
            work_snapshot = {field_name: getattr(work, field_name) for field_name in _WORK_IMMUTABLE_FIELDS}
            outbox_snapshot = {field_name: getattr(queued, field_name) for field_name in _OUTBOX_TASK_FIELDS}

    _assert_only_outbox_callbacks(callbacks)
    assert _counts() == (1, 1, 1, 1, 1, 1, 1)
    committed_work = WorkflowWork.objects.get(id=work_id)
    committed_outbox = CeleryOutbox.objects.get(id=queued_id)
    assert {field_name: getattr(committed_work, field_name) for field_name in _WORK_IMMUTABLE_FIELDS} == work_snapshot
    assert {field_name: getattr(committed_outbox, field_name) for field_name in _OUTBOX_TASK_FIELDS} == outbox_snapshot
    assert committed_outbox.task_id == f'workflow-work:{committed_work.id}'
    assert committed_outbox.args == [str(committed_work.id)]
    assert committed_outbox.kwargs == {}
    assert result.observation.id == committed_work.subject_id


@pytest.mark.django_db
def test_outbox_intent_survives_broker_outage_without_broker_call(
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    enable_realtime_candidates(_organization)
    broker_calls: list[tuple[object, ...]] = []

    def broker_down(*args: object, **kwargs: object) -> object:
        broker_calls.append(args)
        raise AssertionError('broker access is forbidden during outbox ingest and callbacks')

    monkeypatch.setattr('django_celery_outbox.relay._publisher.Celery.send_task', broker_down)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        hook_services.IngestHookEvent().execute(hook_event_input(project, team))

    _assert_only_outbox_callbacks(callbacks)
    for callback in callbacks:
        callback()
    assert broker_calls == []
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_observation_task_payload_contains_only_work_id_and_no_secret() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    enable_realtime_candidates(_organization)
    secret = 'provider-secret-for-payload-test'
    hook_services.IngestHookEvent().execute(
        hook_event_input(
            project,
            team,
            payload={'tool_input': {'api_key': secret}, 'tool_response': {'stdout': secret}},
            observation={
                'type': 'tool_use',
                'title': 'secret output',
                'body': secret,
                'files_read': [],
                'files_modified': [],
            },
        ),
    )

    work = WorkflowWork.objects.get()
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_work_v1'
    assert queued.args == [str(work.id)]
    assert queued.kwargs == {}
    for field_name in _OUTBOX_CONTENT_FIELDS:
        assert secret not in repr(getattr(queued, field_name)), field_name


@pytest.mark.django_db
@pytest.mark.parametrize('foreign_selector', ('project', 'team'))
def test_foreign_project_or_team_is_denied_before_any_evidence_mutation(foreign_selector: str) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    if foreign_selector == 'project':
        foreign_project = Project.objects.create(
            organization=_organization,
            name='Foreign project',
            slug='foreign-project',
        )
        data = hook_event_input(project, team, project_id=foreign_project.id)
    else:
        foreign_team = Team.objects.create(organization=_organization, name='Foreign team', slug='foreign-team')
        ProjectTeam.objects.create(organization=_organization, project=project, team=foreign_team)
        data = hook_event_input(project, foreign_team)

    before = _counts()
    projects_before = list(Project.objects.order_by('id').values())
    teams_before = list(Team.objects.order_by('id').values())
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(AccessDeniedError):
            hook_services.IngestHookEvent().execute(data)

    mutations = [
        query['sql'] for query in queries if query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert mutations == []
    assert _counts() == before
    assert list(Project.objects.order_by('id').values()) == projects_before
    assert list(Team.objects.order_by('id').values()) == teams_before


@pytest.mark.parametrize(
    'source',
    (
        'process_observation_recorded()',
        'process_observation_recorded.delay()',
        'process_observation_recorded.apply_async()',
        'tasks.process_observation_recorded.delay()',
    ),
)
def test_legacy_observation_call_census_recognizes_all_call_forms(source: str) -> None:
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    assert [node for node in calls if _is_legacy_observation_call(node)]


def test_c1_2_producer_census_has_no_legacy_observation_task_or_required_on_commit() -> None:
    package_root, production_trees = _production_trees()
    legacy_calls = [
        (path.relative_to(package_root), node.lineno)
        for path, tree in production_trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_legacy_observation_call(node)
    ]
    assert legacy_calls == []

    hook_tree = ast.parse(Path(inspect.getfile(hook_services)).read_text(encoding='utf-8'))
    hook_on_commit_calls = [
        node
        for node in ast.walk(hook_tree)
        if isinstance(node, ast.Call) and _call_path(node.func)[-1:] == ('on_commit',)
    ]
    assert len(hook_on_commit_calls) == 1
    callback = hook_on_commit_calls[0].args[0]
    assert isinstance(callback, ast.Lambda)
    assert isinstance(callback.body, ast.Call)
    assert _call_path(callback.body.func) == ('distill_session', 'delay')

    for module in (import_services, context_services):
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding='utf-8'))
        assert not [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_path(node.func)[-1:] == ('on_commit',)
        ]
