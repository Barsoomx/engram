from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import structlog
from django.db import transaction
from django.utils import timezone

from engram.celery_app import app
from engram.context.services import ReembedMissingEmbeddings
from engram.core.models import (
    Memory,
    MemoryStatus,
    Observation,
    Project,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.candidate_ttl import ExpireStaleCandidates
from engram.memory.confidence_decay import DecayMemoryConfidence
from engram.memory.distillation import (
    DistillSession,
    DistillSessionInput,
    DistillSessionResult,
    run_session_distillation_with_tracking,
)
from engram.memory.distillation_reconciler import RetryFailedDistillations
from engram.memory.services import (
    WEEKLY_DIGEST_WINDOW_DAYS,
    MemoryCandidateWorkerInput,
    MemoryCandidateWorkerResult,
    MemoryWorkerError,
    ProcessObservationRecorded,
    run_daily_digest_with_tracking,
    run_weekly_digest_with_tracking,
)
from engram.memory.session_sweep import SweepStaleSessions
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


def dispatch_work_task(
    task: object,
    work_id: uuid.UUID,
    workflow_run_id: uuid.UUID | None = None,
) -> object:
    args = (str(work_id),)
    task_id = f'workflow-work:{work_id}'
    if workflow_run_id is not None:
        args = (*args, str(workflow_run_id))
        task_id = f'{task_id}:run:{workflow_run_id}'

    return task.apply_async(args=args, task_id=task_id)


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
    result_memory_id: uuid.UUID | None,
) -> None:
    completed = WorkflowRun.objects.filter(
        id=workflow_run.id,
        status=WorkflowRunStatus.RUNNING,
    ).update(
        status=WorkflowRunStatus.SUCCEEDED,
        result_memory_id=result_memory_id,
        finished_at=timezone.now(),
    )
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


