from __future__ import annotations

import os
import socket
import uuid
from datetime import timedelta

import structlog
from django.db import transaction
from django.utils import timezone

from engram.celery_app import app
from engram.context.services import ReembedMissingEmbeddings
from engram.core.models import (
    AgentSession,
    Observation,
    RetrievalDocument,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.candidate_ttl import ExpireStaleCandidates
from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork
from engram.memory.confidence_decay import DecayMemoryConfidence
from engram.memory.curation import DecideMemoryCandidate, candidate_decision_enabled
from engram.memory.distillation import (
    DistillationStageError,
    run_complete_distillation_attempt,
)
from engram.memory.distillation_reconciler import RetryFailedDistillations
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
    run_daily_digest_with_tracking,
    run_weekly_digest_with_tracking,
)
from engram.memory.session_sweep import SweepStaleSessions
from engram.memory.session_work_reconciler import reconcile_scheduled_session_work
from engram.memory.work_dispatch import queue_work_attempt, work_task_signature
from engram.memory.work_execution import (
    claim_work,
    execution_configuration_fingerprint,
    fail_work_claim,
    finish_work_claim,
    heartbeat_work,
    lock_work_fence,
)
from engram.memory.work_failures import CONFIGURATION, ClassifiedWorkFailure, translate_failure
from engram.memory.workflow_work import (
    observation_content_digest,
    resolve_work_no_signal,
    resolve_work_succeeded,
    work_input_fingerprint,
)

logger = structlog.get_logger(__name__)

_RETRY_BACKOFF_BASE = 5
_MAX_RETRIES = 3
_OBSERVATION_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_OBSERVATION_SOFT_TIME_LIMIT', '60'))
_OBSERVATION_TIME_LIMIT = int(os.environ.get('ENGRAM_OBSERVATION_TIME_LIMIT', '90'))
_DISTILL_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_DISTILL_SOFT_TIME_LIMIT', '600'))
_DISTILL_TIME_LIMIT = int(os.environ.get('ENGRAM_DISTILL_TIME_LIMIT', '660'))
_DECAY_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_DECAY_SOFT_TIME_LIMIT', '600'))
_DECAY_TIME_LIMIT = int(os.environ.get('ENGRAM_DECAY_TIME_LIMIT', '660'))
_DIGEST_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_DIGEST_SOFT_TIME_LIMIT', '180'))
_DIGEST_TIME_LIMIT = int(os.environ.get('ENGRAM_DIGEST_TIME_LIMIT', '210'))
_EMBEDDING_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_EMBEDDING_SOFT_TIME_LIMIT', '180'))
_EMBEDDING_TIME_LIMIT = int(os.environ.get('ENGRAM_EMBEDDING_TIME_LIMIT', '210'))

_OBSERVATION_LEASE = timedelta(seconds=120)
_SESSION_LEASE = timedelta(seconds=720)
_DIGEST_LEASE = timedelta(seconds=240)
_CANDIDATE_DECISION_LEASE = timedelta(seconds=120)
_EMBEDDING_LEASE = timedelta(seconds=300)
_LEASE_OWNER_MAX = 255
_NON_EXECUTING_CLAIM_OUTCOMES = frozenset({'terminal', 'busy', 'not_due', 'blocked'})

LEASE_BY_WORK_TYPE = {
    WorkflowWorkType.OBSERVATION_PROCESSING: _OBSERVATION_LEASE,
    WorkflowWorkType.SESSION_DISTILLATION: _SESSION_LEASE,
    WorkflowWorkType.DAILY_DIGEST: _DIGEST_LEASE,
    WorkflowWorkType.WEEKLY_DIGEST: _DIGEST_LEASE,
    WorkflowWorkType.CANDIDATE_DECISION: _CANDIDATE_DECISION_LEASE,
    WorkflowWorkType.MEMORY_EMBEDDING: _EMBEDDING_LEASE,
}


def dispatch_work_task(
    task: object,
    work_id: uuid.UUID,
    workflow_run_id: uuid.UUID | None = None,
) -> object:
    args, task_id = work_task_signature(work_id, workflow_run_id)

    return task.apply_async(args=tuple(args), task_id=task_id)


