from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from django.db import close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
from structlog.testing import capture_logs

from engram.core.models import (
    Agent,
    AgentSession,
    Organization,
    Project,
    Runtime,
    SessionStatus,
    Team,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.distillation_reconciler import RetryFailedDistillations
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    WorkflowSubjectType,
    create_work,
    resolve_work_no_input,
    resolve_work_succeeded,
)

_DISTILL_WORK_TASK_NAME = 'engram.memory.distill_session_work_v1'
_LEGACY_DISTILL_TASK_NAME = 'engram.memory.distill_session'
_POISONED_RUN_FAILURE_REASON = 'legacy_run_contract_mismatch'


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Reconciler Org', slug='reconciler-org')


@pytest.fixture
def f_team(f_org: Organization) -> Team:
    return Team.objects.create(organization=f_org, name='Platform', slug='platform')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return Agent.objects.create(organization=f_org, runtime=Runtime.CODEX, external_id='codex-reconciler')


def create_session(
    organization: Organization,
    team: Team,
    project: Project,
    agent: Agent,
    *,
    status: str = SessionStatus.ENDED,
    suffix: str = '1',
    end_work_contract_version: int = 0,
) -> AgentSession:
    return AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        status=status,
        observation_sequence_cursor=0,
        end_work_contract_version=end_work_contract_version,
    )


def create_required_session_work(session: AgentSession, *, upper: int = 5) -> WorkflowWork:
    with transaction.atomic():
        work, created = create_work(
            CreateWorkflowWorkInput(
                organization_id=session.organization_id,
                project_id=session.project_id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=session.id,
                input_snapshot={
                    'schema': 'session_distillation_input/v1',
                    'session_id': str(session.id),
                    'lower_sequence_exclusive': 0,
                    'upper_sequence_inclusive': upper,
                },
            ),
        )

    assert created is True

    return work


def create_linked_run(
    work: WorkflowWork,
    *,
    status: str = WorkflowRunStatus.FAILED,
    created_at: object = None,
    finished_at: object = None,
    failure_reason: str = '',
) -> WorkflowRun:
    run = WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=status,
        input_snapshot=work.input_snapshot,
        failure_reason=failure_reason,
    )

    update_fields: dict[str, object] = {}
    if created_at is not None:
        update_fields['created_at'] = created_at
    if finished_at is not None:
        update_fields['finished_at'] = finished_at
    if update_fields:
        WorkflowRun.objects.filter(id=run.id).update(**update_fields)
        run.refresh_from_db()

    return run


def create_poisoned_legacy_run(work: WorkflowWork, *, created_at: object = None) -> WorkflowRun:
    run = WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=0,
        origin=WorkflowRunOrigin.LEGACY,
        input_snapshot=work.input_snapshot,
    )
    if created_at is not None:
        WorkflowRun.objects.filter(id=run.id).update(created_at=created_at)
        run.refresh_from_db()

    return run


def create_v1_queued_run(
    work: WorkflowWork,
    *,
    created_at: object,
    dispatched_at: object,
) -> WorkflowRun:
    run = create_linked_run(work, status=WorkflowRunStatus.QUEUED, created_at=created_at)
    WorkflowRun.objects.filter(id=run.id).update(
        execution_contract_version=1,
        origin=WorkflowRunOrigin.RECONCILIATION,
        dispatched_at=dispatched_at,
    )
    run.refresh_from_db()

    return run


def fresh_v1_runs(work: WorkflowWork) -> list[WorkflowRun]:
    return list(
        WorkflowRun.objects.filter(
            work=work,
            execution_contract_version=1,
            status=WorkflowRunStatus.QUEUED,
        ).order_by('created_at', 'id'),
    )


