from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from datetime import timezone as datetime_timezone
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.services import MemoryWorkerError
from engram.memory.workflow_work import WorkflowWorkScopeError


def _load_digest_scheduler() -> object:
    import engram.memory.digest_scheduler as digest_scheduler

    return digest_scheduler


def _make_scope(suffix: str) -> tuple[Organization, Project]:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')

    return organization, project


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    title: str,
    body: str,
    status: str = MemoryStatus.APPROVED,
    visibility: str = VisibilityScope.PROJECT,
    kind: str = '',
) -> tuple[Memory, MemoryVersion]:
    metadata = {'kind': kind} if kind else {}
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=status,
        visibility_scope=visibility,
        metadata=metadata,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=memory.current_version,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )

    return memory, version


def _add_version(memory: Memory, *, body: str) -> MemoryVersion:
    next_version = memory.current_version + 1
    version = MemoryVersion.objects.create(
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=next_version,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )
    Memory.objects.filter(id=memory.id).update(current_version=next_version, body=body)

    return version


def _daily_bucket(scheduler: object, *, schedule_key: str = 'daily:2026-07-10') -> object:
    now = timezone.now()

    return scheduler.DigestBucket(
        work_type=WorkflowWorkType.DAILY_DIGEST,
        schedule_key=schedule_key,
        window_start=now - timedelta(days=1),
        window_end=now + timedelta(minutes=5),
    )


def _weekly_bucket(scheduler: object, *, schedule_key: str = 'weekly:2026-W28') -> object:
    now = timezone.now()

    return scheduler.DigestBucket(
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        schedule_key=schedule_key,
        window_start=now - timedelta(days=7),
        window_end=now + timedelta(minutes=5),
    )


def test_daily_bucket_before_cut_uses_prior_day_key_and_boundaries() -> None:
    scheduler = _load_digest_scheduler()

    bucket = scheduler.daily_bucket(as_of=datetime(2026, 7, 10, 1, 0, tzinfo=UTC))

    assert bucket.work_type == WorkflowWorkType.DAILY_DIGEST
    assert bucket.schedule_key == 'daily:2026-07-09'
    assert bucket.window_end == datetime(2026, 7, 9, 2, 0, tzinfo=UTC)
    assert bucket.window_start == datetime(2026, 7, 8, 2, 0, tzinfo=UTC)


def test_daily_bucket_after_cut_uses_same_day_key_and_boundaries() -> None:
    scheduler = _load_digest_scheduler()

    bucket = scheduler.daily_bucket(as_of=datetime(2026, 7, 10, 3, 0, tzinfo=UTC))

    assert bucket.schedule_key == 'daily:2026-07-10'
    assert bucket.window_end == datetime(2026, 7, 10, 2, 0, tzinfo=UTC)
    assert bucket.window_start == datetime(2026, 7, 9, 2, 0, tzinfo=UTC)


def test_daily_bucket_normalizes_equivalent_local_timestamps() -> None:
    scheduler = _load_digest_scheduler()
    utc_as_of = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)
    local_as_of = datetime(2026, 7, 10, 5, 0, tzinfo=datetime_timezone(timedelta(hours=2)))

    assert scheduler.daily_bucket(as_of=utc_as_of) == scheduler.daily_bucket(as_of=local_as_of)


def test_daily_bucket_window_days_override_only_moves_window_start() -> None:
    scheduler = _load_digest_scheduler()
    as_of = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)

    base = scheduler.daily_bucket(as_of=as_of)
    widened = scheduler.daily_bucket(as_of=as_of, window_days=3)

    assert widened.schedule_key == base.schedule_key
    assert widened.window_end == base.window_end
    assert widened.window_start == datetime(2026, 7, 7, 2, 0, tzinfo=UTC)


def test_daily_bucket_rejects_naive_as_of() -> None:
    scheduler = _load_digest_scheduler()

    with pytest.raises(ValueError):
        scheduler.daily_bucket(as_of=datetime(2026, 7, 10, 3, 0))  # noqa: DTZ001