def _finalize_observation_work(
    work: WorkflowWork,
    workflow_run: WorkflowRun | None,
    result: MemoryCandidateWorkerResult,
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

    if workflow_run is not None:
        _complete_workflow_run(
            workflow_run,
            result_memory_id=result.memory.id if result.memory is not None else None,
        )


def _prepare_versioned_work(
    *,
    work_id: object,
    workflow_run_id: object,
    expected_work_type: str,
    allow_succeeded_run: bool = False,
) -> tuple[WorkflowWork, WorkflowRun | None, bool]:
    parsed_work_id, parsed_run_id = _parse_work_task_ids(work_id, workflow_run_id)
    work = _load_versioned_work(parsed_work_id, expected_work_type)
    workflow_run = (
        _load_workflow_run(
            work,
            parsed_run_id,
            allow_succeeded=allow_succeeded_run,
        )
        if parsed_run_id is not None
        else None
    )
    automatic_terminal = workflow_run is None and work.disposition != WorkflowWorkDisposition.REQUIRED

    return work, workflow_run, automatic_terminal


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
        fingerprint = work_input_fingerprint(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=work.input_snapshot,
        )
    except ValueError as error:
        raise MemoryWorkerError('workflow work fingerprint is invalid') from error
    if fingerprint != work.input_fingerprint:
        raise MemoryWorkerError('workflow work fingerprint does not match frozen input')

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
        raise MemoryWorkerError('workflow work fingerprint is invalid') from error
    if fingerprint != work.input_fingerprint:
        raise MemoryWorkerError('workflow work fingerprint does not match frozen input')

    return work.input_snapshot['upper_sequence_inclusive']


def _acquire_session_distill_run(
    work: WorkflowWork,
    *,
    request_id: str,
    correlation_id: str,
) -> tuple[WorkflowRun, bool]:
    with transaction.atomic():
        WorkflowWork.objects.select_for_update().get(
            id=work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )
        existing = (
            WorkflowRun.objects.filter(
                work_id=work.id,
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                run_type=work.work_type,
            )
            .order_by('created_at')
            .first()
        )
        if existing is not None:
            return existing, False

        run = WorkflowRun.objects.create(
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
            work_id=work.id,
            run_type=work.work_type,
            status=WorkflowRunStatus.RUNNING,
            started_at=timezone.now(),
            input_snapshot={'session_id': work.input_snapshot['session_id']},
            request_id=request_id,
            correlation_id=correlation_id,
        )

        return run, True


def _complete_session_distill_run(
    workflow_run: WorkflowRun,
    result: DistillSessionResult,
    *,
    escalation: bool,
) -> None:
    fields: dict[str, object] = {
        'status': WorkflowRunStatus.SUCCEEDED,
        'finished_at': timezone.now(),
        'provider_call_ids': list(result.provider_call_ids),
    }
    if result.auto_promoted:
        fields['result_memory_id'] = result.auto_promoted[0].id
    if escalation:
        fields['escalation'] = True

    updated = WorkflowRun.objects.filter(id=workflow_run.id, status=WorkflowRunStatus.RUNNING).update(**fields)
    if updated == 1:
        return
    if WorkflowRun.objects.filter(id=workflow_run.id, status=WorkflowRunStatus.SUCCEEDED).exists():
        return

    raise MemoryWorkerError('workflow run is no longer running')


def _finalize_session_distill_work(
    work: WorkflowWork,
    workflow_run: WorkflowRun,
    result: DistillSessionResult,
) -> None:
    if not result.truncated and work.disposition == WorkflowWorkDisposition.REQUIRED:
        resolver = (
            resolve_work_succeeded if result.auto_promoted or result.queued_for_review else resolve_work_no_signal
        )
        resolver(
            work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )

    _complete_session_distill_run(workflow_run, result, escalation=result.truncated)


def _run_unfinished_versioned_work(
    *,
    work_id: object,
    workflow_run_id: object,
    expected_work_type: str,
) -> str:
    work, _workflow_run, automatic_terminal = _prepare_versioned_work(
        work_id=work_id,
        workflow_run_id=workflow_run_id,
        expected_work_type=expected_work_type,
    )
    if automatic_terminal:
        return str(work.id)

    raise MemoryWorkerError(f'{expected_work_type} work adapter is not implemented')


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
    work, workflow_run, automatic_terminal = _prepare_versioned_work(
        work_id=work_id,
        workflow_run_id=workflow_run_id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        allow_succeeded_run=True,
    )
    if automatic_terminal:
        return str(work.id)
    if workflow_run is not None:
        duplicate_via = 'load'
        if workflow_run.status == WorkflowRunStatus.QUEUED:
            workflow_run = _claim_workflow_run(work, workflow_run)
            duplicate_via = 'claim_cas_loss'
        if workflow_run.status == WorkflowRunStatus.SUCCEEDED:
            return _succeeded_workflow_run_result(
                work,
                workflow_run,
                via=duplicate_via,
            )
        if workflow_run.status != WorkflowRunStatus.RUNNING:
            raise MemoryWorkerError('workflow run claim returned invalid status')

    structlog.contextvars.clear_contextvars()
    try:
        observation = _load_observation_work_subject(work)
        result = ProcessObservationRecorded().execute(
            MemoryCandidateWorkerInput(observation_id=observation.id),
        )
        _finalize_observation_work(work, workflow_run, result)
    except MemoryWorkerError as exc:
        can_retry = exc.retryable and self.request.retries < self.max_retries
        _record_workflow_run_error(workflow_run, exc, requeue=can_retry)
        if can_retry:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    except Exception as error:
        _record_workflow_run_error(workflow_run, error, requeue=False)
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(workflow_run.id if workflow_run is not None else work.id)


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
        parsed_workflow_run_id = uuid.UUID(str(workflow_run_id)) if workflow_run_id is not None else None
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed session id') from error

    correlation_id = f'distill-session:{parsed_session_id}'
    request_id = f'{correlation_id}:{uuid.uuid4().hex[:8]}'
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        request_id=request_id,
    )
    try:
        result = run_session_distillation_with_tracking(
            session_id=parsed_session_id,
            request_id=request_id,
            correlation_id=correlation_id,
            existing_run_id=parsed_workflow_run_id,
        )
    except MemoryWorkerError as exc:
        if exc.retryable:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(result.session.id)


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
    work, explicit_run, automatic_terminal = _prepare_versioned_work(
        work_id=work_id,
        workflow_run_id=workflow_run_id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
    )
    if automatic_terminal:
        return str(work.id)

    upper = _load_session_work_upper(work)
    session_id = uuid.UUID(work.input_snapshot['session_id'])
    correlation_id = f'distill-session:{session_id}'
    request_id = f'{correlation_id}:{uuid.uuid4().hex[:8]}'

    if explicit_run is not None:
        workflow_run = _claim_workflow_run(work, explicit_run)
        if workflow_run.status == WorkflowRunStatus.SUCCEEDED:
            return _succeeded_workflow_run_result(work, workflow_run, via='claim')
        if workflow_run.status != WorkflowRunStatus.RUNNING:
            raise MemoryWorkerError('workflow run claim returned invalid status')
    else:
        workflow_run, created = _acquire_session_distill_run(
            work,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        if not created:
            logger.info(
                'distill_session_work_initial_run_adopted',
                work_id=str(work.id),
                workflow_run_id=str(workflow_run.id),
                status=workflow_run.status,
            )

            return str(workflow_run.id)

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id,
        request_id=request_id,
    )
    try:
        result = DistillSession().execute(
            DistillSessionInput(
                session_id=session_id,
                upper_sequence_inclusive=upper,
                request_id=request_id,
                correlation_id=correlation_id,
                run_id=str(workflow_run.id),
            ),
        )
        _finalize_session_distill_work(work, workflow_run, result)
    except MemoryWorkerError as exc:
        can_retry = exc.retryable and self.request.retries < self.max_retries
        _record_workflow_run_error(workflow_run, exc, requeue=can_retry)
        if can_retry:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    except Exception as error:
        _record_workflow_run_error(workflow_run, error, requeue=False)
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(workflow_run.id)


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


