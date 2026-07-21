from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.transaction import TransactionManagementError

from engram.core.models import (
    OrganizationSettings,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.work_failures import (
    CONFIGURATION,
    INVALID_INPUT,
    WORKER_LOST,
    ClassifiedWorkFailure,
    retry_backoff,
)
from engram.memory.workflow_work import canonical_json_bytes, work_input_fingerprint
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ProviderSecretEnvelope
from engram.model_policy.services import ResolveModelPolicy, ResolveModelPolicyInput

_MAX_OWNER_LENGTH = 255
_LEASE_EXPIRED_CODE = 'lease_expired'
_DEFAULT_FAILURE_STREAK_LIMIT = 12


def _failure_streak_limit() -> int:
    return int(os.getenv('ENGRAM_WORK_FAILURE_STREAK_LIMIT', str(_DEFAULT_FAILURE_STREAK_LIMIT)))


_TASK_TYPE_BY_WORK = {
    WorkflowWorkType.OBSERVATION_PROCESSING: 'generation',
    WorkflowWorkType.SESSION_DISTILLATION: 'generation',
    WorkflowWorkType.DAILY_DIGEST: 'digest',
    WorkflowWorkType.WEEKLY_DIGEST: 'digest',
    WorkflowWorkType.MEMORY_EMBEDDING: 'embedding',
}

_TERMINAL_EXECUTION_STATES = frozenset(
    {
        WorkflowWorkExecutionState.SETTLED,
        WorkflowWorkExecutionState.TERMINAL_FAILURE,
    }
)

_LEASE_WORK_FIELDS = (
    'execution_state',
    'fencing_token',
    'lease_owner',
    'lease_expires_at',
    'heartbeat_at',
    'next_retry_at',
    'failure_streak',
    'blocked_configuration_fingerprint',
    'updated_at',
)


class StaleWorkFenceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WorkClaim:
    work_id: uuid.UUID
    workflow_run_id: uuid.UUID
    fencing_token: int
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: str
    claim: WorkClaim | None


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')

    return


def _require_owner(lease_owner: str) -> None:
    if not lease_owner or len(lease_owner) > _MAX_OWNER_LENGTH:
        raise ValueError('lease owner must be a non-blank bounded string')

    return


def _envelope_section(envelope: ProviderSecretEnvelope | None) -> dict[str, object]:
    if envelope is None:
        return {'status': 'unavailable'}

    return {
        'id': str(envelope.id),
        'version': envelope.version,
        'key_version': envelope.key_version,
        'updated_at': envelope.updated_at,
    }


def _unavailable_marker(
    work: WorkflowWork,
    task_type: str,
    error: ModelPolicyError | ProviderSecretError,
) -> dict[str, object]:
    code = getattr(error, 'code', '') or getattr(error, 'error_code', '')

    return {
        'status': 'unavailable',
        'error_type': type(error).__name__,
        'code': code,
        'organization_id': str(work.organization_id),
        'project_id': str(work.project_id),
        'team_id': str(work.team_id) if work.team_id else None,
        'task_type': task_type,
    }


def _policy_role_section(work: WorkflowWork, task_type: str) -> dict[str, object]:
    try:
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                task_type=task_type,
            )
        )
    except (ModelPolicyError, ProviderSecretError) as error:
        marker = _unavailable_marker(work, task_type, error)

        return {
            'model_policy': marker,
            'provider_secret': {'status': 'unavailable'},
            'envelope': {'status': 'unavailable'},
        }

    policy = resolved.policy
    secret = policy.secret
    envelope = ProviderSecretEnvelope.objects.filter(secret_id=secret.id, active=True).order_by('-version').first()

    return {
        'model_policy': {
            'id': str(policy.id),
            'version': policy.version,
            'provider': policy.provider,
            'model': policy.model,
            'updated_at': policy.updated_at,
        },
        'provider_secret': {
            'id': str(secret.id),
            'current_version': secret.current_version,
            'active': secret.active,
            'rotation_state': secret.rotation_state,
            'updated_at': secret.updated_at,
        },
        'envelope': _envelope_section(envelope),
    }


