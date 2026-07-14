from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from django.db import transaction
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.workflow_work_tests import (
    create_empty_session_work,
    create_required_work,
    create_scope,
)

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
OBSERVATION_TASK = 'engram.memory.process_observation_work_v1'
SESSION_TASK = 'engram.memory.distill_session_work_v1'
CANDIDATE_TASK = 'engram.memory.process_candidate_decision_work_v1'


def _wd() -> ModuleType:
    from engram.memory import work_dispatch

    return work_dispatch


def get_run(run_id: uuid.UUID) -> WorkflowRun:
    return WorkflowRun.objects.get(id=run_id)


def make_reconciliation_run(work: WorkflowWork, *, dispatched_at: datetime) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.RECONCILIATION,
        dispatched_at=dispatched_at,
        input_snapshot=work.input_snapshot,
    )


@pytest.mark.django_db
def test_queue_work_attempt_creates_queued_run_and_package() -> None:
    module = _wd()
    scope = create_scope('dispatch-create')
    work = create_required_work(scope, suffix='dispatch-create')

    run = module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)

    assert run.status == WorkflowRunStatus.QUEUED
    assert run.execution_contract_version == 1
    assert run.origin == WorkflowRunOrigin.RECONCILIATION
    assert run.work_id == work.id
    assert run.run_type == work.work_type
    assert run.organization_id == work.organization_id
    assert run.project_id == work.project_id
    assert run.team_id == work.team_id
    assert run.dispatched_at == NOW
    assert run.fencing_token is None
    assert run.lease_owner == ''

    outbox = CeleryOutbox.objects.get(task_name=OBSERVATION_TASK)
    assert outbox.args == [str(work.id), str(run.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{run.id}'
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_queue_work_attempt_uses_matching_versioned_task_for_session_work() -> None:
    module = _wd()
    scope = create_scope('dispatch-session')
    work = create_empty_session_work(scope)

    run = module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)

    assert run.run_type == WorkflowWorkType.SESSION_DISTILLATION
    outbox = CeleryOutbox.objects.get(task_name=SESSION_TASK)
    assert outbox.args == [str(work.id), str(run.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{run.id}'


@pytest.mark.django_db
def test_queue_work_attempt_uses_candidate_decision_task() -> None:
    module = _wd()
    organization, team, project, _agent, _session = create_scope('dispatch-candidate')
    work = WorkflowWork.objects.create(
        organization=organization,
        project=project,
        team=team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=uuid.uuid4(),
        contract_version=1,
        input_fingerprint='f' * 64,
        input_snapshot={'schema': 'candidate_decision_input/v1'},
    )

    run = module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.AUTOMATIC)

    assert run.run_type == WorkflowWorkType.CANDIDATE_DECISION
    outbox = CeleryOutbox.objects.get(task_name=CANDIDATE_TASK)
    assert outbox.args == [str(work.id), str(run.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{run.id}'


@pytest.mark.django_db
def test_queue_work_attempt_returns_recent_queued_run_without_new_package() -> None:
    module = _wd()
    scope = create_scope('dispatch-recent')
    work = create_required_work(scope, suffix='dispatch-recent')
    existing = make_reconciliation_run(work, dispatched_at=NOW - timedelta(minutes=4))

    run = module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)

    assert run.id == existing.id
    assert WorkflowRun.objects.filter(work=work).count() == 1
    assert get_run(existing.id).dispatched_at == NOW - timedelta(minutes=4)
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_queue_work_attempt_resignals_stale_run_advancing_dispatched_at() -> None:
    module = _wd()
    scope = create_scope('dispatch-stale')
    work = create_required_work(scope, suffix='dispatch-stale')
    existing = make_reconciliation_run(work, dispatched_at=NOW - timedelta(minutes=6))

    run = module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)

    assert run.id == existing.id
    assert WorkflowRun.objects.filter(work=work).count() == 1
    assert get_run(existing.id).dispatched_at == NOW

    outbox = CeleryOutbox.objects.get(task_name=OBSERVATION_TASK)
    assert outbox.args == [str(work.id), str(existing.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{existing.id}'
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_queue_work_attempt_rolls_back_when_package_dispatch_fails() -> None:
    module = _wd()
    scope = create_scope('dispatch-rollback')
    work = create_required_work(scope, suffix='dispatch-rollback')

    with mock.patch.object(module.app, 'send_task', side_effect=RuntimeError('broker down')):
        with pytest.raises(RuntimeError, match='broker down'):
            with transaction.atomic():
                module.queue_work_attempt(work_id=work.id, now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)

    assert WorkflowRun.objects.filter(work=work).count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_queue_work_attempt_rejects_foreign_scope_work() -> None:
    module = _wd()
    create_required_work(create_scope('dispatch-scope'), suffix='dispatch-scope')

    with pytest.raises(ValueError):
        module.queue_work_attempt(work_id=uuid.uuid4(), now=NOW, origin=WorkflowRunOrigin.RECONCILIATION)


def test_memory_embedding_work_routes_to_batch_embedding_projection_task() -> None:
    module = _wd()
    embedding_type = getattr(WorkflowWorkType, 'MEMORY_EMBEDDING', 'memory_embedding')

    task_name = module._TASK_NAME_BY_WORK[embedding_type]

    assert task_name == 'engram.memory.embed_memory_projection_work_v1'
    assert task_name in module.ALLOWED_TASK_NAMES


def test_work_dispatch_never_reads_package_rows() -> None:
    tree = ast.parse(Path(__file__).with_name('work_dispatch.py').read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(module.startswith('django_celery_outbox.models') for module in imported_modules)


def test_work_task_signature_round_trips_through_parse_work_task_id() -> None:
    from engram.memory.work_dispatch import parse_work_task_id, work_task_signature

    work_id = uuid.uuid4()
    run_id = uuid.uuid4()

    _args, task_id_work_only = work_task_signature(work_id)
    _args, task_id_with_run = work_task_signature(work_id, run_id)

    assert parse_work_task_id(task_id_work_only) == (work_id, None)
    assert parse_work_task_id(task_id_with_run) == (work_id, run_id)
    assert parse_work_task_id('not-a-workflow-task') == (None, None)