@app.task(
    bind=True,
    name='engram.memory.generate_daily_digest_work_v1',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_daily_digest_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    return _run_unfinished_versioned_work(
        work_id=work_id,
        workflow_run_id=workflow_run_id,
        expected_work_type=WorkflowWorkType.DAILY_DIGEST,
    )


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
)
def generate_weekly_digest_work_v1(
    self: object,
    work_id: object,
    workflow_run_id: object = None,
) -> str:
    return _run_unfinished_versioned_work(
        work_id=work_id,
        workflow_run_id=workflow_run_id,
        expected_work_type=WorkflowWorkType.WEEKLY_DIGEST,
    )


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
    enqueued_projects = 0
    enqueued_tasks = 0

    weekly_window_start = timezone.now() - timedelta(days=WEEKLY_DIGEST_WINDOW_DAYS)

    for project in Project.objects.all():
        has_approved = (
            Memory.objects.filter(
                organization_id=project.organization_id,
                project=project,
                status=MemoryStatus.APPROVED,
                updated_at__gte=weekly_window_start,
            )
            .exclude(kind='digest')
            .exists()
        )
        if not has_approved:
            continue

        generate_weekly_digest.delay(
            str(project.organization_id),
            str(project.id),
        )
        enqueued_projects += 1
        enqueued_tasks += 1

    return {
        'enqueued_projects': enqueued_projects,
        'enqueued_tasks': enqueued_tasks,
    }


@app.task(name='engram.memory.run_scheduled_digests')
def run_scheduled_digests() -> dict[str, int]:
    enqueued_projects = 0
    enqueued_tasks = 0

    for project in Project.objects.all():
        memory_ids = recent_approved_memory_ids(project)
        if not memory_ids:
            continue

        generate_daily_digest.delay(
            str(project.organization_id),
            str(project.id),
            [str(value) for value in memory_ids],
        )
        enqueued_projects += 1
        enqueued_tasks += 1

    return {
        'enqueued_projects': enqueued_projects,
        'enqueued_tasks': enqueued_tasks,
    }


@app.task(name='engram.memory.sweep_stale_sessions')
def sweep_stale_sessions() -> dict[str, int]:
    result = SweepStaleSessions().execute()

    return {
        'swept': len(result.ended_session_ids),
        'distilled': len(result.distillable_session_ids),
    }


@app.task(name='engram.memory.retry_failed_distillations')
def retry_failed_distillations() -> dict[str, int]:
    result = RetryFailedDistillations().execute()

    for session_id in result.retriable_session_ids:
        distill_session.delay(str(session_id))

    return {
        'retried': len(result.retriable_session_ids),
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


@app.task(name='engram.memory.expire_stale_candidates')
def expire_stale_candidates() -> dict[str, int]:
    result = ExpireStaleCandidates().execute()

    logger.info(
        'expire_stale_candidates_completed',
        scanned=result.scanned,
        rejected=result.rejected,
    )

    return {'scanned': result.scanned, 'rejected': result.rejected}


def _daily_digest_window_days() -> int:
    return int(os.environ.get('ENGRAM_DAILY_DIGEST_WINDOW_DAYS', '1'))


def _daily_digest_max_window_days() -> int:
    return int(os.environ.get('ENGRAM_DAILY_DIGEST_MAX_WINDOW_DAYS', '7'))


def daily_digest_window_start(project: Project, now: datetime | None = None) -> datetime:
    now = now or timezone.now()
    floor_start = now - timedelta(days=_daily_digest_max_window_days())
    last_success = (
        WorkflowRun.objects.filter(
            organization_id=project.organization_id,
            project=project,
            run_type=WorkflowRunType.DAILY_DIGEST,
            status=WorkflowRunStatus.SUCCEEDED,
            finished_at__isnull=False,
        )
        .order_by('-finished_at')
        .values_list('finished_at', flat=True)
        .first()
    )
    if last_success is not None:
        candidate = last_success
    else:
        candidate = now - timedelta(days=_daily_digest_window_days())

    return max(candidate, floor_start)


def recent_approved_memory_ids(project: Project) -> list[uuid.UUID]:
    window_start = daily_digest_window_start(project)

    return list(
        Memory.objects.filter(
            organization_id=project.organization_id,
            project=project,
            status=MemoryStatus.APPROVED,
            updated_at__gte=window_start,
        )
        .exclude(kind='digest')
        .values_list('id', flat=True),
    )
