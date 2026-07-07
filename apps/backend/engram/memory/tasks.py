from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import structlog
from django.utils import timezone

from engram.celery_app import app
from engram.context.services import ReembedMissingEmbeddings
from engram.core.models import (
    Memory,
    MemoryStatus,
    Project,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.candidate_ttl import ExpireStaleCandidates
from engram.memory.confidence_decay import DecayMemoryConfidence
from engram.memory.distillation import run_session_distillation_with_tracking
from engram.memory.distillation_reconciler import RetryFailedDistillations
from engram.memory.services import (
    WEEKLY_DIGEST_WINDOW_DAYS,
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
    run_daily_digest_with_tracking,
    run_weekly_digest_with_tracking,
)
from engram.memory.session_sweep import SweepStaleSessions

logger = structlog.get_logger(__name__)

_RETRY_BACKOFF_BASE = 5
_MAX_RETRIES = 3
_OBSERVATION_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_OBSERVATION_SOFT_TIME_LIMIT', '60'))
_OBSERVATION_TIME_LIMIT = int(os.environ.get('ENGRAM_OBSERVATION_TIME_LIMIT', '90'))
_DISTILL_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_DISTILL_SOFT_TIME_LIMIT', '600'))
_DISTILL_TIME_LIMIT = int(os.environ.get('ENGRAM_DISTILL_TIME_LIMIT', '660'))
_DECAY_SOFT_TIME_LIMIT = int(os.environ.get('ENGRAM_DECAY_SOFT_TIME_LIMIT', '600'))
_DECAY_TIME_LIMIT = int(os.environ.get('ENGRAM_DECAY_TIME_LIMIT', '660'))


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
    name='engram.memory.distill_session',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=_DISTILL_SOFT_TIME_LIMIT,
    time_limit=_DISTILL_TIME_LIMIT,
)
def distill_session(self: object, session_id: object) -> str:
    try:
        parsed_session_id = uuid.UUID(str(session_id))
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
    name='engram.memory.generate_weekly_digest',
    max_retries=_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_weekly_digest(
    self: object,
    organization_id: object,
    project_id: object,
) -> str:
    try:
        parsed_organization_id = uuid.UUID(str(organization_id))
        parsed_project_id = uuid.UUID(str(project_id))
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
        )
    except MemoryWorkerError as exc:
        if exc.retryable:
            countdown = _RETRY_BACKOFF_BASE ** (self.request.retries + 1)
            raise self.retry(exc=exc, countdown=countdown) from None
        raise
    finally:
        structlog.contextvars.clear_contextvars()

    return str(result.digest_memory.id)


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

    for session_id in result.distillable_session_ids:
        distill_session.delay(str(session_id))

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