def _configuration_sections(work: WorkflowWork) -> dict[str, object]:
    return _policy_role_section(work, _TASK_TYPE_BY_WORK.get(work.work_type, ''))


def _settings_section(work: WorkflowWork) -> dict[str, object]:
    updated_at = (
        OrganizationSettings.objects.filter(organization_id=work.organization_id)
        .values_list('updated_at', flat=True)
        .first()
    )
    if updated_at is None:
        return {'status': 'unavailable'}

    return {'updated_at': updated_at}


_CANDIDATE_DECISION_POLICY_ROLES = ('curation', 'embedding', 'generation')


def _candidate_decision_enabled(work: WorkflowWork) -> bool:
    from engram.memory.curation import candidate_decision_enabled

    return candidate_decision_enabled(work)


def _distillation_settings_section() -> dict[str, object]:
    return {
        'chunk_char_budget': os.environ.get('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET'),
        'reduction_target': os.environ.get('ENGRAM_DISTILL_REDUCE_TARGET'),
        'max_provider_calls_per_attempt': os.environ.get('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT'),
    }


def _candidate_decision_fingerprint_payload(work: WorkflowWork) -> dict[str, object]:
    return {
        'schema': 'execution_configuration/v1',
        'work_type': work.work_type,
        'organization_id': str(work.organization_id),
        'project_id': str(work.project_id),
        'team_id': str(work.team_id) if work.team_id else None,
        'policy_roles': {role: _policy_role_section(work, role) for role in _CANDIDATE_DECISION_POLICY_ROLES},
        'candidate_decision_enabled': _candidate_decision_enabled(work),
        'organization_settings': _settings_section(work),
        'execution_contract_version': 1,
    }


def execution_configuration_fingerprint(work: WorkflowWork) -> str:
    if work.work_type == WorkflowWorkType.CANDIDATE_DECISION:
        return hashlib.sha256(canonical_json_bytes(_candidate_decision_fingerprint_payload(work))).hexdigest()

    sections = _configuration_sections(work)
    payload = {
        'schema': 'execution_configuration/v1',
        'work_type': work.work_type,
        'organization_id': str(work.organization_id),
        'project_id': str(work.project_id),
        'team_id': str(work.team_id) if work.team_id else None,
        'task_type': _TASK_TYPE_BY_WORK.get(work.work_type, ''),
        'model_policy': sections['model_policy'],
        'provider_secret': sections['provider_secret'],
        'envelope': sections['envelope'],
        'organization_settings': _settings_section(work),
        'execution_contract_version': 1,
    }
    if work.work_type == WorkflowWorkType.SESSION_DISTILLATION:
        payload['distillation_settings'] = _distillation_settings_section()

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _lock_work(work_id: uuid.UUID, expected_work_type: str) -> WorkflowWork:
    try:
        return WorkflowWork.objects.select_for_update().get(id=work_id, work_type=expected_work_type)
    except WorkflowWork.DoesNotExist as error:
        raise ValueError('workflow work does not match the expected type or scope') from error


def _revalidate_scope(work: WorkflowWork) -> None:
    if work.project.organization_id != work.organization_id:
        raise ValueError('workflow work project scope is invalid')

    if work.team_id is not None and work.team.organization_id != work.organization_id:
        raise ValueError('workflow work team scope is invalid')

    return


def fingerprint_matches(work: WorkflowWork) -> bool:
    try:
        fingerprint = work_input_fingerprint(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=work.input_snapshot,
        )
    except ValueError:
        return False

    return fingerprint == work.input_fingerprint


