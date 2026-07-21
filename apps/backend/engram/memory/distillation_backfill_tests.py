from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    AgentSession,
    Observation,
    Organization,
    Project,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.distillation_backfill import (
    DEFAULT_FAILURE_CODES,
    redrive_target,
    select_targets,
)
from engram.memory.observation_work_tests import create_scope
from engram.memory.session_lifecycle import EndSession
from engram.memory.work_execution import (
    ClaimResult,
    claim_work,
    execution_configuration_fingerprint,
    fail_work_claim,
    finish_work_claim,
)
from engram.memory.work_failures import CONFIGURATION, ClassifiedWorkFailure

SessionScope = tuple[Organization, Project, AgentSession]

_LEASE = timedelta(seconds=720)
_RETRY_STEP = timedelta(seconds=1800)
_DISTILL_TASK = 'engram.memory.distill_session_work_v1'
_STREAK_LIMIT = 12


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

    return EndSession().execute(
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


def _claim(work: WorkflowWork, *, now: datetime) -> ClaimResult:
    return claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:backfill:{uuid.uuid4()}',
        now=now,
        lease_for=_LEASE,
    )


def _fail_work(
    work: WorkflowWork,
    *,
    code: str,
    failure_class: str,
    times: int,
    now: datetime,
) -> None:
    current = now
    for _index in range(times):
        claimed = _claim(work, now=current)
        fingerprint = ''
        if failure_class == CONFIGURATION:
            fingerprint = execution_configuration_fingerprint(WorkflowWork.objects.get(id=work.id))
        fail_work_claim(
            claim=claimed.claim,
            now=current,
            failure=ClassifiedWorkFailure(
                failure_class=failure_class,
                code=code,
                configuration_fingerprint=fingerprint,
            ),
        )
        current = current + _RETRY_STEP


def _succeed(work: WorkflowWork, *, now: datetime) -> None:
    claimed = _claim(work, now=now)
    finish_work_claim(claim=claimed.claim, now=now, completion='product_succeeded')


def _latest_run(work: WorkflowWork) -> WorkflowRun | None:
    return (
        WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
        .order_by('-created_at', '-id')
        .first()
    )


@pytest.fixture
def f_scope() -> SessionScope:
    return create_scope('backfill-redrive')


@pytest.mark.django_db
def test_select_targets_picks_latest_failed_in_set() -> None:
    now = timezone.now()
    scope_a = create_scope('backfill-select-a')
    work_a = _current_work(scope_a, sequence=1)
    _fail_work(work_a, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    scope_b = create_scope('backfill-select-b')
    work_b = _current_work(scope_b, sequence=1)
    _fail_work(work_b, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)
    _succeed(work_b, now=now + timedelta(hours=1))

    scope_c = create_scope('backfill-select-c')
    work_c = _current_work(scope_c, sequence=1)
    _fail_work(work_c, code='provider_timeout', failure_class='provider_transient', times=1, now=now)

    targets = select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=100)

    assert {target.work_id for target in targets} == {work_a.id}
    target = targets[0]
    assert target.session_id == work_a.subject_id
    assert target.failure_code == 'provider_output_malformed'
    assert _latest_run(work_c) is not None


@pytest.mark.django_db
def test_select_targets_scope_and_limit() -> None:
    now = timezone.now()
    scope_1 = create_scope('backfill-scope-1')
    work_1 = _current_work(scope_1, sequence=1)
    _fail_work(work_1, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    scope_2 = create_scope('backfill-scope-2')
    work_2 = _current_work(scope_2, sequence=1)
    _fail_work(work_2, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    WorkflowWork.objects.filter(id=work_1.id).update(created_at=now - timedelta(hours=1))

    scoped = select_targets(
        failure_codes=DEFAULT_FAILURE_CODES,
        limit=100,
        organization_id=scope_1[0].id,
        project_id=scope_1[1].id,
    )
    assert {target.work_id for target in scoped} == {work_1.id}

    limited = select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=1)
    assert len(limited) == 1
    assert limited[0].work_id == work_1.id
    assert {work_1.id, work_2.id} == {
        target.work_id
        for target in select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=100)
    }
    assert WorkflowRun.objects.filter(work_id=work_2.id, status=WorkflowRunStatus.FAILED).exists()


