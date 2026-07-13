from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import ModuleType

import pytest
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    AgentSession,
    Observation,
    Organization,
    Project,
    SessionStatus,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
)
from engram.memory.observation_work_tests import create_scope
from engram.memory.work_failures import (
    CONFIGURATION,
    INVALID_INPUT,
    PROVIDER_TRANSIENT,
    ClassifiedWorkFailure,
)

SessionScope = tuple[Organization, Project, AgentSession]

_SESSION_LEASE = timedelta(seconds=720)
_GRACE = timedelta(minutes=5)
_STALE = timedelta(minutes=6)
_HEX_A = 'a' * 64
_DISTILL_TASK = 'engram.memory.distill_session_work_v1'

POSTGRES = connection.vendor == 'postgresql'
requires_postgres = pytest.mark.skipif(not POSTGRES, reason='concurrency evidence requires PostgreSQL row locks')


def _reconciler() -> ModuleType:
    from engram.memory import session_work_reconciler

    return session_work_reconciler


def _we() -> ModuleType:
    from engram.memory import work_execution

    return work_execution


def _end_session_service() -> object:
    from engram.memory.session_lifecycle import EndSession

    return EndSession()


def _seed(session: AgentSession, *, sequence: int, event_type: str = 'post_tool_use') -> Observation:
    return Observation.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {sequence}',
        content_hash=f'content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': event_type},
    )


def _end(scope: SessionScope) -> object:
    organization, project, session = scope

    return _end_session_service().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )


def _current_work(scope: SessionScope, *, sequence: int) -> WorkflowWork:
    _seed(scope[2], sequence=sequence)
    result = _end(scope)

    return WorkflowWork.objects.get(id=result.work_id)


def _owner(tag: str) -> str:
    return f'host:{tag}:{uuid.uuid4()}'


def _claim(work: WorkflowWork, *, now: object, tag: str = 'worker') -> object:
    from engram.core.models import WorkflowWorkType

    return _we().claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=_owner(tag),
        now=now,
        lease_for=_SESSION_LEASE,
    )


def _settle(work: WorkflowWork, *, now: object) -> None:
    claimed = _claim(work, now=now, tag='settle')
    _we().finish_work_claim(claim=claimed.claim, now=now, completion='product_succeeded')


def _queued_run(work: WorkflowWork, *, dispatched_at: object) -> WorkflowRun:
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


def _backdate_required_work(work: WorkflowWork, session: AgentSession, when: object) -> None:
    WorkflowWork.objects.filter(id=work.id).update(created_at=when)
    AgentSession.objects.filter(id=session.id).update(ended_at=when)


def _inspect(scope: SessionScope, *, as_of: object) -> list[object]:
    organization, project, _session = scope
    result = _reconciler().inspect_session_work(
        organization_id=organization.id,
        project_id=project.id,
        as_of=as_of,
    )

    return list(getattr(result, 'findings', result))


def _reconcile(scope: SessionScope, *, as_of: object) -> object:
    organization, project, _session = scope

    return _reconciler().reconcile_session_work(
        organization_id=organization.id,
        project_id=project.id,
        as_of=as_of,
    )


def _codes(findings: list[object]) -> list[str]:
    return sorted(finding.code for finding in findings)


def _one(findings: list[object], code: str) -> object:
    matches = [finding for finding in findings if finding.code == code]
    assert len(matches) == 1, f'expected exactly one {code!r}, got {_codes(findings)}'

    return matches[0]


@pytest.mark.django_db
def test_missing_current_work_reports_session_current_work_missing() -> None:
    scope = create_scope('reconcile-missing')
    work = _current_work(scope, sequence=1)
    WorkflowRun.objects.filter(work=work).delete()
    work.delete()

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'session_current_work_missing')
    assert finding.work_id is None
    assert finding.entity_id == str(scope[2].id)


@pytest.mark.django_db
def test_required_latest_with_older_success_reports_incomplete() -> None:
    scope = create_scope('reconcile-incomplete')
    organization, project, session = scope
    now = timezone.now()
    older = _current_work(scope, sequence=1)
    _settle(older, now=now)

    AgentSession.objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=1,
    )
    latest = _current_work(scope, sequence=2)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'session_current_work_incomplete')
    assert finding.work_id == latest.id
    assert all(other.work_id != older.id for other in findings)