def _terminalize_fingerprint_mismatch(work: WorkflowWork, *, lease_owner: str, now: datetime) -> None:
    token = work.fencing_token + 1
    WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.FAILED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.AUTOMATIC,
        fencing_token=token,
        lease_owner=lease_owner,
        started_at=now,
        finished_at=now,
        failure_class=INVALID_INPUT,
        failure_code='work_fingerprint_mismatch',
        input_snapshot=work.input_snapshot,
    )
    work.execution_state = WorkflowWorkExecutionState.TERMINAL_FAILURE
    work.fencing_token = token
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.failure_streak = work.failure_streak + 1
    _clear_lease_fields(work)
    work.save(update_fields=list(_LEASE_WORK_FIELDS))

    return


def _handle_identity(work: WorkflowWork, *, lease_owner: str, now: datetime) -> ClaimResult | None:
    if fingerprint_matches(work):
        return None

    if work.disposition != WorkflowWorkDisposition.REQUIRED:
        raise ValueError('workflow work fingerprint does not match its immutable snapshot')

    _terminalize_fingerprint_mismatch(work, lease_owner=lease_owner, now=now)

    return ClaimResult(outcome='terminal', claim=None)


def _locked_v1_runs(work: WorkflowWork) -> list[WorkflowRun]:
    return list(
        WorkflowRun.objects.select_for_update()
        .filter(work_id=work.id, execution_contract_version=1)
        .order_by('created_at', 'id')
    )


def _running_run(runs: list[WorkflowRun]) -> WorkflowRun | None:
    for run in runs:
        if run.status == WorkflowRunStatus.RUNNING:
            return run

    return None


def _clear_lease_fields(work: WorkflowWork) -> None:
    work.lease_owner = ''
    work.lease_expires_at = None
    work.heartbeat_at = None

    return


def _fail_run_worker_lost(run: WorkflowRun, now: datetime) -> None:
    run.status = WorkflowRunStatus.FAILED
    run.finished_at = now
    run.failure_class = WORKER_LOST
    run.failure_code = _LEASE_EXPIRED_CODE
    run.save(
        update_fields=[
            'status',
            'finished_at',
            'failure_class',
            'failure_code',
            'updated_at',
        ]
    )

    return


def _build_automatic_run(
    work: WorkflowWork,
    *,
    token: int,
    lease_owner: str,
    now: datetime,
    lease_for: timedelta,
) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.RUNNING,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.AUTOMATIC,
        fencing_token=token,
        lease_owner=lease_owner,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + lease_for,
        input_snapshot=work.input_snapshot,
    )


def _lease_supplied_run(run: WorkflowRun, *, token: int, lease_owner: str, now: datetime, lease_for: timedelta) -> None:
    run.status = WorkflowRunStatus.RUNNING
    run.fencing_token = token
    run.lease_owner = lease_owner
    run.started_at = now
    run.heartbeat_at = now
    run.lease_expires_at = now + lease_for
    run.finished_at = None
    run.failure_class = ''
    run.failure_code = ''
    run.configuration_fingerprint = ''
    run.save()

    return


def _apply_lease(work: WorkflowWork, *, token: int, lease_owner: str, now: datetime, lease_for: timedelta) -> None:
    work.execution_state = WorkflowWorkExecutionState.LEASED
    work.fencing_token = token
    work.lease_owner = lease_owner
    work.lease_expires_at = now + lease_for
    work.heartbeat_at = now
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.save(update_fields=list(_LEASE_WORK_FIELDS))

    return


def _select_supplied_run(runs: list[WorkflowRun], workflow_run_id: uuid.UUID) -> WorkflowRun:
    for run in runs:
        if run.id == workflow_run_id:
            if run.status != WorkflowRunStatus.QUEUED:
                raise ValueError('supplied workflow run is not a queued v1 attempt')

            return run

    raise ValueError('supplied workflow run is not a queued v1 attempt for this work')