@pytest.mark.django_db
def test_required_work_with_stale_failed_run_creates_one_linked_queued_run_and_versioned_signal(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    queued = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED)
    assert queued.count() == 1
    new_run = queued.get()
    assert new_run.run_type == WorkflowRunType.SESSION_DISTILLATION
    assert new_run.organization_id == work.organization_id
    assert new_run.project_id == work.project_id
    assert new_run.team_id == work.team_id
    assert new_run.input_snapshot == work.input_snapshot

    outbox = CeleryOutbox.objects.get()
    assert outbox.task_name == _DISTILL_WORK_TASK_NAME
    assert outbox.args == [str(work.id), str(new_run.id)]
    assert outbox.kwargs == {}
    assert outbox.task_id == f'workflow-work:{work.id}:run:{new_run.id}'
    assert CeleryOutbox.objects.filter(task_name=_LEGACY_DISTILL_TASK_NAME).count() == 0

    assert len(result.retried) == 1
    retried = result.retried[0]
    assert retried.work_id == work.id
    assert retried.run_id == new_run.id
    assert result.unlinked_run_ids == ()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'scenario',
    (
        'latest_succeeded_truncated',
        'latest_running',
        'work_complete',
        'work_no_op',
    ),
)
def test_execute_suppresses_ineligible_required_or_terminal_work(
    scenario: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    if scenario == 'latest_succeeded_truncated':
        create_linked_run(
            work,
            status=WorkflowRunStatus.FAILED,
            created_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=2),
            failure_reason='provider returned 400',
        )
        succeeded = create_linked_run(
            work,
            status=WorkflowRunStatus.SUCCEEDED,
            created_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=40),
        )
        WorkflowRun.objects.filter(id=succeeded.id).update(escalation=True)
    elif scenario == 'latest_running':
        create_linked_run(
            work,
            status=WorkflowRunStatus.FAILED,
            created_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=2),
        )
        create_linked_run(work, status=WorkflowRunStatus.RUNNING, created_at=now - timedelta(hours=1))
    elif scenario == 'work_complete':
        create_linked_run(work, finished_at=now - timedelta(minutes=40))
        resolve_work_succeeded(work.id, organization_id=work.organization_id, project_id=work.project_id)
    else:
        create_linked_run(work, finished_at=now - timedelta(minutes=40))
        resolve_work_no_input(work.id, organization_id=work.organization_id, project_id=work.project_id)

    run_ids_before = set(WorkflowRun.objects.values_list('id', flat=True))

    result = RetryFailedDistillations().execute()

    assert set(WorkflowRun.objects.values_list('id', flat=True)) == run_ids_before
    assert CeleryOutbox.objects.count() == 0
    assert result.retried == ()


@pytest.mark.django_db
def test_two_non_transient_failures_reach_default_cap_and_are_not_retried(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 400',
    )
    create_linked_run(
        work,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=40),
        failure_reason='provider returned 400',
    )

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0
    assert result.retried == ()


@pytest.mark.django_db
def test_failed_run_inside_cooldown_is_not_retried(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=5))

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0
    assert result.retried == ()


@pytest.mark.django_db
def test_cooldown_env_override_makes_recent_failed_run_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_COOLDOWN_MINUTES', '2')
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=5))

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 1
    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id


