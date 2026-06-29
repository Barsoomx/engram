from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from engram.celery_app import app
from engram.core.models import Memory, MemoryStatus, Project
from engram.memory.services import (
    DAILY_DIGEST_WINDOW_DAYS,
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
    run_daily_digest_with_tracking,
)


@app.task(name='engram.memory.process_observation_recorded')
def process_observation_recorded(observation_id: object) -> str:
    try:
        parsed_observation_id = uuid.UUID(observation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed observation id') from error

    result = ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(observation_id=parsed_observation_id),
    )

    return str(result.memory.id)


@app.task(name='engram.memory.generate_daily_digest')
def generate_daily_digest(
    organization_id: object,
    project_id: object,
    memory_ids: list[str],
) -> str:
    try:
        parsed_organization_id = uuid.UUID(str(organization_id))
        parsed_project_id = uuid.UUID(project_id)
        parsed_memory_ids = tuple(uuid.UUID(value) for value in memory_ids)
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed daily digest input') from error

    request_id = f'daily-digest:{parsed_project_id}'

    result = run_daily_digest_with_tracking(
        organization_id=parsed_organization_id,
        project_id=parsed_project_id,
        memory_ids=parsed_memory_ids,
        request_id=request_id,
    )

    return str(result.memory.id)


@app.task(name='engram.memory.run_scheduled_digests')
def run_scheduled_digests() -> dict[str, int]:
    enqueued_projects = 0
    enqueued_tasks = 0

    for project in Project.objects.all():
        memory_ids = _recent_approved_memory_ids(project)
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


def _recent_approved_memory_ids(project: Project) -> list[uuid.UUID]:
    window_start = timezone.now() - timedelta(days=DAILY_DIGEST_WINDOW_DAYS)

    return list(
        Memory.objects.filter(
            organization_id=project.organization_id,
            project=project,
            status=MemoryStatus.APPROVED,
            updated_at__gte=window_start,
        )
        .exclude(metadata__kind='digest')
        .values_list('id', flat=True),
    )
