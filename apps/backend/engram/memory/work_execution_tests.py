from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from django.db import close_old_connections, connection
from django.db.transaction import TransactionManagementError

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.work_failures import ClassifiedWorkFailure
from engram.memory.workflow_work_tests import (
    create_required_work,
    create_scope,
)

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
OBS_LEASE = timedelta(seconds=120)
HEX_A = 'a' * 64
HEX_B = 'b' * 64

POSTGRES = connection.vendor == 'postgresql'
requires_postgres = pytest.mark.skipif(not POSTGRES, reason='concurrency evidence requires PostgreSQL row locks')


def _we() -> ModuleType:
    from engram.memory import work_execution

    return work_execution


def _wd() -> ModuleType:
    from engram.memory import work_dispatch

    return work_dispatch


def owner(tag: str) -> str:
    return f'host:{tag}:{uuid.uuid4()}'


def get_work(work: WorkflowWork) -> WorkflowWork:
    return WorkflowWork.objects.get(id=work.id)


def get_run(run_id: uuid.UUID) -> WorkflowRun:
    return WorkflowRun.objects.get(id=run_id)


def running_v1_count(work: WorkflowWork) -> int:
    return WorkflowRun.objects.filter(
        work=work,
        status=WorkflowRunStatus.RUNNING,
        execution_contract_version=1,
    ).count()


def claim(
    module: ModuleType,
    work: WorkflowWork,
    *,
    lease_owner: str,
    now: datetime,
    run_id: uuid.UUID | None = None,
    lease_for: timedelta = OBS_LEASE,
    work_type: str = WorkflowWorkType.OBSERVATION_PROCESSING,
) -> object:
    return module.claim_work(
        work_id=work.id,
        expected_work_type=work_type,
        lease_owner=lease_owner,
        now=now,
        lease_for=lease_for,
        workflow_run_id=run_id,
    )


def claim_variant(module: ModuleType, source: object, **changes: object) -> object:
    fields: dict[str, object] = {
        'work_id': source.work_id,
        'workflow_run_id': source.workflow_run_id,
        'fencing_token': source.fencing_token,
        'lease_owner': source.lease_owner,
        'lease_expires_at': source.lease_expires_at,
    }
    fields.update(changes)

    return module.WorkClaim(**fields)


def make_queued_run(
    work: WorkflowWork,
    *,
    origin: str,
    dispatched_at: datetime,
) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=origin,
        dispatched_at=dispatched_at,
        input_snapshot=work.input_snapshot,
    )


def settle_work(module: ModuleType, work: WorkflowWork) -> None:
    claimed = claim(module, work, lease_owner=owner('settle'), now=NOW)
    module.lock_work_fence(claim=claimed.claim, now=NOW)
    module.finish_work_claim(claim=claimed.claim, now=NOW, completion='product_succeeded')


@pytest.mark.django_db
def test_automatic_claim_creates_v1_run_and_leases_both_rows() -> None:
    module = _we()
    scope = create_scope('claim-automatic')
    work = create_required_work(scope, suffix='claim-automatic')
    lease_owner = owner('auto')

    result = claim(module, work, lease_owner=lease_owner, now=NOW)

    assert result.outcome == 'claimed'
    assert result.claim.work_id == work.id
    assert result.claim.fencing_token == 1
    assert result.claim.lease_owner == lease_owner
    assert result.claim.lease_expires_at == NOW + OBS_LEASE

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.LEASED
    assert stored.fencing_token == 1
    assert stored.lease_owner == lease_owner
    assert stored.lease_expires_at == NOW + OBS_LEASE
    assert stored.heartbeat_at == NOW

    run = get_run(result.claim.workflow_run_id)
    assert run.status == WorkflowRunStatus.RUNNING
    assert run.execution_contract_version == 1
    assert run.origin == WorkflowRunOrigin.AUTOMATIC
    assert run.fencing_token == 1
    assert run.lease_owner == lease_owner
    assert run.started_at == NOW
    assert run.heartbeat_at == NOW
    assert run.lease_expires_at == NOW + OBS_LEASE
    assert run.work_id == work.id