@pytest.mark.django_db
def test_max_attempts_env_override_suppresses_single_non_transient_failure(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_MAX_ATTEMPTS', '1')
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(
        work,
        finished_at=timezone.now() - timedelta(minutes=40),
        failure_reason='provider returned 400',
    )

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0
    assert result.retried == ()


@pytest.mark.django_db
def test_two_transient_failures_past_cooldown_are_retried(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    create_linked_run(
        work,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=40),
        failure_reason='provider returned 402',
    )

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 1
    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id


@pytest.mark.django_db
def test_transient_failures_beyond_non_transient_cap_are_still_retried(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    for index in range(5):
        create_linked_run(
            work,
            created_at=now - timedelta(hours=5 - index),
            finished_at=now - timedelta(minutes=40 + index),
            failure_reason='provider timed out',
        )

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 1
    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id


@pytest.mark.django_db
def test_transient_failures_exceeding_transient_cap_are_abandoned_and_logged(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_TRANSIENT_MAX_ATTEMPTS', '2')
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 429',
    )
    create_linked_run(
        work,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=40),
        failure_reason='provider returned 503',
    )

    with capture_logs() as logs:
        result = RetryFailedDistillations().execute()

    assert result.retried == ()
    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0

    abandoned = [entry for entry in logs if entry['event'] == 'distillation_reconciler_abandoned']
    assert len(abandoned) == 1
    assert abandoned[0]['work_id'] == str(work.id)
    assert abandoned[0]['failed_count'] == 2
    assert abandoned[0]['transient_count'] == 2


@pytest.mark.django_db
def test_failed_run_without_linked_work_is_reported_unlinked_and_never_backfilled(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    legacy_run = WorkflowRun.objects.create(
        organization_id=session.organization_id,
        project_id=session.project_id,
        team_id=session.team_id,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.FAILED,
        input_snapshot={'session_id': str(session.id)},
    )
    WorkflowRun.objects.filter(id=legacy_run.id).update(finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    assert result.retried == ()
    assert legacy_run.id in result.unlinked_run_ids
    assert WorkflowWork.objects.count() == 0
    assert WorkflowRun.objects.filter(status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_retry_queues_v1_contract_attempt_with_reconciliation_origin_and_dispatched_at(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    queued = fresh_v1_runs(work)
    assert len(queued) == 1
    new_run = queued[0]
    assert new_run.execution_contract_version == 1
    assert new_run.origin == WorkflowRunOrigin.RECONCILIATION
    assert new_run.status == WorkflowRunStatus.QUEUED
    assert new_run.dispatched_at is not None
    assert new_run.fencing_token is None
    assert new_run.lease_owner == ''

    outbox = CeleryOutbox.objects.get()
    assert outbox.task_name == _DISTILL_WORK_TASK_NAME
    assert outbox.args == [str(work.id), str(new_run.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{new_run.id}'

    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id
    assert result.retried[0].run_id == new_run.id


@pytest.mark.django_db
def test_poisoned_legacy_run_is_failed_and_work_requeued_in_one_pass(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    poisoned = create_poisoned_legacy_run(work, created_at=now - timedelta(hours=1))

    result = RetryFailedDistillations().execute()

    poisoned.refresh_from_db()
    assert poisoned.status == WorkflowRunStatus.FAILED
    assert poisoned.failure_reason == _POISONED_RUN_FAILURE_REASON
    assert poisoned.finished_at is not None
    assert poisoned.execution_contract_version == 0

    queued = fresh_v1_runs(work)
    assert len(queued) == 1
    new_run = queued[0]
    assert new_run.origin == WorkflowRunOrigin.RECONCILIATION
    assert new_run.dispatched_at is not None

    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id
    assert result.retried[0].run_id == new_run.id

    assert CeleryOutbox.objects.filter(task_name=_DISTILL_WORK_TASK_NAME).count() == 1
    outbox = CeleryOutbox.objects.get(task_name=_DISTILL_WORK_TASK_NAME)
    assert outbox.args == [str(work.id), str(new_run.id)]


@pytest.mark.django_db
def test_poisoned_run_cleanup_does_not_count_toward_non_transient_attempt_cap(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 400',
    )
    create_poisoned_legacy_run(work, created_at=now - timedelta(hours=1))

    with capture_logs() as logs:
        result = RetryFailedDistillations().execute()

    assert [entry for entry in logs if entry['event'] == 'distillation_reconciler_abandoned'] == []
    assert len(fresh_v1_runs(work)) == 1
    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id


@pytest.mark.django_db
def test_abandonment_accounting_ignores_poisoned_runs_but_still_terminalizes_them(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=3),
        finished_at=now - timedelta(hours=3),
        failure_reason='provider returned 400',
    )
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(minutes=40),
        failure_reason='provider returned 400',
    )
    poisoned = create_poisoned_legacy_run(work, created_at=now - timedelta(hours=1))

    with capture_logs() as logs:
        result = RetryFailedDistillations().execute()

    poisoned.refresh_from_db()
    assert poisoned.status == WorkflowRunStatus.FAILED
    assert poisoned.failure_reason == _POISONED_RUN_FAILURE_REASON

    abandoned = [entry for entry in logs if entry['event'] == 'distillation_reconciler_abandoned']
    assert len(abandoned) == 1
    assert abandoned[0]['work_id'] == str(work.id)
    assert abandoned[0]['failed_count'] == 2
    assert abandoned[0]['transient_count'] == 0

    assert result.retried == ()
    assert fresh_v1_runs(work) == []
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_v1_managed_session_keeps_its_runs_and_is_never_touched_by_cleanup(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent, end_work_contract_version=1)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
    )
    poisoned = create_poisoned_legacy_run(work, created_at=now - timedelta(hours=1))

    result = RetryFailedDistillations().execute()

    poisoned.refresh_from_db()
    assert poisoned.status == WorkflowRunStatus.QUEUED
    assert poisoned.failure_reason == ''
    assert result.retried == ()
    assert fresh_v1_runs(work) == []
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_execute_calls_create_one_queued_run_and_one_signal(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=40))
    barrier = Barrier(2)

    def reconcile() -> object:
        close_old_connections()
        try:
            barrier.wait(timeout=5)

            return RetryFailedDistillations().execute()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reconcile) for _index in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert sum(len(result.retried) for result in results) == 1
    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 1
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_WORK_TASK_NAME).count() == 1
    assert CeleryOutbox.objects.filter(task_name=_LEGACY_DISTILL_TASK_NAME).count() == 0


@pytest.mark.django_db
def test_stale_v1_queued_run_is_resignaled_without_new_row(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    queued = create_v1_queued_run(
        work,
        created_at=now - timedelta(hours=1),
        dispatched_at=now - timedelta(hours=1),
    )
    run_ids_before = set(WorkflowRun.objects.values_list('id', flat=True))
    original_dispatched_at = queued.dispatched_at

    result = RetryFailedDistillations().execute()

    assert set(WorkflowRun.objects.values_list('id', flat=True)) == run_ids_before

    outbox = CeleryOutbox.objects.get()
    assert outbox.task_name == _DISTILL_WORK_TASK_NAME
    assert outbox.task_id == f'workflow-work:{work.id}:run:{queued.id}'
    assert outbox.args == [str(work.id), str(queued.id)]

    queued.refresh_from_db()
    assert queued.dispatched_at > original_dispatched_at

    assert result.retried == ()
    assert len(result.resignaled) == 1
    assert result.resignaled[0].work_id == work.id
    assert result.resignaled[0].run_id == queued.id


@pytest.mark.django_db
def test_recent_v1_queued_run_within_window_is_not_resignaled(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    queued = create_v1_queued_run(
        work,
        created_at=now - timedelta(hours=1),
        dispatched_at=now - timedelta(minutes=2),
    )
    run_ids_before = set(WorkflowRun.objects.values_list('id', flat=True))
    original_dispatched_at = queued.dispatched_at

    result = RetryFailedDistillations().execute()

    assert set(WorkflowRun.objects.values_list('id', flat=True)) == run_ids_before
    assert CeleryOutbox.objects.count() == 0

    queued.refresh_from_db()
    assert queued.dispatched_at == original_dispatched_at

    assert result.retried == ()
    assert result.resignaled == ()


@pytest.mark.django_db
def test_latest_v1_running_run_stays_suppressed_without_resignal(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    running = create_linked_run(work, status=WorkflowRunStatus.QUEUED, created_at=now - timedelta(hours=1))
    WorkflowRun.objects.filter(id=running.id).update(
        execution_contract_version=1,
        origin=WorkflowRunOrigin.RECONCILIATION,
        status=WorkflowRunStatus.RUNNING,
        fencing_token=1,
        lease_owner='worker-1',
        dispatched_at=now - timedelta(hours=1),
        started_at=now - timedelta(minutes=50),
        heartbeat_at=now - timedelta(minutes=1),
        lease_expires_at=now + timedelta(minutes=5),
    )
    run_ids_before = set(WorkflowRun.objects.values_list('id', flat=True))

    result = RetryFailedDistillations().execute()

    assert set(WorkflowRun.objects.values_list('id', flat=True)) == run_ids_before
    assert CeleryOutbox.objects.count() == 0
    assert result.retried == ()
    assert result.resignaled == ()


@pytest.mark.django_db
def test_blocked_work_with_stale_v1_queued_run_still_resignals(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    queued = create_v1_queued_run(
        work,
        created_at=now - timedelta(hours=1),
        dispatched_at=now - timedelta(hours=1),
    )
    WorkflowWork.objects.filter(id=work.id).update(
        execution_state=WorkflowWorkExecutionState.BLOCKED,
        blocked_configuration_fingerprint='a' * 64,
    )
    original_dispatched_at = queued.dispatched_at

    result = RetryFailedDistillations().execute()

    assert WorkflowRun.objects.filter(work=work).count() == 2

    outbox = CeleryOutbox.objects.get()
    assert outbox.task_name == _DISTILL_WORK_TASK_NAME
    assert outbox.task_id == f'workflow-work:{work.id}:run:{queued.id}'
    assert outbox.args == [str(work.id), str(queued.id)]

    queued.refresh_from_db()
    assert queued.dispatched_at > original_dispatched_at

    assert result.retried == ()
    assert len(result.resignaled) == 1
    assert result.resignaled[0].work_id == work.id
    assert result.resignaled[0].run_id == queued.id


@pytest.mark.django_db(transaction=True)
def test_poisoned_cleanup_does_not_overwrite_concurrent_legacy_success(
    monkeypatch: pytest.MonkeyPatch,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_session_work(session)
    now = timezone.now()
    create_linked_run(
        work,
        created_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    poisoned = create_poisoned_legacy_run(
        work,
        created_at=now - timedelta(hours=1),
    )

    poison_ids_selected = Event()
    success_committed = Event()
    real_update = QuerySet.update

    def pause_poison_update(queryset: QuerySet, **kwargs: object) -> int:
        if kwargs.get('failure_reason') == _POISONED_RUN_FAILURE_REASON:
            poison_ids_selected.set()
            assert success_committed.wait(timeout=10)

        return real_update(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, 'update', pause_poison_update)

    def reconcile() -> object:
        close_old_connections()
        try:
            with transaction.atomic():
                return RetryFailedDistillations().execute()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reconcile)
        assert poison_ids_selected.wait(timeout=10)

        succeeded_at = timezone.now()
        try:
            with transaction.atomic():
                completed = WorkflowRun.objects.filter(
                    id=poisoned.id,
                    status=WorkflowRunStatus.QUEUED,
                ).update(
                    status=WorkflowRunStatus.SUCCEEDED,
                    failure_reason='',
                    finished_at=succeeded_at,
                    updated_at=succeeded_at,
                )
                assert completed == 1
        finally:
            success_committed.set()

        result = future.result(timeout=15)

    poisoned.refresh_from_db()
    assert poisoned.status == WorkflowRunStatus.SUCCEEDED
    assert poisoned.failure_reason == ''
    assert poisoned.finished_at == succeeded_at
    assert result.retried == ()
    assert fresh_v1_runs(work) == []
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_WORK_TASK_NAME).count() == 0


@pytest.mark.django_db
def test_legacy_reconciler_reclaims_expired_lease_stuck_running_work(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    from engram.memory.work_dispatch import queue_work_attempt
    from engram.memory.work_execution import claim_work

    session = create_session(f_org, f_team, f_project, f_agent)
    assert session.end_work_contract_version == 0
    work = create_required_session_work(session)
    now = timezone.now()
    claimed_at = now - timedelta(hours=2)
    queued = queue_work_attempt(
        work_id=work.id,
        now=claimed_at,
        origin=WorkflowRunOrigin.RECONCILIATION,
    )
    claimed = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner='stuck-worker',
        now=claimed_at,
        lease_for=timedelta(minutes=5),
        workflow_run_id=queued.id,
    )
    assert claimed.claim is not None
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.LEASED
    assert work.lease_expires_at is not None
    assert work.lease_expires_at < now
    CeleryOutbox.objects.all().delete()

    result = RetryFailedDistillations().execute()

    assert len(result.retried) == 1
    assert result.retried[0].work_id == work.id
    assert (
        WorkflowRun.objects.filter(
            work=work,
            status=WorkflowRunStatus.QUEUED,
            execution_contract_version=1,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_WORK_TASK_NAME).count() == 1