@pytest.mark.django_db
def test_required_ready_no_run_past_grace_reports_work_never_claimed() -> None:
    scope = create_scope('reconcile-never-claimed')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)

    findings = _inspect(scope, as_of=now)

    finding = _one(findings, 'work_never_claimed')
    assert finding.work_id == work.id
    assert finding.workflow_run_id is None


@pytest.mark.django_db
def test_stale_queued_attempt_reports_attempt_signal_stale() -> None:
    scope = create_scope('reconcile-signal-stale')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    queued = _queued_run(work, dispatched_at=now - _STALE)

    findings = _inspect(scope, as_of=now)

    finding = _one(findings, 'attempt_signal_stale')
    assert finding.work_id == work.id
    assert finding.workflow_run_id == queued.id


@pytest.mark.django_db
def test_expired_lease_reports_lease_expired() -> None:
    scope = create_scope('reconcile-lease-expired')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)

    findings = _inspect(scope, as_of=now + _SESSION_LEASE + timedelta(seconds=60))

    finding = _one(findings, 'lease_expired')
    assert finding.work_id == work.id
    assert finding.workflow_run_id == claimed.claim.workflow_run_id


@pytest.mark.django_db
def test_retry_wait_due_reports_logical_retry_due() -> None:
    scope = create_scope('reconcile-retry-due')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))

    finding = _one(findings, 'logical_retry_due')
    assert finding.work_id == work.id


@pytest.mark.django_db
def test_older_success_never_hides_later_retry_failure() -> None:
    scope = create_scope('reconcile-no-hide')
    organization, project, session = scope
    now = timezone.now()
    older = _current_work(scope, sequence=1)
    _settle(older, now=now)

    AgentSession.objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=1,
    )
    latest = _current_work(scope, sequence=2)
    claimed = _claim(latest, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))

    finding = _one(findings, 'logical_retry_due')
    assert finding.work_id == latest.id
    assert all(other.work_id != older.id for other in findings)


@pytest.mark.django_db
def test_blocked_unchanged_fingerprint_reports_configuration_blocked() -> None:
    scope = create_scope('reconcile-config-blocked')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    real_fingerprint = _we().execution_configuration_fingerprint(work)
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(
            failure_class=CONFIGURATION,
            code='model_policy_unavailable',
            configuration_fingerprint=real_fingerprint,
        ),
    )

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))

    finding = _one(findings, 'configuration_blocked')
    assert finding.work_id == work.id


@pytest.mark.django_db
def test_changed_fingerprint_reports_configuration_changed() -> None:
    scope = create_scope('reconcile-config-changed')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    assert _we().execution_configuration_fingerprint(work) != _HEX_A
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(
            failure_class=CONFIGURATION,
            code='model_policy_unavailable',
            configuration_fingerprint=_HEX_A,
        ),
    )

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))

    finding = _one(findings, 'configuration_changed')
    assert finding.work_id == work.id


@pytest.mark.django_db
def test_terminal_input_failure_reports_terminal_input_failure() -> None:
    scope = create_scope('reconcile-terminal')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=INVALID_INPUT, code='work_contract_invalid'),
    )

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))

    finding = _one(findings, 'terminal_input_failure')
    assert finding.work_id == work.id
    stored = WorkflowWork.objects.get(id=work.id)
    assert stored.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert stored.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_generation_zero_terminal_no_op_work_is_healthy() -> None:
    scope = create_scope('reconcile-gen-zero')
    _seed(scope[2], sequence=1, event_type='session_start')
    _seed(scope[2], sequence=2, event_type='session_end')
    result = _end(scope)

    work = WorkflowWork.objects.get(id=result.work_id)
    assert work.disposition == WorkflowWorkDisposition.NO_OP

    findings = _inspect(scope, as_of=timezone.now())

    assert [finding for finding in findings if finding.entity_id == str(scope[2].id)] == []


@pytest.mark.django_db
def test_non_marker_ended_session_is_untouched() -> None:
    scope = create_scope('reconcile-v0')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)
    AgentSession.objects.filter(id=scope[2].id).update(end_work_contract_version=0)
    CeleryOutbox.objects.all().delete()

    findings = _inspect(scope, as_of=now)
    assert findings == []

    _reconcile(scope, as_of=now)
    assert WorkflowRun.objects.filter(work=work).count() == 0
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 0