@pytest.mark.django_db
def test_supplied_queued_run_is_claimed_without_new_run() -> None:
    module = _we()
    scope = create_scope('claim-supplied')
    work = create_required_work(scope, suffix='claim-supplied')
    queued = make_queued_run(work, origin=WorkflowRunOrigin.RECONCILIATION, dispatched_at=NOW)
    lease_owner = owner('supplied')

    result = claim(module, work, lease_owner=lease_owner, now=NOW, run_id=queued.id)

    assert result.outcome == 'claimed'
    assert result.claim.workflow_run_id == queued.id
    assert WorkflowRun.objects.filter(work=work).count() == 1

    run = get_run(queued.id)
    assert run.status == WorkflowRunStatus.RUNNING
    assert run.origin == WorkflowRunOrigin.RECONCILIATION
    assert run.fencing_token == 1
    assert get_work(work).fencing_token == 1


@pytest.mark.django_db
def test_replayed_same_run_owner_token_returns_same_claim() -> None:
    module = _we()
    scope = create_scope('claim-replay')
    work = create_required_work(scope, suffix='claim-replay')
    lease_owner = owner('replay')

    first = claim(module, work, lease_owner=lease_owner, now=NOW)
    replay = claim(
        module,
        work,
        lease_owner=lease_owner,
        now=NOW + timedelta(seconds=5),
        run_id=first.claim.workflow_run_id,
    )

    assert replay.outcome == 'replayed'
    assert replay.claim.workflow_run_id == first.claim.workflow_run_id
    assert replay.claim.fencing_token == first.claim.fencing_token
    assert get_work(work).fencing_token == 1
    assert WorkflowRun.objects.filter(work=work).count() == 1


@pytest.mark.django_db
def test_busy_when_unexpired_foreign_lease_held() -> None:
    module = _we()
    scope = create_scope('claim-busy')
    work = create_required_work(scope, suffix='claim-busy')
    holder = owner('holder')

    claim(module, work, lease_owner=holder, now=NOW)
    busy = claim(module, work, lease_owner=owner('intruder'), now=NOW + timedelta(seconds=30))

    assert busy.outcome == 'busy'
    assert busy.claim is None

    stored = get_work(work)
    assert stored.lease_owner == holder
    assert stored.fencing_token == 1
    assert running_v1_count(work) == 1


@pytest.mark.django_db
def test_expired_lease_reclaim_fails_old_run_worker_lost() -> None:
    module = _we()
    scope = create_scope('claim-reclaim')
    work = create_required_work(scope, suffix='claim-reclaim')

    first = claim(module, work, lease_owner=owner('lost'), now=NOW)
    reclaim_at = NOW + timedelta(seconds=200)
    second = claim(module, work, lease_owner=owner('fresh'), now=reclaim_at)

    assert second.outcome == 'claimed'
    assert second.claim.fencing_token == 2
    assert second.claim.workflow_run_id != first.claim.workflow_run_id

    old_run = get_run(first.claim.workflow_run_id)
    assert old_run.status == WorkflowRunStatus.FAILED
    assert old_run.failure_class == 'worker_lost'
    assert old_run.failure_code == 'lease_expired'
    assert old_run.finished_at == reclaim_at
    assert old_run.fencing_token == 1

    stored = get_work(work)
    assert stored.fencing_token == 2
    assert stored.execution_state == WorkflowWorkExecutionState.LEASED
    assert running_v1_count(work) == 1