def _parse_work_task_ids(
    work_id: object,
    workflow_run_id: object,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    try:
        parsed_work_id = uuid.UUID(str(work_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed work id') from error

    if workflow_run_id is None:
        return parsed_work_id, None

    try:
        parsed_run_id = uuid.UUID(str(workflow_run_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed workflow run id') from error

    return parsed_work_id, parsed_run_id


def _load_versioned_work(work_id: uuid.UUID, expected_work_type: str) -> WorkflowWork:
    try:
        work = WorkflowWork.objects.select_related('project', 'team').get(id=work_id)
    except WorkflowWork.DoesNotExist as error:
        raise MemoryWorkerError('workflow work not found') from error

    if work.contract_version != 1 or work.work_type != expected_work_type:
        raise MemoryWorkerError('workflow work type or contract version does not match task')
    if work.project.organization_id != work.organization_id:
        raise MemoryWorkerError('workflow work project scope is invalid')
    if work.team_id is not None and work.team.organization_id != work.organization_id:
        raise MemoryWorkerError('workflow work team scope is invalid')

    return work


def _load_workflow_run(
    work: WorkflowWork,
    workflow_run_id: uuid.UUID,
    *,
    allow_succeeded: bool,
) -> WorkflowRun:
    allowed_statuses = [WorkflowRunStatus.QUEUED]
    if allow_succeeded:
        allowed_statuses.append(WorkflowRunStatus.SUCCEEDED)

    try:
        return WorkflowRun.objects.get(
            id=workflow_run_id,
            work_id=work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
            run_type=work.work_type,
            status__in=allowed_statuses,
        )
    except WorkflowRun.DoesNotExist as error:
        raise MemoryWorkerError('workflow run does not match queued work scope') from error


def _claim_workflow_run(work: WorkflowWork, workflow_run: WorkflowRun) -> WorkflowRun:
    with transaction.atomic():
        claimed = WorkflowRun.objects.filter(
            id=workflow_run.id,
            work_id=work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
            run_type=work.work_type,
            status=WorkflowRunStatus.QUEUED,
        ).update(
            status=WorkflowRunStatus.RUNNING,
            started_at=timezone.now(),
            finished_at=None,
            failure_reason='',
        )
    if claimed != 1:
        try:
            return WorkflowRun.objects.get(
                id=workflow_run.id,
                work_id=work.id,
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                run_type=work.work_type,
                status=WorkflowRunStatus.SUCCEEDED,
            )
        except WorkflowRun.DoesNotExist as error:
            raise MemoryWorkerError('workflow run is no longer queued') from error

    workflow_run.refresh_from_db()

    return workflow_run


def _succeeded_workflow_run_result(
    work: WorkflowWork,
    workflow_run: WorkflowRun,
    *,
    via: str,
) -> str:
    disposition = (
        WorkflowWork.objects.filter(
            id=work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )
        .values_list('disposition', flat=True)
        .get()
    )
    if disposition != WorkflowWorkDisposition.COMPLETE:
        raise MemoryWorkerError('succeeded workflow run has non-complete work')

    logger.info(
        'workflow_run_duplicate_delivery_absorbed',
        work_id=str(work.id),
        workflow_run_id=str(workflow_run.id),
        via=via,
    )

    return str(workflow_run.id)


def _requeue_workflow_run(workflow_run: WorkflowRun) -> None:
    requeued = WorkflowRun.objects.filter(
        id=workflow_run.id,
        status=WorkflowRunStatus.RUNNING,
    ).update(
        status=WorkflowRunStatus.QUEUED,
        finished_at=None,
        failure_reason='',
    )
    if requeued != 1:
        raise MemoryWorkerError('workflow run is no longer running')


def _fail_workflow_run(workflow_run: WorkflowRun, error: Exception) -> None:
    WorkflowRun.objects.filter(
        id=workflow_run.id,
        status=WorkflowRunStatus.RUNNING,
    ).update(
        status=WorkflowRunStatus.FAILED,
        failure_reason=str(error)[:1024],
        finished_at=timezone.now(),
    )


def _complete_workflow_run(
    workflow_run: WorkflowRun,
    *,
    result_memory_id: uuid.UUID | None = None,
    provider_call_ids: list[str] | None = None,
    escalation: bool = False,
) -> None:
    fields: dict[str, object] = {
        'status': WorkflowRunStatus.SUCCEEDED,
        'result_memory_id': result_memory_id,
        'finished_at': timezone.now(),
    }
    if provider_call_ids is not None:
        fields['provider_call_ids'] = provider_call_ids
    if escalation:
        fields['escalation'] = True

    completed = WorkflowRun.objects.filter(
        id=workflow_run.id,
        status=WorkflowRunStatus.RUNNING,
    ).update(**fields)
    if completed == 1:
        return

    if WorkflowRun.objects.filter(id=workflow_run.id, status=WorkflowRunStatus.SUCCEEDED).exists():
        return

    raise MemoryWorkerError('workflow run is no longer running')


def _record_workflow_run_error(
    workflow_run: WorkflowRun | None,
    error: Exception,
    *,
    requeue: bool,
) -> None:
    if workflow_run is None:
        return
    if requeue:
        _requeue_workflow_run(workflow_run)
    else:
        _fail_workflow_run(workflow_run, error)


def _lease_owner(task: object) -> str:
    hostname = getattr(getattr(task, 'request', None), 'hostname', None) or socket.gethostname()
    hostname = hostname.replace(':', '_')
    tail = f':{os.getpid()}:{uuid.uuid4()}'
    max_hostname = _LEASE_OWNER_MAX - len(tail)

    return f'{hostname[:max_hostname]}{tail}'


def _root_cause_error(error: Exception) -> BaseException:
    if isinstance(error, MemoryWorkerError) and not error.code and error.__cause__ is not None:
        return error.__cause__

    return error


def _record_claim_failure(
    claim: object,
    error: Exception,
    *,
    configuration_fingerprint: str,
) -> None:
    failure = (
        error.failure
        if isinstance(error, DistillationStageError)
        else translate_failure(
            _root_cause_error(error),
            configuration_fingerprint=configuration_fingerprint,
        )
    )
    fail_work_claim(claim=claim, now=timezone.now(), failure=failure)

    return


def _run_fenced_automatic(
    task: object,
    work: WorkflowWork,
    *,
    work_type: str,
    lease_for: timedelta,
    event_prefix: str,
    execute: object,
    workflow_run_id: uuid.UUID | None = None,
) -> str:
    claim_result = claim_work(
        work_id=work.id,
        expected_work_type=work_type,
        lease_owner=_lease_owner(task),
        now=timezone.now(),
        lease_for=lease_for,
        workflow_run_id=workflow_run_id,
    )
    if claim_result.outcome in _NON_EXECUTING_CLAIM_OUTCOMES:
        logger.info(
            f'{event_prefix}_claim_skipped',
            work_id=str(work.id),
            outcome=claim_result.outcome,
        )

        return str(work.id)

    claim = claim_result.claim
    configuration_fingerprint = execution_configuration_fingerprint(work)
    try:
        return execute(claim)
    except Exception as error:
        _record_claim_failure(claim, error, configuration_fingerprint=configuration_fingerprint)

        raise


def _finalize_observation_work(
    work: WorkflowWork,
    workflow_run: WorkflowRun,
    result: object,
) -> None:
    if work.disposition == WorkflowWorkDisposition.REQUIRED:
        resolver = (
            resolve_work_succeeded
            if result.memory is not None or result.candidate is not None
            else resolve_work_no_signal
        )
        resolver(
            work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )

    _complete_workflow_run(
        workflow_run,
        result_memory_id=result.memory.id if result.memory is not None else None,
    )

    return


def _verify_work_fingerprint(work: WorkflowWork) -> None:
    try:
        fingerprint = work_input_fingerprint(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=work.input_snapshot,
        )
    except ValueError as error:
        raise MemoryWorkerError('workflow work fingerprint is invalid', code='work_contract_invalid') from error

    if fingerprint != work.input_fingerprint:
        raise MemoryWorkerError(
            'workflow work fingerprint does not match frozen input',
            code='work_fingerprint_mismatch',
        )


def _load_observation_work_subject(work: WorkflowWork) -> Observation:
    if work.subject_type != WorkflowSubjectType.OBSERVATION:
        raise MemoryWorkerError('workflow work subject type does not match observation task')

    try:
        observation = Observation.objects.get(
            id=work.subject_id,
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
        )
    except Observation.DoesNotExist as error:
        raise MemoryWorkerError('observation is outside workflow work scope') from error

    try:
        current_digest = observation_content_digest(observation)
    except ValueError as error:
        raise MemoryWorkerError('observation digest cannot be recomputed') from error
    if current_digest != work.input_snapshot['observation_digest']:
        raise MemoryWorkerError('observation digest does not match frozen input')

    return observation


def _load_session_work_upper(work: WorkflowWork) -> int:
    if work.subject_type != WorkflowSubjectType.AGENT_SESSION:
        raise MemoryWorkerError('workflow work subject type does not match session task')

    return work.input_snapshot['upper_sequence_inclusive']


def _load_embedding_work_document(work: WorkflowWork) -> RetrievalDocument:
    if work.subject_type != WorkflowSubjectType.RETRIEVAL_DOCUMENT:
        raise MemoryWorkerError(
            'workflow work subject type does not match embedding task', code='work_contract_invalid'
        )
    try:
        document = RetrievalDocument.objects.select_related('memory', 'memory_version').get(
            id=work.subject_id,
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
        )
    except RetrievalDocument.DoesNotExist as error:
        raise MemoryWorkerError(
            'retrieval document is outside workflow work scope', code='work_scope_invalid'
        ) from error
    snapshot = work.input_snapshot
    if snapshot.get('retrieval_document_id') != str(document.id) or snapshot.get('memory_id') != str(
        document.memory_id
    ):
        raise MemoryWorkerError('embedding work snapshot does not match document', code='work_fingerprint_mismatch')
    if snapshot.get('memory_version_id') != str(document.memory_version_id):
        raise MemoryWorkerError(
            'embedding work snapshot version does not match document', code='work_fingerprint_mismatch'
        )
    return document


@app.task(
    bind=True,
    name='engram.memory.process_observation_recorded',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_OBSERVATION_SOFT_TIME_LIMIT,
    time_limit=_OBSERVATION_TIME_LIMIT,
)
def process_observation_recorded(self: object, observation_id: object) -> str:
    try:
        parsed_observation_id = uuid.UUID(observation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed observation id') from error

    structlog.contextvars.clear_contextvars()
    try:
        result = ProcessObservationRecorded().execute(
            MemoryCandidateWorkerInput(observation_id=parsed_observation_id),
        )
    except MemoryWorkerError as exc:
        if exc.retryable:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    if result.memory is not None:
        return str(result.memory.id)

    if result.candidate is None:
        return 'skipped'

    return str(result.candidate.id)


@app.task(
    bind=True,
    name='engram.memory.process_observation_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_OBSERVATION_SOFT_TIME_LIMIT,
    time_limit=_OBSERVATION_TIME_LIMIT,
)
def process_observation_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.OBSERVATION_PROCESSING)

    if parsed_run_id is not None:
        return _run_observation_explicit_delivery(self, work, parsed_run_id)

    return _run_observation_automatic_delivery(self, work)


def _run_observation_explicit_delivery(
    task: object,
    work: WorkflowWork,
    workflow_run_id: uuid.UUID,
) -> str:
    workflow_run = _load_workflow_run(work, workflow_run_id, allow_succeeded=True)
    duplicate_via = 'load'
    if workflow_run.status == WorkflowRunStatus.QUEUED:
        workflow_run = _claim_workflow_run(work, workflow_run)
        duplicate_via = 'claim_cas_loss'
    if workflow_run.status == WorkflowRunStatus.SUCCEEDED:
        return _succeeded_workflow_run_result(work, workflow_run, via=duplicate_via)
    if workflow_run.status != WorkflowRunStatus.RUNNING:
        raise MemoryWorkerError('workflow run claim returned invalid status')

    structlog.contextvars.clear_contextvars()
    try:
        _verify_work_fingerprint(work)
        observation = _load_observation_work_subject(work)
        result = ProcessObservationRecorded().execute(
            MemoryCandidateWorkerInput(observation_id=observation.id),
        )
        _finalize_observation_work(work, workflow_run, result)
    except MemoryWorkerError as exc:
        can_retry = exc.retryable and task.request.retries < task.max_retries
        _record_workflow_run_error(workflow_run, exc, requeue=can_retry)
        if can_retry:
            countdown = _RETRY_BACKOFF_BASE ** (task.request.retries + 1)
            raise task.retry(exc=exc, countdown=countdown) from None
        raise
    except Exception as error:
        _record_workflow_run_error(workflow_run, error, requeue=False)
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(workflow_run.id)


def _run_observation_automatic_delivery(task: object, work: WorkflowWork) -> str:
    observation = _load_observation_work_subject(work)

    if work.disposition != WorkflowWorkDisposition.REQUIRED:
        logger.info(
            'observation_work_already_resolved',
            work_id=str(work.id),
            disposition=work.disposition,
        )

        return str(work.id)

    def execute(claim: object) -> str:
        structlog.contextvars.clear_contextvars()
        try:
            result = ProcessObservationRecorded().execute(
                MemoryCandidateWorkerInput(observation_id=observation.id),
            )
        finally:
            structlog.contextvars.clear_contextvars()

        completion = (
            'product_succeeded' if result.memory is not None or result.candidate is not None else 'product_no_signal'
        )
        result_memory_id = result.memory.id if result.memory is not None else None
        now = timezone.now()
        with transaction.atomic():
            lock_work_fence(claim=claim, now=now)
            finish_work_claim(
                claim=claim,
                now=now,
                completion=completion,
                result_memory_id=result_memory_id,
            )

        return str(claim.workflow_run_id)

    return _run_fenced_automatic(
        task,
        work,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_for=_OBSERVATION_LEASE,
        event_prefix='observation_work',
        execute=execute,
    )


@app.task(
    bind=True,
    name='engram.memory.process_candidate_decision_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_candidate_decision_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.CANDIDATE_DECISION)

    def execute(claim: object) -> str:
        _verify_work_fingerprint(work)
        if not candidate_decision_enabled(work):
            failure = ClassifiedWorkFailure(
                failure_class=CONFIGURATION,
                code='rollout_not_enabled',
                redacted_detail='candidate decision rollout not enabled',
                configuration_fingerprint=execution_configuration_fingerprint(work),
            )
            fail_work_claim(claim=claim, now=timezone.now(), failure=failure)

            return str(claim.workflow_run_id)

        DecideMemoryCandidate().execute(work=work, claim=claim)

        return str(claim.workflow_run_id)

    return _run_fenced_automatic(
        self,
        work,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        lease_for=_CANDIDATE_DECISION_LEASE,
        event_prefix='candidate_decision_work',
        execute=execute,
        workflow_run_id=parsed_run_id,
    )


@app.task(
    bind=True,
    name='engram.memory.embed_memory_projection_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_EMBEDDING_SOFT_TIME_LIMIT,
    time_limit=_EMBEDDING_TIME_LIMIT,
)
def embed_memory_projection_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    from engram.memory.projections import complete_embedding_projection
    from engram.model_policy.services import (
        EmbeddingCallInput,
        ResolveModelPolicy,
        ResolveModelPolicyInput,
        get_provider_gateway,
    )

    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.MEMORY_EMBEDDING)
    document = _load_embedding_work_document(work)

    def execute(claim: object) -> str:
        _verify_work_fingerprint(work)
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                task_type='embedding',
            )
        )
        result = get_provider_gateway(resolved.policy).embed(
            EmbeddingCallInput(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                policy=resolved.policy,
                request_id=f'memory-embedding:{document.id}:{claim.workflow_run_id}',
                trace_id=f'memory-embedding:{document.id}',
                text=document.full_text,
            )
        )
        complete_embedding_projection(
            claim=claim,
            expected_projection_hash=work.input_snapshot['exact_projection_hash'],
            embedding=result.embedding,
            provider_call_id=result.call_record_id,
            now=timezone.now(),
        )

        return str(claim.workflow_run_id)

    return _run_fenced_automatic(
        self,
        work,
        work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        lease_for=_EMBEDDING_LEASE,
        event_prefix='memory_embedding_work',
        execute=execute,
        workflow_run_id=parsed_run_id,
    )


@app.task(
    bind=True,
    name='engram.memory.distill_session',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_DISTILL_SOFT_TIME_LIMIT,
    time_limit=_DISTILL_TIME_LIMIT,
)
def distill_session(self: object, session_id: object, workflow_run_id: object = None) -> str:
    try:
        parsed_session_id = uuid.UUID(str(session_id))
        if workflow_run_id is not None:
            uuid.UUID(str(workflow_run_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed session id') from error

    try:
        session = AgentSession.objects.get(
            id=parsed_session_id,
            end_work_contract_version=1,
        )
    except AgentSession.DoesNotExist as error:
        raise MemoryWorkerError(
            'legacy distillation delivery has no versioned session owner',
            code='legacy_distillation_work_missing',
        ) from error
    work = (
        WorkflowWork.objects.filter(
            organization_id=session.organization_id,
            project_id=session.project_id,
            team_id=session.team_id,
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
            subject_type=WorkflowSubjectType.AGENT_SESSION,
            subject_id=session.id,
            contract_version=1,
            input_snapshot__schema='session_distillation_input/v1',
            input_snapshot__session_id=str(session.id),
        )
        .order_by('-created_at', '-id')
        .first()
    )
    if work is None:
        raise MemoryWorkerError(
            'legacy distillation delivery has no versioned session work',
            code='legacy_distillation_work_missing',
        )
    if (
        work.disposition == WorkflowWorkDisposition.REQUIRED
        and work.execution_state == WorkflowWorkExecutionState.READY
    ):
        queue_work_attempt(
            work_id=work.id,
            now=timezone.now(),
            origin=WorkflowRunOrigin.RECONCILIATION,
        )

    return str(work.id)


@app.task(
    bind=True,
    name='engram.memory.distill_session_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_DISTILL_SOFT_TIME_LIMIT,
    time_limit=_DISTILL_TIME_LIMIT,
)
def distill_session_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.SESSION_DISTILLATION)
    _load_session_work_upper(work)
    session_id = uuid.UUID(work.input_snapshot['session_id'])

    if parsed_run_id is None:
        if work.disposition != WorkflowWorkDisposition.REQUIRED:
            logger.info(
                'distill_session_work_already_resolved',
                work_id=str(work.id),
                disposition=work.disposition,
            )

            return str(work.id)

    correlation_id = f'distill-session:{session_id}'
    request_id = f'{correlation_id}:{uuid.uuid4().hex[:8]}'

    def execute(claim: object) -> str:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
        )
        try:
            attempt_now = timezone.now()
            claim = heartbeat_work(claim=claim, now=attempt_now, lease_for=_SESSION_LEASE)
            run_complete_distillation_attempt(
                work=work,
                claim=claim,
                now=attempt_now,
            )
        finally:
            structlog.contextvars.clear_contextvars()

        return str(claim.workflow_run_id)

    return _run_fenced_automatic(
        self,
        work,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_for=_SESSION_LEASE,
        event_prefix='distill_session_work',
        execute=execute,
        workflow_run_id=parsed_run_id,
    )


@app.task(
    bind=True,
    name='engram.memory.generate_daily_digest',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_daily_digest(
    self: object,
    organization_id: object,
    project_id: object,
    memory_ids: list[str],
    workflow_run_id: object = None,
) -> str:
    try:
        parsed_organization_id = uuid.UUID(str(organization_id))
        parsed_project_id = uuid.UUID(project_id)
        parsed_memory_ids = tuple(uuid.UUID(value) for value in memory_ids)
        parsed_workflow_run_id = uuid.UUID(str(workflow_run_id)) if workflow_run_id is not None else None
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed daily digest input') from error

    request_id = f'daily-digest:{parsed_project_id}'
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=request_id,
        request_id=request_id,
    )
    try:
        result = run_daily_digest_with_tracking(
            organization_id=parsed_organization_id,
            project_id=parsed_project_id,
            memory_ids=parsed_memory_ids,
            request_id=request_id,
            correlation_id=request_id,
            existing_run_id=parsed_workflow_run_id,
        )
    except MemoryWorkerError as exc:
        if exc.retryable:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(result.memory.id)


def _run_digest_explicit_delivery(task: object, work: WorkflowWork, workflow_run_id: uuid.UUID) -> str:
    from engram.memory.digest_work import execute_frozen_digest_work

    workflow_run = _load_workflow_run(work, workflow_run_id, allow_succeeded=True)
    duplicate_via = 'load'
    if workflow_run.status == WorkflowRunStatus.QUEUED:
        workflow_run = _claim_workflow_run(work, workflow_run)
        duplicate_via = 'claim_cas_loss'
    if workflow_run.status == WorkflowRunStatus.SUCCEEDED:
        return _succeeded_workflow_run_result(work, workflow_run, via=duplicate_via)
    if workflow_run.status != WorkflowRunStatus.RUNNING:
        raise MemoryWorkerError('workflow run claim returned invalid status')

    structlog.contextvars.clear_contextvars()
    try:
        result_memory_id = execute_frozen_digest_work(work, workflow_run)
        _complete_workflow_run(workflow_run, result_memory_id=result_memory_id)
    except MemoryWorkerError as exc:
        can_retry = exc.retryable and task.request.retries < task.max_retries
        _record_workflow_run_error(workflow_run, exc, requeue=can_retry)
        if can_retry:
            countdown = _RETRY_BACKOFF_BASE ** (task.request.retries + 1)
            raise task.retry(exc=exc, countdown=countdown) from None
        raise
    except Exception as error:
        _record_workflow_run_error(workflow_run, error, requeue=False)
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(workflow_run.id)


def _run_digest_automatic_delivery(task: object, work: WorkflowWork, expected_work_type: str) -> str:
    from engram.memory.digest_work import execute_frozen_digest_work

    if work.disposition != WorkflowWorkDisposition.REQUIRED:
        logger.info(
            'digest_work_already_resolved',
            work_id=str(work.id),
            disposition=work.disposition,
        )

        return str(work.id)

    def execute(claim: object) -> str:
        structlog.contextvars.clear_contextvars()
        try:
            if work.work_type == WorkflowWorkType.DAILY_DIGEST:
                heartbeat_work(claim=claim, now=timezone.now(), lease_for=_DIGEST_LEASE)
            execute_frozen_digest_work(work, None, claim)
        finally:
            structlog.contextvars.clear_contextvars()

        return str(claim.workflow_run_id)

    return _run_fenced_automatic(
        task,
        work,
        work_type=expected_work_type,
        lease_for=_DIGEST_LEASE,
        event_prefix='digest_work',
        execute=execute,
    )


@app.task(
    bind=True,
    name='engram.memory.generate_daily_digest_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_DIGEST_SOFT_TIME_LIMIT,
    time_limit=_DIGEST_TIME_LIMIT,
)
def generate_daily_digest_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.DAILY_DIGEST)

    if parsed_run_id is not None:
        return _run_digest_explicit_delivery(self, work, parsed_run_id)

    return _run_digest_automatic_delivery(self, work, WorkflowWorkType.DAILY_DIGEST)


@app.task(
    bind=True,
    name='engram.memory.generate_weekly_digest',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_weekly_digest(
    self: object,
    organization_id: object,
    project_id: object,
    workflow_run_id: object = None,
) -> str:
    try:
        parsed_organization_id = uuid.UUID(str(organization_id))
        parsed_project_id = uuid.UUID(str(project_id))
        parsed_workflow_run_id = uuid.UUID(str(workflow_run_id)) if workflow_run_id is not None else None
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed weekly digest input') from error

    request_id = f'weekly-digest:{parsed_project_id}'
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=request_id,
        request_id=request_id,
    )
    try:
        result = run_weekly_digest_with_tracking(
            organization_id=parsed_organization_id,
            project_id=parsed_project_id,
            request_id=request_id,
            correlation_id=request_id,
            existing_run_id=parsed_workflow_run_id,
        )
    except MemoryWorkerError as exc:
        if exc.retryable:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(result.digest_memory.id)


@app.task(
    bind=True,
    name='engram.memory.generate_weekly_digest_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_DIGEST_SOFT_TIME_LIMIT,
    time_limit=_DIGEST_TIME_LIMIT,
)
def generate_weekly_digest_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, WorkflowWorkType.WEEKLY_DIGEST)

    if parsed_run_id is not None:
        return _run_digest_explicit_delivery(self, work, parsed_run_id)

    return _run_digest_automatic_delivery(self, work, WorkflowWorkType.WEEKLY_DIGEST)


@app.task(name='engram.memory.reembed_missing_embeddings')
def reembed_missing_embeddings() -> dict[str, int]:
    result = ReembedMissingEmbeddings().execute()
    logger.info(
        'reembed_missing_embeddings_completed',
        scanned=result.scanned,
        embedded=result.embedded,
        failed=result.failed,
    )

    return {'scanned': result.scanned, 'embedded': result.embedded, 'failed': result.failed}


@app.task(name='engram.memory.run_scheduled_weekly_digests')
def run_scheduled_weekly_digests() -> dict[str, int]:
    from engram.memory.digest_scheduler import run_weekly_schedule

    return run_weekly_schedule(as_of=timezone.now())


@app.task(name='engram.memory.run_scheduled_digests')
def run_scheduled_digests() -> dict[str, int]:
    from engram.memory.digest_scheduler import run_daily_schedule

    return run_daily_schedule(as_of=timezone.now())


@app.task(name='engram.memory.sweep_stale_sessions')
def sweep_stale_sessions() -> dict[str, int]:
    result = SweepStaleSessions().execute()

    return {
        'swept': len(result.ended_session_ids),
        'distilled': len(result.distillable_session_ids),
    }


@app.task(name='engram.memory.retry_failed_distillations')
def retry_failed_distillations() -> dict[str, int]:
    legacy = RetryFailedDistillations().execute()
    as_of = timezone.now()
    reconciled = reconcile_scheduled_session_work(as_of=as_of)

    return {
        'retried': len(legacy.retried),
        'reconciled': reconciled,
        'unlinked': len(legacy.unlinked_run_ids),
    }


@app.task(
    name='engram.memory.decay_memory_confidence',
    soft_time_limit=_DECAY_SOFT_TIME_LIMIT,
    time_limit=_DECAY_TIME_LIMIT,
)
def decay_memory_confidence() -> dict[str, int]:
    result = DecayMemoryConfidence().execute()

    logger.info(
        'confidence_decay_completed',
        organizations=result.organizations,
        projects=result.projects,
        memories=result.memories,
    )

    return {
        'organizations': result.organizations,
        'projects': result.projects,
        'memories': result.memories,
    }


@app.task(name='engram.memory.reconcile_candidate_decision_work')
def reconcile_candidate_decision_work() -> dict[str, int]:
    result = ReconcileCandidateDecisionWork().execute(as_of=timezone.now())

    return {'scanned': result.scanned, 'queued': result.queued}


@app.task(name='engram.memory.expire_stale_candidates')
def expire_stale_candidates() -> dict[str, int]:
    result = ExpireStaleCandidates().execute()

    logger.info(
        'expire_stale_candidates_completed',
        scanned=result.scanned,
        rejected=result.rejected,
    )

    return {'scanned': result.scanned, 'rejected': result.rejected}