@pytest.mark.django_db
def test_foreign_scope_negative_control() -> None:
    scope = create_scope('reconcile-owned')
    foreign = create_scope('reconcile-foreign')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)
    CeleryOutbox.objects.all().delete()

    assert _inspect(foreign, as_of=now) == []

    _reconcile(foreign, as_of=now)
    assert WorkflowRun.objects.filter(work=work).count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_inspect_is_read_only() -> None:
    scope = create_scope('reconcile-readonly')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)
    CeleryOutbox.objects.all().delete()

    runs_before = WorkflowRun.objects.count()
    works_before = WorkflowWork.objects.count()

    with CaptureQueriesContext(connection) as queries:
        findings = _inspect(scope, as_of=now)

    assert findings
    write_statements = [
        entry['sql']
        for entry in queries.captured_queries
        if entry['sql'].strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert write_statements == []
    assert WorkflowRun.objects.count() == runs_before
    assert WorkflowWork.objects.count() == works_before
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_reconcile_work_never_claimed_queues_one_attempt_and_is_idempotent() -> None:
    scope = create_scope('reconcile-apply-never-claimed')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)
    CeleryOutbox.objects.all().delete()

    _reconcile(scope, as_of=now)

    queued = WorkflowRun.objects.filter(
        work=work,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    )
    assert queued.count() == 1
    assert queued.get().origin == WorkflowRunOrigin.RECONCILIATION
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1

    _reconcile(scope, as_of=now)

    assert queued.count() == 1
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1


@pytest.mark.django_db
def test_reconcile_logical_retry_due_queues_one_new_run() -> None:
    scope = create_scope('reconcile-apply-retry-due')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )
    CeleryOutbox.objects.all().delete()

    _reconcile(scope, as_of=now + timedelta(seconds=60))

    queued = WorkflowRun.objects.filter(
        work=work,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    )
    assert queued.count() == 1
    assert queued.get().origin == WorkflowRunOrigin.RECONCILIATION
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1


@pytest.mark.django_db
def test_reconcile_configuration_changed_clears_block_and_queues_run() -> None:
    scope = create_scope('reconcile-apply-config-changed')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)
    _we().fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(
            failure_class=CONFIGURATION,
            code='model_policy_unavailable',
            configuration_fingerprint=_HEX_A,
        ),
    )
    CeleryOutbox.objects.all().delete()

    _reconcile(scope, as_of=now + timedelta(seconds=60))

    stored = WorkflowWork.objects.get(id=work.id)
    assert stored.execution_state != WorkflowWorkExecutionState.BLOCKED
    assert stored.blocked_configuration_fingerprint == ''
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() >= 2


@pytest.mark.django_db
@pytest.mark.parametrize('code', ('configuration_blocked', 'terminal_input_failure'))
def test_reconcile_report_only_codes_mutate_nothing(code: str) -> None:
    scope = create_scope(f'reconcile-report-only-{code}')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    claimed = _claim(work, now=now)
    if code == 'configuration_blocked':
        failure = ClassifiedWorkFailure(
            failure_class=CONFIGURATION,
            code='model_policy_unavailable',
            configuration_fingerprint=_we().execution_configuration_fingerprint(work),
        )
    else:
        failure = ClassifiedWorkFailure(failure_class=INVALID_INPUT, code='work_contract_invalid')
    _we().fail_work_claim(claim=claimed.claim, now=now, failure=failure)
    CeleryOutbox.objects.all().delete()

    findings = _inspect(scope, as_of=now + timedelta(seconds=60))
    assert _one(findings, code)

    runs_before = WorkflowRun.objects.filter(work=work).count()
    state_before = WorkflowWork.objects.get(id=work.id).execution_state

    _reconcile(scope, as_of=now + timedelta(seconds=60))

    assert WorkflowRun.objects.filter(work=work).count() == runs_before
    assert WorkflowWork.objects.get(id=work.id).execution_state == state_before
    assert CeleryOutbox.objects.count() == 0


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_reconcile_converges_on_one_attempt() -> None:
    scope = create_scope('reconcile-concurrent')
    now = timezone.now()
    work = _current_work(scope, sequence=1)
    _backdate_required_work(work, scope[2], now - _STALE)
    CeleryOutbox.objects.all().delete()
    barrier = Barrier(2)

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            _reconcile(scope, as_of=now)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _index in range(2)]
        for future in futures:
            future.result(timeout=15)

    assert (
        WorkflowRun.objects.filter(
            work=work,
            status=WorkflowRunStatus.QUEUED,
            execution_contract_version=1,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1