@pytest.mark.django_db
def test_not_due_before_next_retry_at() -> None:
    module = _we()
    scope = create_scope('claim-not-due')
    work = create_required_work(scope, suffix='claim-not-due')

    first = claim(module, work, lease_owner=owner('retry'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    result = claim(module, work, lease_owner=owner('early'), now=NOW + timedelta(seconds=10))

    assert result.outcome == 'not_due'
    assert result.claim is None

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert stored.next_retry_at == NOW + timedelta(seconds=30)
    assert stored.fencing_token == 1


@pytest.mark.django_db
def test_due_retry_reclaims_with_new_token() -> None:
    module = _we()
    scope = create_scope('claim-due')
    work = create_required_work(scope, suffix='claim-due')

    first = claim(module, work, lease_owner=owner('retry'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    due_at = NOW + timedelta(seconds=30)
    result = claim(module, work, lease_owner=owner('due'), now=due_at)

    assert result.outcome == 'claimed'
    assert result.claim.fencing_token == 2
    assert result.claim.workflow_run_id != first.claim.workflow_run_id

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.LEASED
    assert stored.fencing_token == 2
    assert running_v1_count(work) == 1


@pytest.mark.django_db
def test_blocked_when_configuration_fingerprint_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _we()
    scope = create_scope('claim-blocked')
    work = create_required_work(scope, suffix='claim-blocked')

    first = claim(module, work, lease_owner=owner('config'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(
            failure_class='configuration',
            code='model_policy_unavailable',
            configuration_fingerprint=HEX_A,
        ),
    )

    monkeypatch.setattr(module, 'execution_configuration_fingerprint', lambda _work: HEX_A)
    result = claim(module, work, lease_owner=owner('retry-config'), now=NOW + timedelta(hours=6))

    assert result.outcome == 'blocked'
    assert result.claim is None

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert stored.blocked_configuration_fingerprint == HEX_A
    assert stored.failure_streak == 1
    assert stored.fencing_token == 1


@pytest.mark.django_db
def test_blocked_configuration_change_resumes_and_resets_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _we()
    scope = create_scope('claim-resume')
    work = create_required_work(scope, suffix='claim-resume')

    first = claim(module, work, lease_owner=owner('config'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(
            failure_class='configuration',
            code='model_policy_unavailable',
            configuration_fingerprint=HEX_A,
        ),
    )

    monkeypatch.setattr(module, 'execution_configuration_fingerprint', lambda _work: HEX_B)
    result = claim(module, work, lease_owner=owner('resumed'), now=NOW + timedelta(hours=6))

    assert result.outcome == 'claimed'
    assert result.claim.fencing_token == 2

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.LEASED
    assert stored.blocked_configuration_fingerprint == ''
    assert stored.failure_streak == 0
    assert stored.fencing_token == 2


@pytest.mark.django_db
def test_automatic_delivery_of_settled_work_returns_terminal() -> None:
    module = _we()
    scope = create_scope('claim-settled')
    work = create_required_work(scope, suffix='claim-settled')
    settle_work(module, work)

    result = claim(module, work, lease_owner=owner('late'), now=NOW + timedelta(seconds=5))

    assert result.outcome == 'terminal'
    assert result.claim is None

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.SETTLED
    assert stored.disposition == WorkflowWorkDisposition.COMPLETE


@pytest.mark.django_db
def test_automatic_delivery_of_terminal_failure_returns_terminal() -> None:
    module = _we()
    scope = create_scope('claim-terminal')
    work = create_required_work(scope, suffix='claim-terminal')

    first = claim(module, work, lease_owner=owner('bad-input'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='invalid_input', code='work_contract_invalid'),
    )

    result = claim(module, work, lease_owner=owner('late'), now=NOW + timedelta(seconds=5))

    assert result.outcome == 'terminal'
    assert result.claim is None

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert stored.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_valid_manual_queued_run_leases_settled_work() -> None:
    module = _we()
    scope = create_scope('claim-manual')
    work = create_required_work(scope, suffix='claim-manual')
    settle_work(module, work)

    manual = make_queued_run(work, origin=WorkflowRunOrigin.MANUAL, dispatched_at=NOW + timedelta(seconds=5))
    lease_owner = owner('manual')
    result = claim(module, work, lease_owner=lease_owner, now=NOW + timedelta(seconds=10), run_id=manual.id)

    assert result.outcome == 'claimed'
    assert result.claim.workflow_run_id == manual.id

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.LEASED
    assert stored.disposition == WorkflowWorkDisposition.COMPLETE

    run = get_run(manual.id)
    assert run.status == WorkflowRunStatus.RUNNING
    assert run.origin == WorkflowRunOrigin.MANUAL


@pytest.mark.django_db
def test_claim_rejects_naive_now_blank_and_oversized_owner() -> None:
    module = _we()
    scope = create_scope('claim-inputs')
    work = create_required_work(scope, suffix='claim-inputs')

    with pytest.raises(ValueError):
        claim(module, work, lease_owner=owner('naive'), now=NOW.replace(tzinfo=None))

    with pytest.raises(ValueError):
        claim(module, work, lease_owner='', now=NOW)

    with pytest.raises(ValueError):
        claim(module, work, lease_owner='x' * 256, now=NOW)


@pytest.mark.django_db
def test_claim_rejects_wrong_expected_work_type() -> None:
    module = _we()
    scope = create_scope('claim-type')
    work = create_required_work(scope, suffix='claim-type')

    with pytest.raises(ValueError):
        claim(
            module,
            work,
            lease_owner=owner('type'),
            now=NOW,
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
        )

    assert get_work(work).execution_state == WorkflowWorkExecutionState.READY


@pytest.mark.django_db
def test_claim_rejects_tampered_fingerprint() -> None:
    module = _we()
    scope = create_scope('claim-fingerprint')
    work = create_required_work(scope, suffix='claim-fingerprint')
    WorkflowWork.objects.filter(id=work.id).update(input_fingerprint='f' * 64)

    with pytest.raises(ValueError):
        claim(module, work, lease_owner=owner('tampered'), now=NOW)

    assert get_work(work).execution_state == WorkflowWorkExecutionState.READY


@pytest.mark.django_db
def test_claim_rejects_invalid_supplied_run() -> None:
    module = _we()
    scope = create_scope('claim-invalid-run')
    work = create_required_work(scope, suffix='claim-invalid-run')

    with pytest.raises(ValueError):
        claim(module, work, lease_owner=owner('missing'), now=NOW, run_id=uuid.uuid4())


@pytest.mark.django_db
def test_claim_rejects_linked_v0_run_as_v1() -> None:
    module = _we()
    scope = create_scope('claim-v0')
    work = create_required_work(scope, suffix='claim-v0')
    legacy = WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=0,
        input_snapshot=work.input_snapshot,
    )

    with pytest.raises(ValueError):
        claim(module, work, lease_owner=owner('v0'), now=NOW, run_id=legacy.id)

    stored = get_run(legacy.id)
    assert stored.execution_contract_version == 0
    assert stored.status == WorkflowRunStatus.QUEUED


@pytest.mark.django_db
def test_heartbeat_extends_lease_for_valid_owner() -> None:
    module = _we()
    scope = create_scope('heartbeat-valid')
    work = create_required_work(scope, suffix='heartbeat-valid')
    first = claim(module, work, lease_owner=owner('beat'), now=NOW)

    beat_at = NOW + timedelta(seconds=30)
    extended = module.heartbeat_work(claim=first.claim, now=beat_at, lease_for=OBS_LEASE)

    assert extended.workflow_run_id == first.claim.workflow_run_id
    assert extended.fencing_token == first.claim.fencing_token
    assert extended.lease_expires_at == beat_at + OBS_LEASE

    stored = get_work(work)
    assert stored.heartbeat_at == beat_at
    assert stored.lease_expires_at == beat_at + OBS_LEASE

    run = get_run(first.claim.workflow_run_id)
    assert run.heartbeat_at == beat_at
    assert run.lease_expires_at == beat_at + OBS_LEASE


@pytest.mark.django_db
def test_heartbeat_rejects_stale_owner() -> None:
    module = _we()
    scope = create_scope('heartbeat-owner')
    work = create_required_work(scope, suffix='heartbeat-owner')
    first = claim(module, work, lease_owner=owner('beat'), now=NOW)
    stale = claim_variant(module, first.claim, lease_owner=owner('other'))

    with pytest.raises(module.StaleWorkFenceError):
        module.heartbeat_work(claim=stale, now=NOW + timedelta(seconds=30), lease_for=OBS_LEASE)


@pytest.mark.django_db
def test_heartbeat_rejects_stale_token() -> None:
    module = _we()
    scope = create_scope('heartbeat-token')
    work = create_required_work(scope, suffix='heartbeat-token')
    first = claim(module, work, lease_owner=owner('beat'), now=NOW)
    stale = claim_variant(module, first.claim, fencing_token=first.claim.fencing_token + 1)

    with pytest.raises(module.StaleWorkFenceError):
        module.heartbeat_work(claim=stale, now=NOW + timedelta(seconds=30), lease_for=OBS_LEASE)


@pytest.mark.django_db
def test_heartbeat_rejects_after_expiry() -> None:
    module = _we()
    scope = create_scope('heartbeat-expiry')
    work = create_required_work(scope, suffix='heartbeat-expiry')
    first = claim(module, work, lease_owner=owner('beat'), now=NOW)

    with pytest.raises(module.StaleWorkFenceError):
        module.heartbeat_work(claim=first.claim, now=NOW + timedelta(seconds=200), lease_for=OBS_LEASE)


@pytest.mark.django_db
def test_lock_work_fence_returns_locked_work_and_run() -> None:
    module = _we()
    scope = create_scope('fence-happy')
    work = create_required_work(scope, suffix='fence-happy')
    first = claim(module, work, lease_owner=owner('fence'), now=NOW)

    locked_work, locked_run = module.lock_work_fence(claim=first.claim, now=NOW + timedelta(seconds=10))

    assert locked_work.id == work.id
    assert locked_work.execution_state == WorkflowWorkExecutionState.LEASED
    assert locked_run.id == first.claim.workflow_run_id
    assert locked_run.status == WorkflowRunStatus.RUNNING


@pytest.mark.django_db(transaction=True)
def test_lock_work_fence_requires_active_transaction() -> None:
    module = _we()
    scope = create_scope('fence-no-txn')
    work = create_required_work(scope, suffix='fence-no-txn')
    first = claim(module, work, lease_owner=owner('fence'), now=NOW)

    with pytest.raises(TransactionManagementError):
        module.lock_work_fence(claim=first.claim, now=NOW + timedelta(seconds=10))


@pytest.mark.django_db
def test_lock_work_fence_raises_on_expired_lease() -> None:
    module = _we()
    scope = create_scope('fence-expired')
    work = create_required_work(scope, suffix='fence-expired')
    first = claim(module, work, lease_owner=owner('fence'), now=NOW)

    with pytest.raises(module.StaleWorkFenceError):
        module.lock_work_fence(claim=first.claim, now=NOW + timedelta(seconds=200))


@pytest.mark.django_db
def test_lock_work_fence_raises_on_older_token() -> None:
    module = _we()
    scope = create_scope('fence-token')
    work = create_required_work(scope, suffix='fence-token')
    first = claim(module, work, lease_owner=owner('first'), now=NOW)
    claim(module, work, lease_owner=owner('second'), now=NOW + timedelta(seconds=200))

    with pytest.raises(module.StaleWorkFenceError):
        module.lock_work_fence(claim=first.claim, now=NOW + timedelta(seconds=210))


@pytest.mark.django_db
def test_lock_work_fence_raises_on_wrong_owner() -> None:
    module = _we()
    scope = create_scope('fence-owner')
    work = create_required_work(scope, suffix='fence-owner')
    first = claim(module, work, lease_owner=owner('fence'), now=NOW)
    stale = claim_variant(module, first.claim, lease_owner=owner('other'))

    with pytest.raises(module.StaleWorkFenceError):
        module.lock_work_fence(claim=stale, now=NOW + timedelta(seconds=10))


@pytest.mark.django_db
def test_finish_product_succeeded_settles_work_and_succeeds_run() -> None:
    module = _we()
    scope = create_scope('finish-succeeded')
    work = create_required_work(scope, suffix='finish-succeeded')
    lease_owner = owner('finish')
    first = claim(module, work, lease_owner=lease_owner, now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='product_succeeded')

    stored = get_work(work)
    assert stored.disposition == WorkflowWorkDisposition.COMPLETE
    assert stored.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    assert stored.resolved_at is not None
    assert stored.execution_state == WorkflowWorkExecutionState.SETTLED
    assert stored.lease_owner == ''
    assert stored.lease_expires_at is None
    assert stored.heartbeat_at is None

    run = get_run(first.claim.workflow_run_id)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.finished_at == finish_at
    assert run.fencing_token == 1
    assert run.lease_owner == lease_owner


@pytest.mark.django_db
def test_finish_product_no_signal_settles_no_signal() -> None:
    module = _we()
    scope = create_scope('finish-no-signal')
    work = create_required_work(scope, suffix='finish-no-signal')
    first = claim(module, work, lease_owner=owner('finish'), now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='product_no_signal')

    stored = get_work(work)
    assert stored.disposition == WorkflowWorkDisposition.COMPLETE
    assert stored.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL
    assert stored.execution_state == WorkflowWorkExecutionState.SETTLED
    assert get_run(first.claim.workflow_run_id).status == WorkflowRunStatus.SUCCEEDED


@pytest.mark.django_db
def test_finish_continue_required_keeps_work_required_ready() -> None:
    module = _we()
    scope = create_scope('finish-continue')
    work = create_required_work(scope, suffix='finish-continue')
    first = claim(module, work, lease_owner=owner('finish'), now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='continue_required')

    stored = get_work(work)
    assert stored.disposition == WorkflowWorkDisposition.REQUIRED
    assert stored.execution_state == WorkflowWorkExecutionState.READY
    assert stored.lease_owner == ''
    assert stored.lease_expires_at is None
    assert stored.heartbeat_at is None
    assert get_run(first.claim.workflow_run_id).status == WorkflowRunStatus.SUCCEEDED


@pytest.mark.django_db
def test_continue_required_permits_queue_and_new_token() -> None:
    module = _we()
    dispatch = _wd()
    scope = create_scope('finish-queue')
    work = create_required_work(scope, suffix='finish-queue')
    first = claim(module, work, lease_owner=owner('finish'), now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='continue_required')
    new_run = dispatch.queue_work_attempt(
        work_id=work.id,
        now=finish_at,
        origin=WorkflowRunOrigin.RECONCILIATION,
    )

    assert new_run.id != first.claim.workflow_run_id
    assert new_run.status == WorkflowRunStatus.QUEUED

    next_owner = owner('next')
    second = claim(module, work, lease_owner=next_owner, now=NOW + timedelta(seconds=20), run_id=new_run.id)
    assert second.outcome == 'claimed'
    assert second.claim.fencing_token == 2


@pytest.mark.django_db
def test_finish_idempotent_for_matching_outcome() -> None:
    module = _we()
    scope = create_scope('finish-idempotent')
    work = create_required_work(scope, suffix='finish-idempotent')
    first = claim(module, work, lease_owner=owner('finish'), now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='product_succeeded')
    resolved_at = get_work(work).resolved_at

    module.finish_work_claim(claim=first.claim, now=NOW + timedelta(seconds=20), completion='product_succeeded')

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.SETTLED
    assert stored.resolved_at == resolved_at


@pytest.mark.django_db
def test_finish_mismatched_outcome_raises() -> None:
    module = _we()
    scope = create_scope('finish-mismatch')
    work = create_required_work(scope, suffix='finish-mismatch')
    first = claim(module, work, lease_owner=owner('finish'), now=NOW)

    finish_at = NOW + timedelta(seconds=10)
    module.lock_work_fence(claim=first.claim, now=finish_at)
    module.finish_work_claim(claim=first.claim, now=finish_at, completion='product_succeeded')

    with pytest.raises(ValueError):
        module.finish_work_claim(
            claim=first.claim,
            now=NOW + timedelta(seconds=20),
            completion='product_no_signal',
        )


RETRYING_FAILURES = (
    ('provider_transient', 'provider_timeout', 30),
    ('infrastructure_transient', 'database_unavailable', 30),
    ('unexpected', 'unexpected_exception', 300),
)


@pytest.mark.parametrize(('failure_class', 'code', 'expected_delay'), RETRYING_FAILURES)
@pytest.mark.django_db
def test_fail_retrying_class_schedules_backoff(failure_class: str, code: str, expected_delay: int) -> None:
    module = _we()
    scope = create_scope(f'fail-{code}')
    work = create_required_work(scope, suffix=f'fail-{code}')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class=failure_class, code=code),
    )

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert stored.next_retry_at == NOW + timedelta(seconds=expected_delay)
    assert stored.failure_streak == 1
    assert stored.disposition == WorkflowWorkDisposition.REQUIRED
    assert stored.lease_owner == ''

    run = get_run(first.claim.workflow_run_id)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == failure_class
    assert run.failure_code == code
    assert run.finished_at == NOW


@pytest.mark.django_db
def test_fail_worker_lost_zero_delay() -> None:
    module = _we()
    scope = create_scope('fail-worker-lost')
    work = create_required_work(scope, suffix='fail-worker-lost')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='worker_lost', code='lease_expired'),
    )

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert stored.next_retry_at == NOW
    assert stored.failure_streak == 1


@pytest.mark.django_db
def test_fail_configuration_blocks_with_fingerprint() -> None:
    module = _we()
    scope = create_scope('fail-config')
    work = create_required_work(scope, suffix='fail-config')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(
            failure_class='configuration',
            code='model_policy_unavailable',
            configuration_fingerprint=HEX_A,
        ),
    )

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert stored.blocked_configuration_fingerprint == HEX_A
    assert stored.next_retry_at is None
    assert stored.failure_streak == 1
    assert stored.lease_owner == ''

    run = get_run(first.claim.workflow_run_id)
    assert run.failure_class == 'configuration'
    assert run.configuration_fingerprint == HEX_A