def _do_claim(
    work: WorkflowWork,
    supplied_run: WorkflowRun | None,
    *,
    lease_owner: str,
    now: datetime,
    lease_for: timedelta,
) -> ClaimResult:
    token = work.fencing_token + 1
    if supplied_run is None:
        run = _build_automatic_run(work, token=token, lease_owner=lease_owner, now=now, lease_for=lease_for)
    else:
        _lease_supplied_run(supplied_run, token=token, lease_owner=lease_owner, now=now, lease_for=lease_for)
        run = supplied_run

    _apply_lease(work, token=token, lease_owner=lease_owner, now=now, lease_for=lease_for)

    return ClaimResult(
        outcome='claimed',
        claim=WorkClaim(
            work_id=work.id,
            workflow_run_id=run.id,
            fencing_token=token,
            lease_owner=lease_owner,
            lease_expires_at=now + lease_for,
        ),
    )


def _short_circuit_state(work: WorkflowWork, *, now: datetime, absorb_terminal: bool) -> ClaimResult | None:
    state = work.execution_state

    if state in _TERMINAL_EXECUTION_STATES and absorb_terminal:
        return ClaimResult(outcome='terminal', claim=None)

    if state == WorkflowWorkExecutionState.RETRY_WAIT and now < work.next_retry_at:
        return ClaimResult(outcome='not_due', claim=None)

    if state == WorkflowWorkExecutionState.BLOCKED:
        current_fingerprint = execution_configuration_fingerprint(work)
        if current_fingerprint == work.blocked_configuration_fingerprint:
            return ClaimResult(outcome='blocked', claim=None)

        work.failure_streak = 0
        work.blocked_configuration_fingerprint = ''

    return None


def _handle_leased_state(
    work: WorkflowWork,
    runs: list[WorkflowRun],
    *,
    now: datetime,
    lease_owner: str,
    workflow_run_id: uuid.UUID | None,
) -> ClaimResult | None:
    running = _running_run(runs)
    if now < work.lease_expires_at:
        if running is not None and workflow_run_id == running.id and lease_owner == work.lease_owner:
            return ClaimResult(
                outcome='replayed',
                claim=WorkClaim(
                    work_id=work.id,
                    workflow_run_id=running.id,
                    fencing_token=work.fencing_token,
                    lease_owner=work.lease_owner,
                    lease_expires_at=work.lease_expires_at,
                ),
            )

        return ClaimResult(outcome='busy', claim=None)

    if running is not None:
        _fail_run_worker_lost(running, now)

    return None


def _resolve_supplied_run(
    runs: list[WorkflowRun],
    workflow_run_id: uuid.UUID | None,
    *,
    is_automatic: bool,
) -> tuple[WorkflowRun | None, ValueError | None]:
    if is_automatic:
        return None, None

    try:
        return _select_supplied_run(runs, workflow_run_id), None
    except ValueError as error:
        return None, error


def _absorbs_redelivered_terminal_run(
    work: WorkflowWork,
    runs: list[WorkflowRun],
    workflow_run_id: uuid.UUID | None,
) -> bool:
    if work.execution_state not in _TERMINAL_EXECUTION_STATES:
        return False

    for run in runs:
        if run.id == workflow_run_id:
            return run.status in (WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED)

    return False


def claim_work(
    *,
    work_id: uuid.UUID,
    expected_work_type: str,
    lease_owner: str,
    now: datetime,
    lease_for: timedelta,
    workflow_run_id: uuid.UUID | None = None,
) -> ClaimResult:
    _require_aware(now)
    _require_owner(lease_owner)

    with transaction.atomic():
        work = _lock_work(work_id, expected_work_type)
        _revalidate_scope(work)
        runs = _locked_v1_runs(work)
        is_automatic = workflow_run_id is None

        supplied_run, pending_error = _resolve_supplied_run(runs, workflow_run_id, is_automatic=is_automatic)
        absorb_terminal = is_automatic or (supplied_run is not None and supplied_run.origin != WorkflowRunOrigin.MANUAL)

        if pending_error is not None and _absorbs_redelivered_terminal_run(work, runs, workflow_run_id):
            return ClaimResult(outcome='terminal', claim=None)

        short_circuit = _short_circuit_state(work, now=now, absorb_terminal=absorb_terminal)
        if short_circuit is not None:
            return short_circuit

        identity = _handle_identity(work, lease_owner=lease_owner, now=now)
        if identity is not None:
            return identity

        if work.execution_state == WorkflowWorkExecutionState.LEASED:
            leased = _handle_leased_state(
                work,
                runs,
                now=now,
                lease_owner=lease_owner,
                workflow_run_id=workflow_run_id,
            )
            if leased is not None:
                return leased

        if pending_error is None:
            return _do_claim(work, supplied_run, lease_owner=lease_owner, now=now, lease_for=lease_for)

    raise pending_error


