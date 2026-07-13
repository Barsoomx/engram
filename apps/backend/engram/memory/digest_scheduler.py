from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from django.db import Error as DatabaseError
from django.db import transaction

from engram.core.models import (
    Project,
    WorkflowRun,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.digest_work import (
    create_digest_work_and_signal,
    freeze_daily_digest_input,
    freeze_weekly_digest_input,
)
from engram.memory.tasks import (
    generate_daily_digest_work_v1,
    generate_weekly_digest_work_v1,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    WorkflowWorkCollisionError,
    WorkflowWorkScopeError,
)

logger = structlog.get_logger(__name__)

_DAILY_CUT_HOUR = 2
_WEEKLY_CUT_HOUR = 3


@dataclass(frozen=True, slots=True)
class DigestBucket:
    work_type: str
    schedule_key: str
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    work_id: UUID
    created: bool
    disposition: str
    source_count: int
    task_enqueued: bool


def daily_window_days_default() -> int:
    return int(os.environ.get('ENGRAM_DAILY_DIGEST_WINDOW_DAYS', '1'))


def daily_window_days_max() -> int:
    return int(os.environ.get('ENGRAM_DAILY_DIGEST_MAX_WINDOW_DAYS', '7'))


def digest_max_sources() -> int:
    return int(os.environ.get('ENGRAM_DIGEST_MAX_SOURCES', '200'))


def _to_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError('as_of must be a datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('as_of must be timezone-aware')

    return value.astimezone(UTC)


def _daily_cut(moment: datetime, schedule_hour: int) -> datetime:
    candidate = moment.replace(hour=schedule_hour, minute=0, second=0, microsecond=0)
    if candidate > moment:
        candidate -= timedelta(days=1)

    return candidate


def _weekly_cut(moment: datetime, schedule_hour: int) -> datetime:
    candidate = moment.replace(hour=schedule_hour, minute=0, second=0, microsecond=0) - timedelta(days=moment.weekday())
    if candidate > moment:
        candidate -= timedelta(days=7)

    return candidate


def daily_bucket(*, as_of: datetime, window_days: int = 1, schedule_hour: int = _DAILY_CUT_HOUR) -> DigestBucket:
    moment = _to_utc(as_of)
    window_end = _daily_cut(moment, schedule_hour)
    window_start = window_end - timedelta(days=window_days)

    return DigestBucket(
        work_type=WorkflowWorkType.DAILY_DIGEST,
        schedule_key=f'daily:{window_end.date().isoformat()}',
        window_start=window_start,
        window_end=window_end,
    )


def weekly_bucket(*, as_of: datetime, schedule_hour: int = _WEEKLY_CUT_HOUR) -> DigestBucket:
    moment = _to_utc(as_of)
    window_end = _weekly_cut(moment, schedule_hour)
    window_start = window_end - timedelta(days=7)
    year, week, _weekday = window_start.isocalendar()

    return DigestBucket(
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        schedule_key=f'weekly:{year:04d}-W{week:02d}',
        window_start=window_start,
        window_end=window_end,
    )


def _resolve_project(project_id: UUID) -> Project:
    try:
        return Project.objects.get(id=project_id)
    except Project.DoesNotExist as error:
        raise WorkflowWorkScopeError('project is outside the digest scheduling scope') from error


def _resolve_run(
    project: Project,
    work_type: str,
    team_id: UUID | None,
    workflow_run_id: UUID,
) -> WorkflowRun:
    try:
        return WorkflowRun.objects.get(
            id=workflow_run_id,
            organization_id=project.organization_id,
            project_id=project.id,
            team_id=team_id,
            run_type=work_type,
        )
    except WorkflowRun.DoesNotExist as error:
        raise WorkflowWorkScopeError('workflow run is outside the declared work scope') from error


def _source_count(snapshot: dict[str, object], source_key: str) -> int:
    refs = snapshot.get(source_key)

    return len(refs) if isinstance(refs, list) else 0


def _log_frozen_decision(*, work: WorkflowWork, occurrence_key: str, proposed: dict[str, object]) -> None:
    proposed_digest = proposed.get('input_digest')
    frozen_digest = work.input_snapshot.get('input_digest')
    if proposed_digest == frozen_digest:
        return

    logger.info(
        'digest_occurrence_frozen_decision_retained',
        work_id=str(work.id),
        occurrence_key=occurrence_key,
        proposed_input_digest=proposed_digest,
        frozen_input_digest=frozen_digest,
    )


def _create_from_snapshot(
    *,
    project: Project,
    work_type: str,
    subject_type: str,
    subject_id: UUID,
    team_id: UUID | None,
    occurrence_key: str,
    snapshot: dict[str, object],
    signal_task: object,
    workflow_run_id: UUID | None,
    source_key: str,
) -> ScheduleResult:
    workflow_run = _resolve_run(project, work_type, team_id, workflow_run_id) if workflow_run_id is not None else None
    data = CreateWorkflowWorkInput(
        organization_id=project.organization_id,
        project_id=project.id,
        work_type=work_type,
        subject_type=subject_type,
        subject_id=subject_id,
        input_snapshot=snapshot,
        occurrence_key=occurrence_key,
    )
    with transaction.atomic():
        work, created = create_digest_work_and_signal(
            data=data,
            signal_task=signal_task,
            workflow_run=workflow_run,
        )
    if created:
        source_count = _source_count(snapshot, source_key)
    else:
        source_count = _source_count(work.input_snapshot, source_key)
        _log_frozen_decision(work=work, occurrence_key=occurrence_key, proposed=snapshot)
    task_enqueued = created and work.disposition == WorkflowWorkDisposition.REQUIRED

    return ScheduleResult(
        work_id=work.id,
        created=created,
        disposition=work.disposition,
        source_count=source_count,
        task_enqueued=task_enqueued,
    )


def schedule_daily_project(
    *,
    project_id: UUID,
    bucket: DigestBucket,
    max_sources: int,
    workflow_run_id: UUID | None = None,
) -> ScheduleResult:
    project = _resolve_project(project_id)
    snapshot = freeze_daily_digest_input(
        organization_id=project.organization_id,
        project_id=project.id,
        window_start=bucket.window_start,
        window_end=bucket.window_end,
        schedule_key=bucket.schedule_key,
        max_sources=max_sources,
    )

    return _create_from_snapshot(
        project=project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        subject_type=WorkflowSubjectType.PROJECT,
        subject_id=project.id,
        team_id=None,
        occurrence_key=bucket.schedule_key,
        snapshot=snapshot,
        signal_task=generate_daily_digest_work_v1,
        workflow_run_id=workflow_run_id,
        source_key='sources',
    )


def schedule_weekly_project(
    *,
    project_id: UUID,
    bucket: DigestBucket,
    team_id: UUID | None = None,
    workflow_run_id: UUID | None = None,
) -> ScheduleResult:
    project = _resolve_project(project_id)
    snapshot = freeze_weekly_digest_input(
        organization_id=project.organization_id,
        project_id=project.id,
        team_id=team_id,
        window_start=bucket.window_start,
        window_end=bucket.window_end,
        schedule_key=bucket.schedule_key,
    )
    subject_type = WorkflowSubjectType.TEAM if team_id is not None else WorkflowSubjectType.PROJECT
    subject_id = team_id if team_id is not None else project.id

    return _create_from_snapshot(
        project=project,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        subject_type=subject_type,
        subject_id=subject_id,
        team_id=team_id,
        occurrence_key=bucket.schedule_key,
        snapshot=snapshot,
        signal_task=generate_weekly_digest_work_v1,
        workflow_run_id=workflow_run_id,
        source_key='changes',
    )


def _run_schedule(
    projects: Iterable[Project],
    schedule: Callable[[Project], ScheduleResult],
    *,
    schedule_name: str,
) -> dict[str, int]:
    scheduled_projects = 0
    required_work = 0
    no_input_projects = 0
    task_enqueued = 0
    failed_projects = 0
    for project in projects:
        try:
            result = schedule(project)
        except (WorkflowWorkScopeError, WorkflowWorkCollisionError, ValueError, DatabaseError) as error:
            failed_projects += 1
            logger.warning(
                'digest_schedule_project_failed',
                schedule=schedule_name,
                project_id=str(project.id),
                error=str(error),
            )
            continue
        scheduled_projects += 1
        if result.disposition == WorkflowWorkDisposition.REQUIRED:
            required_work += 1
        else:
            no_input_projects += 1
        if result.task_enqueued:
            task_enqueued += 1

    return {
        'scheduled_projects': scheduled_projects,
        'required_work': required_work,
        'no_input_projects': no_input_projects,
        'task_enqueued': task_enqueued,
        'failed_projects': failed_projects,
    }


def run_daily_schedule(*, as_of: datetime) -> dict[str, int]:
    bucket = daily_bucket(as_of=as_of, window_days=daily_window_days_default())
    max_sources = digest_max_sources()

    return _run_schedule(
        Project.objects.order_by('id'),
        lambda project: schedule_daily_project(project_id=project.id, bucket=bucket, max_sources=max_sources),
        schedule_name='daily',
    )


def run_weekly_schedule(*, as_of: datetime) -> dict[str, int]:
    bucket = weekly_bucket(as_of=as_of)

    return _run_schedule(
        Project.objects.order_by('id'),
        lambda project: schedule_weekly_project(project_id=project.id, bucket=bucket),
        schedule_name='weekly',
    )