@pytest.mark.django_db
def test_fail_invalid_input_is_terminal() -> None:
    module = _we()
    scope = create_scope('fail-invalid')
    work = create_required_work(scope, suffix='fail-invalid')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='invalid_input', code='work_contract_invalid'),
    )

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert stored.disposition == WorkflowWorkDisposition.REQUIRED
    assert stored.next_retry_at is None
    assert stored.failure_streak == 1
    assert get_run(first.claim.workflow_run_id).failure_class == 'invalid_input'


@pytest.mark.django_db
def test_fail_streak_increments_across_attempts() -> None:
    module = _we()
    scope = create_scope('fail-streak')
    work = create_required_work(scope, suffix='fail-streak')

    first = claim(module, work, lease_owner=owner('fail-a'), now=NOW)
    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    due_at = NOW + timedelta(seconds=30)
    second = claim(module, work, lease_owner=owner('fail-b'), now=due_at)
    module.fail_work_claim(
        claim=second.claim,
        now=due_at,
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    stored = get_work(work)
    assert stored.failure_streak == 2
    assert stored.next_retry_at == due_at + timedelta(seconds=60)


@pytest.mark.django_db
def test_fail_non_required_work_returns_to_settled() -> None:
    module = _we()
    scope = create_scope('fail-non-required')
    work = create_required_work(scope, suffix='fail-non-required')
    settle_work(module, work)

    manual = make_queued_run(work, origin=WorkflowRunOrigin.MANUAL, dispatched_at=NOW + timedelta(seconds=5))
    leased = claim(module, work, lease_owner=owner('manual'), now=NOW + timedelta(seconds=10), run_id=manual.id)
    module.fail_work_claim(
        claim=leased.claim,
        now=NOW + timedelta(seconds=20),
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    stored = get_work(work)
    assert stored.execution_state == WorkflowWorkExecutionState.SETTLED
    assert stored.disposition == WorkflowWorkDisposition.COMPLETE
    assert get_run(manual.id).status == WorkflowRunStatus.FAILED


@pytest.mark.django_db
def test_fail_idempotent_for_matching_outcome() -> None:
    module = _we()
    scope = create_scope('fail-idempotent')
    work = create_required_work(scope, suffix='fail-idempotent')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    failure = ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout')
    module.fail_work_claim(claim=first.claim, now=NOW, failure=failure)
    module.fail_work_claim(claim=first.claim, now=NOW + timedelta(seconds=5), failure=failure)

    stored = get_work(work)
    assert stored.failure_streak == 1
    assert stored.next_retry_at == NOW + timedelta(seconds=30)


@pytest.mark.django_db
def test_fail_mismatched_outcome_raises() -> None:
    module = _we()
    scope = create_scope('fail-mismatch')
    work = create_required_work(scope, suffix='fail-mismatch')
    first = claim(module, work, lease_owner=owner('fail'), now=NOW)

    module.fail_work_claim(
        claim=first.claim,
        now=NOW,
        failure=ClassifiedWorkFailure(failure_class='provider_transient', code='provider_timeout'),
    )

    with pytest.raises(ValueError):
        module.fail_work_claim(
            claim=first.claim,
            now=NOW + timedelta(seconds=5),
            failure=ClassifiedWorkFailure(failure_class='invalid_input', code='work_contract_invalid'),
        )


def run_two(first: object, second: object) -> tuple[dict[str, object], list[BaseException]]:
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(key: str, action: object) -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            value = action()
            with lock:
                results[key] = value
        except BaseException as error:
            with lock:
                errors.append(error)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=worker, args=('a', first)),
        threading.Thread(target=worker, args=('b', second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)

    return results, errors


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_claims_yield_one_claimed_one_busy() -> None:
    module = _we()
    scope = create_scope('concurrent-claim')
    work = create_required_work(scope, suffix='concurrent-claim')

    results, errors = run_two(
        lambda: claim(module, work, lease_owner=owner('a'), now=NOW),
        lambda: claim(module, work, lease_owner=owner('b'), now=NOW),
    )

    assert errors == []
    outcomes = {result.outcome for result in results.values()}
    assert outcomes == {'claimed', 'busy'}
    assert get_work(work).fencing_token == 1
    assert running_v1_count(work) == 1


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_reclaim_of_expired_lease_converges() -> None:
    module = _we()
    scope = create_scope('concurrent-reclaim')
    work = create_required_work(scope, suffix='concurrent-reclaim')
    first = claim(module, work, lease_owner=owner('lost'), now=NOW)

    reclaim_at = NOW + timedelta(seconds=200)
    results, errors = run_two(
        lambda: claim(module, work, lease_owner=owner('c'), now=reclaim_at),
        lambda: claim(module, work, lease_owner=owner('d'), now=reclaim_at),
    )

    assert errors == []
    outcomes = {result.outcome for result in results.values()}
    assert outcomes == {'claimed', 'busy'}
    assert get_work(work).fencing_token == 2
    assert running_v1_count(work) == 1
    assert get_run(first.claim.workflow_run_id).failure_class == 'worker_lost'


@pytest.mark.django_db
def test_execution_configuration_fingerprint_is_deterministic_hex() -> None:
    module = _we()
    scope = create_scope('fingerprint-hex')
    work = create_required_work(scope, suffix='fingerprint-hex')

    first = module.execution_configuration_fingerprint(work)
    second = module.execution_configuration_fingerprint(get_work(work))

    assert first == second
    assert len(first) == 64
    assert all(character in '0123456789abcdef' for character in first)


@pytest.mark.django_db
def test_execution_configuration_fingerprint_differs_across_scope() -> None:
    module = _we()
    first_work = create_required_work(create_scope('fingerprint-a'), suffix='fingerprint-a')
    second_work = create_required_work(create_scope('fingerprint-b'), suffix='fingerprint-b')

    assert module.execution_configuration_fingerprint(first_work) != module.execution_configuration_fingerprint(
        second_work
    )


@pytest.mark.django_db
def test_execution_configuration_fingerprint_ignores_execution_state() -> None:
    module = _we()
    scope = create_scope('fingerprint-state')
    work = create_required_work(scope, suffix='fingerprint-state')
    before = module.execution_configuration_fingerprint(work)

    claim(module, work, lease_owner=owner('state'), now=NOW)

    assert module.execution_configuration_fingerprint(get_work(work)) == before