def _lock_and_verify(claim: WorkClaim, now: datetime) -> tuple[WorkflowWork, WorkflowRun]:
    try:
        work = WorkflowWork.objects.select_for_update().get(id=claim.work_id)
    except WorkflowWork.DoesNotExist as error:
        raise StaleWorkFenceError('workflow work no longer exists') from error

    if (
        work.execution_state != WorkflowWorkExecutionState.LEASED
        or work.fencing_token != claim.fencing_token
        or work.lease_owner != claim.lease_owner
        or work.lease_expires_at is None
        or now >= work.lease_expires_at
    ):
        raise StaleWorkFenceError('workflow work lease no longer matches the claim')

    try:
        run = WorkflowRun.objects.select_for_update().get(
            id=claim.workflow_run_id,
            work_id=claim.work_id,
            execution_contract_version=1,
        )
    except WorkflowRun.DoesNotExist as error:
        raise StaleWorkFenceError('workflow run no longer matches the claim') from error

    if (
        run.status != WorkflowRunStatus.RUNNING
        or run.fencing_token != claim.fencing_token
        or run.lease_owner != claim.lease_owner
    ):
        raise StaleWorkFenceError('workflow run no longer matches the claim')

    return work, run


def heartbeat_work(*, claim: WorkClaim, now: datetime, lease_for: timedelta) -> WorkClaim:
    _require_aware(now)
    with transaction.atomic():
        work, run = _lock_and_verify(claim, now)
        expires_at = now + lease_for
        work.lease_expires_at = expires_at
        work.heartbeat_at = now
        work.save(update_fields=['lease_expires_at', 'heartbeat_at', 'updated_at'])
        run.lease_expires_at = expires_at
        run.heartbeat_at = now
        run.save(update_fields=['lease_expires_at', 'heartbeat_at', 'updated_at'])

    return WorkClaim(
        work_id=claim.work_id,
        workflow_run_id=claim.workflow_run_id,
        fencing_token=claim.fencing_token,
        lease_owner=claim.lease_owner,
        lease_expires_at=expires_at,
    )


def lock_work_fence(*, claim: WorkClaim, now: datetime) -> tuple[WorkflowWork, WorkflowRun]:
    if not transaction.get_connection().in_atomic_block:
        raise TransactionManagementError('lock_work_fence requires an active transaction')

    return _lock_and_verify(claim, now)


def _lock_claim_rows(claim: WorkClaim) -> tuple[WorkflowWork, WorkflowRun]:
    work = WorkflowWork.objects.select_for_update().get(id=claim.work_id)
    run = WorkflowRun.objects.select_for_update().get(
        id=claim.workflow_run_id,
        work_id=claim.work_id,
        execution_contract_version=1,
    )

    return work, run


def _require_claim_fence(work: WorkflowWork, run: WorkflowRun, claim: WorkClaim, now: datetime) -> None:
    if (
        run.fencing_token != claim.fencing_token
        or run.lease_owner != claim.lease_owner
        or work.fencing_token != claim.fencing_token
        or work.lease_owner != claim.lease_owner
        or work.execution_state != WorkflowWorkExecutionState.LEASED
        or work.lease_expires_at is None
        or now >= work.lease_expires_at
    ):
        raise StaleWorkFenceError('workflow run no longer matches the claim')

    return