@pytest.mark.django_db
def test_redrive_resets_terminal_failure_and_dispatches(f_scope: SessionScope) -> None:
    now = timezone.now()
    work = _current_work(f_scope, sequence=1)
    _fail_work(
        work,
        code='provider_output_malformed',
        failure_class='provider_transient',
        times=_STREAK_LIMIT,
        now=now,
    )
    assert WorkflowWork.objects.get(id=work.id).execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    CeleryOutbox.objects.all().delete()

    run_id = redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())

    assert isinstance(run_id, uuid.UUID)
    reloaded = WorkflowWork.objects.get(id=work.id)
    assert reloaded.execution_state == WorkflowWorkExecutionState.READY
    assert reloaded.failure_streak == 0
    assert reloaded.next_retry_at is None
    assert reloaded.blocked_configuration_fingerprint == ''
    assert reloaded.lease_owner == ''
    assert reloaded.lease_expires_at is None
    assert reloaded.heartbeat_at is None
    queued = WorkflowRun.objects.get(id=run_id)
    assert queued.status == WorkflowRunStatus.QUEUED
    assert queued.execution_contract_version == 1
    assert queued.origin == WorkflowRunOrigin.RECONCILIATION
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1


@pytest.mark.django_db
def test_redrive_clears_blocked_config(f_scope: SessionScope) -> None:
    now = timezone.now()
    work = _current_work(f_scope, sequence=1)
    _fail_work(
        work,
        code='provider_account_unavailable',
        failure_class=CONFIGURATION,
        times=1,
        now=now,
    )
    blocked = WorkflowWork.objects.get(id=work.id)
    assert blocked.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert blocked.blocked_configuration_fingerprint != ''
    CeleryOutbox.objects.all().delete()

    run_id = redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())

    assert isinstance(run_id, uuid.UUID)
    reloaded = WorkflowWork.objects.get(id=work.id)
    assert reloaded.execution_state == WorkflowWorkExecutionState.READY
    assert reloaded.blocked_configuration_fingerprint == ''
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 1


@pytest.mark.django_db
def test_redrive_preserves_identity_fields(f_scope: SessionScope) -> None:
    now = timezone.now()
    work = _current_work(f_scope, sequence=1)
    _fail_work(
        work,
        code='provider_output_malformed',
        failure_class='provider_transient',
        times=1,
        now=now,
    )
    before = WorkflowWork.objects.get(id=work.id)
    fencing_token = before.fencing_token
    contract_version = before.contract_version
    input_fingerprint = before.input_fingerprint
    input_snapshot = before.input_snapshot
    disposition = before.disposition
    subject_id = before.subject_id
    team_id = before.team_id

    redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())

    after = WorkflowWork.objects.get(id=work.id)
    assert after.fencing_token == fencing_token
    assert after.contract_version == contract_version
    assert after.input_fingerprint == input_fingerprint
    assert after.input_snapshot == input_snapshot
    assert after.disposition == disposition
    assert after.subject_id == subject_id
    assert after.team_id == team_id


@pytest.mark.django_db
def test_redrive_idempotent_second_call_skips(f_scope: SessionScope) -> None:
    now = timezone.now()
    work = _current_work(f_scope, sequence=1)
    _fail_work(
        work,
        code='provider_output_malformed',
        failure_class='provider_transient',
        times=1,
        now=now,
    )

    first = redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())
    assert isinstance(first, uuid.UUID)
    queued_count = WorkflowRun.objects.filter(
        work_id=work.id,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    ).count()

    second = redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())

    assert second is None
    assert (
        WorkflowRun.objects.filter(
            work_id=work.id,
            status=WorkflowRunStatus.QUEUED,
            execution_contract_version=1,
        ).count()
        == queued_count
    )


@pytest.mark.django_db
def test_redrive_reset_reclaimable(f_scope: SessionScope) -> None:
    now = timezone.now()
    work = _current_work(f_scope, sequence=1)
    _fail_work(
        work,
        code='provider_output_malformed',
        failure_class='provider_transient',
        times=_STREAK_LIMIT,
        now=now,
    )
    redrive_target(work_id=work.id, failure_codes=DEFAULT_FAILURE_CODES, now=timezone.now())
    token_before = WorkflowWork.objects.get(id=work.id).fencing_token

    claimed = _claim(work, now=timezone.now())

    assert claimed.claim is not None
    assert claimed.claim.fencing_token == token_before + 1