def test_weekly_bucket_before_monday_cut_boundaries() -> None:
    scheduler = _load_digest_scheduler()

    bucket = scheduler.weekly_bucket(as_of=datetime(2026, 7, 13, 2, 59, tzinfo=UTC))

    assert bucket.work_type == WorkflowWorkType.WEEKLY_DIGEST
    assert bucket.window_end == datetime(2026, 7, 6, 3, 0, tzinfo=UTC)
    assert bucket.window_start == datetime(2026, 6, 29, 3, 0, tzinfo=UTC)
    year, week, _weekday = bucket.window_start.isocalendar()
    assert bucket.schedule_key == f'weekly:{year:04d}-W{week:02d}'


def test_weekly_bucket_across_monday_cut_moves_to_new_week() -> None:
    scheduler = _load_digest_scheduler()

    before = scheduler.weekly_bucket(as_of=datetime(2026, 7, 13, 2, 59, tzinfo=UTC))
    after = scheduler.weekly_bucket(as_of=datetime(2026, 7, 13, 3, 1, tzinfo=UTC))

    assert after.window_end == datetime(2026, 7, 13, 3, 0, tzinfo=UTC)
    assert after.window_start == datetime(2026, 7, 6, 3, 0, tzinfo=UTC)
    assert after.window_end == before.window_end + timedelta(days=7)
    assert after.schedule_key != before.schedule_key


def test_weekly_bucket_same_week_two_times_share_bucket() -> None:
    scheduler = _load_digest_scheduler()

    early = scheduler.weekly_bucket(as_of=datetime(2026, 7, 13, 10, 0, tzinfo=UTC))
    late = scheduler.weekly_bucket(as_of=datetime(2026, 7, 15, 20, 0, tzinfo=UTC))

    assert early == late
    assert early.window_end == datetime(2026, 7, 13, 3, 0, tzinfo=UTC)


def test_weekly_bucket_rejects_naive_as_of() -> None:
    scheduler = _load_digest_scheduler()

    with pytest.raises(ValueError):
        scheduler.weekly_bucket(as_of=datetime(2026, 7, 15, 12, 0))  # noqa: DTZ001


@pytest.mark.django_db(transaction=True)
def test_concurrent_daily_schedule_converges_on_one_work_and_signal() -> None:
    scheduler = _load_digest_scheduler()
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    organization, project = _make_scope('sched-daily-concurrent')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    schedule_key = 'daily:2026-07-10'
    barrier = Barrier(2)

    def produce() -> tuple[bool, str]:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            result = scheduler.schedule_daily_project(
                project_id=project.id,
                bucket=_daily_bucket(scheduler, schedule_key=schedule_key),
                max_sources=10,
            )

            return result.created, result.disposition
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(produce) for _index in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert sum(1 for created, _disposition in results if created) == 1
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    frozen = WorkflowWork.objects.get()
    original_snapshot = frozen.input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed', body='body-a changed')
    frozen.refresh_from_db()
    assert frozen.input_snapshot == original_snapshot


@pytest.mark.django_db
def test_daily_schedule_duplicate_with_changed_sources_keeps_winner_and_no_second_signal() -> None:
    scheduler = _load_digest_scheduler()
    organization, project = _make_scope('sched-daily-dup')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')

    first = scheduler.schedule_daily_project(project_id=project.id, bucket=_daily_bucket(scheduler), max_sources=10)
    assert first.created is True
    original_snapshot = WorkflowWork.objects.get(id=first.work_id).input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed')
    _add_version(memory, body='body-a-v2')
    _make_memory(organization, project, title='Beta', body='body-b')

    second = scheduler.schedule_daily_project(project_id=project.id, bucket=_daily_bucket(scheduler), max_sources=10)

    assert second.created is False
    assert second.work_id == first.work_id
    assert second.task_enqueued is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    assert WorkflowWork.objects.get(id=first.work_id).input_snapshot == original_snapshot