def _recorded_completion(work: WorkflowWork) -> str:
    if (
        work.execution_state == WorkflowWorkExecutionState.SETTLED
        and work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    ):
        return 'product_succeeded'

    if work.execution_state == WorkflowWorkExecutionState.SETTLED and work.resolution_reason in (
        WorkflowWorkResolutionReason.NO_SIGNAL,
        WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
    ):
        return 'product_no_signal'

    return 'continue_required'


def _settle_work(work: WorkflowWork, *, reason: str, now: datetime) -> None:
    work.disposition = WorkflowWorkDisposition.COMPLETE
    work.resolution_reason = reason
    work.resolved_at = now
    work.execution_state = WorkflowWorkExecutionState.SETTLED
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.failure_streak = 0
    _clear_lease_fields(work)
    work.save(
        update_fields=[
            'disposition',
            'resolution_reason',
            'resolved_at',
            'execution_state',
            'lease_owner',
            'lease_expires_at',
            'heartbeat_at',
            'next_retry_at',
            'blocked_configuration_fingerprint',
            'failure_streak',
            'updated_at',
        ]
    )

    return


def _apply_finish(
    work: WorkflowWork,
    run: WorkflowRun,
    *,
    now: datetime,
    completion: str,
    result_memory_id: uuid.UUID | None,
    resolution_reason: str | None,
) -> None:
    run.status = WorkflowRunStatus.SUCCEEDED
    run.finished_at = now
    if result_memory_id is not None:
        run.result_memory_id = result_memory_id
    run.save(update_fields=['status', 'finished_at', 'result_memory', 'updated_at'])

    if completion == 'continue_required':
        work.execution_state = WorkflowWorkExecutionState.READY
        work.next_retry_at = None
        work.blocked_configuration_fingerprint = ''
        _clear_lease_fields(work)
        work.save(
            update_fields=[
                'execution_state',
                'lease_owner',
                'lease_expires_at',
                'heartbeat_at',
                'next_retry_at',
                'blocked_configuration_fingerprint',
                'updated_at',
            ]
        )

        return

    if completion == 'product_succeeded':
        if resolution_reason is not None:
            raise ValueError('product_succeeded does not accept a resolution reason override')
        reason = WorkflowWorkResolutionReason.SUCCEEDED
    else:
        reason = resolution_reason or WorkflowWorkResolutionReason.NO_SIGNAL
        if reason not in (
            WorkflowWorkResolutionReason.NO_SIGNAL,
            WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
        ):
            raise ValueError('product_no_signal has an unsupported resolution reason')
    _settle_work(work, reason=reason, now=now)

    return


def _settle_lease_preserving_resolution(work: WorkflowWork) -> None:
    work.execution_state = WorkflowWorkExecutionState.SETTLED
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.failure_streak = 0
    _clear_lease_fields(work)
    work.save(update_fields=list(_LEASE_WORK_FIELDS))

    return


def finish_work_claim(
    *,
    claim: WorkClaim,
    now: datetime,
    completion: str,
    result_memory_id: uuid.UUID | None = None,
    resolution_reason: str | None = None,
) -> None:
    _require_aware(now)
    if completion not in ('product_succeeded', 'product_no_signal', 'continue_required'):
        raise ValueError(f'unsupported completion {completion!r}')
    if completion == 'continue_required' and resolution_reason is not None:
        raise ValueError('continue_required does not accept a resolution reason override')

    with transaction.atomic():
        work, run = _lock_claim_rows(claim)

        if run.status == WorkflowRunStatus.SUCCEEDED:
            if _recorded_completion(work) != completion:
                raise ValueError('workflow run already completed with a different outcome')
            if resolution_reason is not None and work.resolution_reason != resolution_reason:
                raise ValueError('workflow run already completed with a different resolution reason')

            return

        if run.status != WorkflowRunStatus.RUNNING:
            raise ValueError('workflow run is not in a completable state')

        _require_claim_fence(work, run, claim, now)
        _apply_finish(
            work,
            run,
            now=now,
            completion=completion,
            result_memory_id=result_memory_id,
            resolution_reason=resolution_reason,
        )

    return