@pytest.mark.django_db
def test_daily_schedule_empty_input_is_terminal_no_input_and_idempotent() -> None:
    scheduler = _load_digest_scheduler()
    _organization, project = _make_scope('sched-daily-empty')

    first = scheduler.schedule_daily_project(project_id=project.id, bucket=_daily_bucket(scheduler), max_sources=10)

    assert first.created is True
    assert first.disposition == WorkflowWorkDisposition.NO_OP
    assert first.source_count == 0
    assert first.task_enqueued is False
    assert CeleryOutbox.objects.count() == 0
    assert WorkflowRun.objects.count() == 0
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    work = WorkflowWork.objects.get()
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT

    second = scheduler.schedule_daily_project(project_id=project.id, bucket=_daily_bucket(scheduler), max_sources=10)

    assert second.created is False
    assert second.task_enqueued is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_weekly_schedule_classifies_refutation_without_approved_proxy_and_excludes_digest() -> None:
    scheduler = _load_digest_scheduler()
    organization, project = _make_scope('sched-weekly-refuted')
    refuted, _version = _make_memory(
        organization,
        project,
        title='Refuted',
        body='body-refuted',
        status=MemoryStatus.REFUTED,
    )
    digest_source, _digest_version = _make_memory(
        organization,
        project,
        title='Digest',
        body='body-digest',
        kind='digest',
    )

    result = scheduler.schedule_weekly_project(project_id=project.id, bucket=_weekly_bucket(scheduler))

    assert result.created is True
    assert result.disposition == WorkflowWorkDisposition.REQUIRED
    assert result.source_count >= 1
    assert result.task_enqueued is True
    changes = WorkflowWork.objects.get(id=result.work_id).input_snapshot['changes']
    change_ids = {change['memory_id'] for change in changes}
    assert str(refuted.id) in change_ids
    assert str(digest_source.id) not in change_ids


@pytest.mark.django_db
def test_weekly_schedule_rejects_unlinked_team_before_work_creation() -> None:
    scheduler = _load_digest_scheduler()
    organization, project = _make_scope('sched-weekly-unlinked')
    unlinked_team = Team.objects.create(organization=organization, name='Unlinked', slug='sched-weekly-unlinked-team')
    _make_memory(organization, project, title='Project', body='body-project')

    with pytest.raises((ValueError, WorkflowWorkScopeError)):
        scheduler.schedule_weekly_project(
            project_id=project.id,
            bucket=_weekly_bucket(scheduler),
            team_id=unlinked_team.id,
        )

    assert WorkflowWork.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('work_kind', ('daily', 'weekly'))
def test_project_wide_schedule_rejects_foreign_scope_run_before_work_creation(work_kind: str) -> None:
    scheduler = _load_digest_scheduler()
    organization, project = _make_scope(f'sched-{work_kind}-foreign-run')
    _make_memory(organization, project, title='Alpha', body='body-a')
    foreign_organization, foreign_project = _make_scope(f'sched-{work_kind}-foreign-run-other')
    run_type = WorkflowRunType.DAILY_DIGEST if work_kind == 'daily' else WorkflowRunType.WEEKLY_DIGEST
    foreign_run = WorkflowRun.objects.create(
        organization=foreign_organization,
        project=foreign_project,
        run_type=run_type,
        status=WorkflowRunStatus.QUEUED,
    )

    with pytest.raises((ValueError, WorkflowWorkScopeError, MemoryWorkerError)):
        if work_kind == 'daily':
            scheduler.schedule_daily_project(
                project_id=project.id,
                bucket=_daily_bucket(scheduler),
                max_sources=10,
                workflow_run_id=foreign_run.id,
            )
        else:
            scheduler.schedule_weekly_project(
                project_id=project.id,
                bucket=_weekly_bucket(scheduler),
                workflow_run_id=foreign_run.id,
            )

    assert WorkflowWork.objects.filter(project=project).count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_daily_schedule_signal_payload_contains_only_work_id_and_no_source_body() -> None:
    scheduler = _load_digest_scheduler()
    organization, project = _make_scope('sched-daily-payload')
    secret = 'super-secret-source-body-xyz'
    memory, _version = _make_memory(organization, project, title='Secret source', body=secret)

    result = scheduler.schedule_daily_project(project_id=project.id, bucket=_daily_bucket(scheduler), max_sources=10)

    assert result.task_enqueued is True
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.generate_daily_digest_work_v1'
    assert queued.args == [str(result.work_id)]
    assert queued.kwargs == {}
    assert str(memory.id) not in repr(queued.args)
    for field_name in ('args', 'kwargs', 'options', 'redacted_args', 'redacted_kwargs'):
        assert secret not in repr(getattr(queued, field_name)), field_name