def finish_claim_resolved_elsewhere(*, claim: WorkClaim, now: datetime) -> None:
    _require_aware(now)
    with transaction.atomic():
        work, run = _lock_claim_rows(claim)

        if run.status == WorkflowRunStatus.SUCCEEDED:
            return

        if run.status != WorkflowRunStatus.RUNNING:
            raise ValueError('workflow run is not in a completable state')

        _require_claim_fence(work, run, claim, now)

        run.status = WorkflowRunStatus.SUCCEEDED
        run.finished_at = now
        run.save(update_fields=['status', 'finished_at', 'updated_at'])
        _settle_lease_preserving_resolution(work)

    return


def _apply_failure_run(run: WorkflowRun, *, now: datetime, failure: ClassifiedWorkFailure) -> None:
    run.status = WorkflowRunStatus.FAILED
    run.finished_at = now
    run.failure_class = failure.failure_class
    run.failure_code = failure.code
    run.failure_reason = failure.redacted_detail
    run.configuration_fingerprint = failure.configuration_fingerprint
    run.save(
        update_fields=[
            'status',
            'finished_at',
            'failure_class',
            'failure_code',
            'failure_reason',
            'configuration_fingerprint',
            'updated_at',
        ]
    )

    return


def _apply_failure_work(work: WorkflowWork, *, now: datetime, failure: ClassifiedWorkFailure) -> None:
    if work.disposition != WorkflowWorkDisposition.REQUIRED:
        work.execution_state = WorkflowWorkExecutionState.SETTLED
        work.next_retry_at = None
        work.blocked_configuration_fingerprint = ''
        _clear_lease_fields(work)
        work.save(update_fields=list(_LEASE_WORK_FIELDS))

        return

    streak = work.failure_streak + 1
    _clear_lease_fields(work)

    if failure.failure_class == CONFIGURATION:
        work.execution_state = WorkflowWorkExecutionState.BLOCKED
        work.blocked_configuration_fingerprint = failure.configuration_fingerprint
        work.next_retry_at = None
    elif failure.failure_class == INVALID_INPUT:
        work.execution_state = WorkflowWorkExecutionState.TERMINAL_FAILURE
        work.blocked_configuration_fingerprint = ''
        work.next_retry_at = None
    elif streak >= _failure_streak_limit():
        work.execution_state = WorkflowWorkExecutionState.TERMINAL_FAILURE
        work.blocked_configuration_fingerprint = ''
        work.next_retry_at = None
    else:
        work.execution_state = WorkflowWorkExecutionState.RETRY_WAIT
        work.blocked_configuration_fingerprint = ''
        work.next_retry_at = now + retry_backoff(
            failure_class=failure.failure_class,
            failure_streak=streak,
        )

    work.failure_streak = streak
    work.save(update_fields=list(_LEASE_WORK_FIELDS))

    return


def fail_work_claim(*, claim: WorkClaim, now: datetime, failure: ClassifiedWorkFailure) -> None:
    _require_aware(now)
    with transaction.atomic():
        work, run = _lock_claim_rows(claim)

        if run.status == WorkflowRunStatus.FAILED:
            if run.failure_class == failure.failure_class and run.failure_code == failure.code:
                return

            raise ValueError('workflow run already failed with a different outcome')

        if run.status != WorkflowRunStatus.RUNNING:
            raise ValueError('workflow run is not in a failable state')

        _require_claim_fence(work, run, claim, now)
        _apply_failure_run(run, now=now, failure=failure)
        _apply_failure_work(work, now=now, failure=failure)

    return
