from __future__ import annotations

import datetime
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from engram.core.models import (
    LinkType,
    Memory,
    MemoryLink,
    MemoryStatus,
    Organization,
    Project,
    Team,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.services import (
    BuildWeeklyStructuredDigest,
    WeeklyDigestInput,
    WeeklyDigestResult,
    run_weekly_digest_with_tracking,
)


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Digest Test Org', slug='digest-test-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_org,
        name='digest-project',
        slug='digest-project',
    )


def _make_memory(
    org: Organization,
    project: Project,
    title: str = 'mem',
    status: str = MemoryStatus.APPROVED,
    refuted: bool = False,
    team: Team | None = None,
) -> Memory:
    return Memory.objects.create(
        organization=org,
        project=project,
        title=title,
        body='body',
        status=status,
        refuted=refuted,
        team=team,
    )


def _make_link(
    org: Organization,
    project: Project,
    memory: Memory,
    link_type: str,
    target: Memory,
) -> MemoryLink:
    return MemoryLink.objects.create(
        organization=org,
        project=project,
        memory=memory,
        link_type=link_type,
        target=str(target.id),
    )


def _iso_week_monday(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=day.isoweekday() - 1)


def _window_start() -> datetime.datetime:
    monday = _iso_week_monday(timezone.now().date())

    return timezone.make_aware(datetime.datetime.combine(monday - datetime.timedelta(days=7), datetime.time.min))


def _window_end() -> datetime.datetime:
    monday = _iso_week_monday(timezone.now().date())

    return timezone.make_aware(datetime.datetime.combine(monday, datetime.time.min))


def _in_window() -> datetime.datetime:
    return _window_start() + datetime.timedelta(days=3)


def _out_of_window() -> datetime.datetime:
    return _window_start() - datetime.timedelta(days=3)


def _run(
    org: Organization,
    project: Project,
    window_days: int = 7,
    team_id: uuid.UUID | None = None,
) -> WeeklyDigestResult:
    return BuildWeeklyStructuredDigest().execute(
        WeeklyDigestInput(
            organization_id=org.id,
            project_id=project.id,
            window_days=window_days,
            team_id=team_id,
        ),
    )


@pytest.mark.django_db
def test_refuted_status_memory_in_window_goes_to_refuted_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='refuted-mem', status=MemoryStatus.REFUTED)

    Memory.objects.filter(id=mem.id).update(updated_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_refuted = [item['id'] for item in result.memory_changes['refuted']]

    assert str(mem.id) in ids_in_refuted

    assert result.counts['refuted'] >= 1


@pytest.mark.django_db
def test_refuted_flag_memory_in_window_goes_to_refuted_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='refuted-flag-mem', refuted=True)

    Memory.objects.filter(id=mem.id).update(updated_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_refuted = [item['id'] for item in result.memory_changes['refuted']]

    assert str(mem.id) in ids_in_refuted


@pytest.mark.django_db
def test_archived_memory_in_window_goes_to_retired_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='retired-mem', status=MemoryStatus.ARCHIVED)

    Memory.objects.filter(id=mem.id).update(updated_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_retired = [item['id'] for item in result.memory_changes['retired']]

    assert str(mem.id) in ids_in_retired

    assert result.counts['retired'] >= 1


@pytest.mark.django_db
def test_superseded_by_link_in_window_goes_to_superseded_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    loser = _make_memory(f_org, f_project, title='loser-mem')

    winner = _make_memory(f_org, f_project, title='winner-mem')

    link = _make_link(f_org, f_project, loser, LinkType.SUPERSEDED_BY, winner)

    MemoryLink.objects.filter(id=link.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_superseded = [item['id'] for item in result.memory_changes['superseded']]

    assert str(loser.id) in ids_in_superseded

    assert result.counts['superseded'] >= 1


@pytest.mark.django_db
def test_narrowed_by_link_in_window_goes_to_merged_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    narrow_mem = _make_memory(f_org, f_project, title='narrow-mem')

    target_mem = _make_memory(f_org, f_project, title='target-mem')

    link = _make_link(f_org, f_project, narrow_mem, LinkType.NARROWED_BY, target_mem)

    MemoryLink.objects.filter(id=link.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_merged = [item['id'] for item in result.memory_changes['merged']]

    assert str(narrow_mem.id) in ids_in_merged

    assert result.counts['merged'] >= 1


@pytest.mark.django_db
def test_new_memory_in_window_goes_to_added_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='new-mem')

    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_added = [item['id'] for item in result.memory_changes['added']]

    assert str(mem.id) in ids_in_added

    assert result.counts['added'] >= 1


@pytest.mark.django_db
def test_memory_outside_window_not_in_any_bucket(
    f_org: Organization,
    f_project: Project,
) -> None:
    old_mem = _make_memory(f_org, f_project, title='old-mem')

    Memory.objects.filter(id=old_mem.id).update(
        created_at=_out_of_window(),
        updated_at=_out_of_window(),
    )

    result = _run(f_org, f_project)

    all_ids: list[str] = []

    for items in result.memory_changes.values():
        all_ids.extend(item['id'] for item in items)

    assert str(old_mem.id) not in all_ids


@pytest.mark.django_db
def test_precedence_refuted_over_superseded(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='refuted-and-superseded', status=MemoryStatus.REFUTED)

    Memory.objects.filter(id=mem.id).update(updated_at=_in_window())

    winner = _make_memory(f_org, f_project, title='winner')

    link = _make_link(f_org, f_project, mem, LinkType.SUPERSEDED_BY, winner)

    MemoryLink.objects.filter(id=link.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_refuted = [item['id'] for item in result.memory_changes['refuted']]

    ids_in_superseded = [item['id'] for item in result.memory_changes['superseded']]

    assert str(mem.id) in ids_in_refuted

    assert str(mem.id) not in ids_in_superseded


@pytest.mark.django_db
def test_precedence_retired_over_merged(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='retired-and-merged', status=MemoryStatus.ARCHIVED)

    Memory.objects.filter(id=mem.id).update(updated_at=_in_window())

    target = _make_memory(f_org, f_project, title='target')

    link = _make_link(f_org, f_project, mem, LinkType.NARROWED_BY, target)

    MemoryLink.objects.filter(id=link.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    ids_in_retired = [item['id'] for item in result.memory_changes['retired']]

    ids_in_merged = [item['id'] for item in result.memory_changes['merged']]

    assert str(mem.id) in ids_in_retired

    assert str(mem.id) not in ids_in_merged


@pytest.mark.django_db
def test_added_memory_excluded_when_also_refuted_in_window(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='new-and-refuted', status=MemoryStatus.REFUTED)

    in_win = _in_window()

    Memory.objects.filter(id=mem.id).update(created_at=in_win, updated_at=in_win)

    result = _run(f_org, f_project)

    ids_in_refuted = [item['id'] for item in result.memory_changes['refuted']]

    ids_in_added = [item['id'] for item in result.memory_changes['added']]

    assert str(mem.id) in ids_in_refuted

    assert str(mem.id) not in ids_in_added


@pytest.mark.django_db
def test_ready_is_false_initially(
    f_org: Organization,
    f_project: Project,
) -> None:
    result = _run(f_org, f_project)

    assert result.ready is False

    assert result.digest_memory.metadata['ready'] is False


@pytest.mark.django_db
def test_idempotent_rerun_returns_same_digest_memory(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='added-mem')

    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result1 = _run(f_org, f_project)

    result2 = _run(f_org, f_project)

    assert result1.digest_memory.id == result2.digest_memory.id

    assert (
        Memory.objects.filter(
            organization=f_org,
            project=f_project,
            metadata__kind='digest',
            metadata__digest_kind='weekly_structured',
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_reruns_within_same_iso_week_produce_only_one_digest(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='added-mem')

    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    monday_this_week = _iso_week_monday(timezone.now().date())

    first_instant = timezone.make_aware(datetime.datetime.combine(monday_this_week, datetime.time(9, 17)))

    second_instant = timezone.make_aware(
        datetime.datetime.combine(monday_this_week + datetime.timedelta(days=4), datetime.time(23, 45)),
    )

    with mock.patch('engram.memory.services.timezone.now', return_value=first_instant):
        result1 = _run(f_org, f_project)

    with mock.patch('engram.memory.services.timezone.now', return_value=second_instant):
        result2 = _run(f_org, f_project)

    assert result1.digest_memory.id == result2.digest_memory.id

    assert (
        Memory.objects.filter(
            organization=f_org,
            project=f_project,
            metadata__kind='digest',
            metadata__digest_kind='weekly_structured',
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_rerun_in_different_iso_week_creates_new_digest(
    f_org: Organization,
    f_project: Project,
) -> None:
    monday_this_week = _iso_week_monday(timezone.now().date())

    first_instant = timezone.make_aware(datetime.datetime.combine(monday_this_week, datetime.time(10, 0)))

    next_week_instant = timezone.make_aware(
        datetime.datetime.combine(monday_this_week + datetime.timedelta(days=7), datetime.time(10, 0)),
    )

    with mock.patch('engram.memory.services.timezone.now', return_value=first_instant):
        result1 = _run(f_org, f_project)

    with mock.patch('engram.memory.services.timezone.now', return_value=next_week_instant):
        result2 = _run(f_org, f_project)

    assert result1.digest_memory.id != result2.digest_memory.id

    assert (
        Memory.objects.filter(
            organization=f_org,
            project=f_project,
            metadata__kind='digest',
            metadata__digest_kind='weekly_structured',
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_window_covers_monday_to_monday_boundary_of_completed_week(
    f_org: Organization,
    f_project: Project,
) -> None:
    result = _run(f_org, f_project)

    window_start = datetime.datetime.fromisoformat(result.digest_memory.metadata['window_start'])

    window_end = datetime.datetime.fromisoformat(result.digest_memory.metadata['window_end'])

    assert window_start.isoweekday() == 1

    assert window_end.isoweekday() == 1

    assert window_start.time() == datetime.time.min

    assert window_end.time() == datetime.time.min

    assert (window_end.date() - window_start.date()).days == 7


@pytest.mark.django_db
def test_counts_match_bucket_lengths(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem_added = _make_memory(f_org, f_project, title='added-count-mem')

    Memory.objects.filter(id=mem_added.id).update(created_at=_in_window())

    mem_retired = _make_memory(f_org, f_project, title='retired-count-mem', status=MemoryStatus.ARCHIVED)

    Memory.objects.filter(id=mem_retired.id).update(updated_at=_in_window())

    result = _run(f_org, f_project)

    for bucket, items in result.memory_changes.items():
        assert result.counts[bucket] == len(items), f'counts mismatch for bucket {bucket}'


@pytest.mark.django_db
def test_run_weekly_digest_with_tracking_creates_succeeded_workflow_run(
    f_org: Organization,
    f_project: Project,
) -> None:
    result = run_weekly_digest_with_tracking(
        organization_id=f_org.id,
        project_id=f_project.id,
        window_days=7,
    )

    run = WorkflowRun.objects.filter(
        organization=f_org,
        project=f_project,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
    ).first()

    assert run is not None

    assert run.status == WorkflowRunStatus.SUCCEEDED

    assert run.result_memory_id == result.digest_memory.id


@pytest.mark.django_db
def test_different_projects_isolated(
    f_org: Organization,
    f_project: Project,
) -> None:
    other_project = Project.objects.create(
        organization=f_org,
        name='other-project',
        slug='other-project',
    )

    mem_in_project = _make_memory(f_org, f_project, title='proj-mem')

    Memory.objects.filter(id=mem_in_project.id).update(created_at=_in_window())

    mem_in_other = _make_memory(f_org, other_project, title='other-mem')

    Memory.objects.filter(id=mem_in_other.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    all_ids = [item['id'] for items in result.memory_changes.values() for item in items]

    assert str(mem_in_project.id) in all_ids

    assert str(mem_in_other.id) not in all_ids


@pytest.mark.django_db
def test_team_id_restricts_added_bucket_to_that_team(
    f_org: Organization,
    f_project: Project,
) -> None:
    team_a = Team.objects.create(organization=f_org, name='Team A', slug='team-a')

    team_b = Team.objects.create(organization=f_org, name='Team B', slug='team-b')

    mem_team_a = _make_memory(f_org, f_project, title='team-a-mem', team=team_a)

    Memory.objects.filter(id=mem_team_a.id).update(created_at=_in_window())

    mem_team_b = _make_memory(f_org, f_project, title='team-b-mem', team=team_b)

    Memory.objects.filter(id=mem_team_b.id).update(created_at=_in_window())

    result = _run(f_org, f_project, team_id=team_a.id)

    all_ids = [item['id'] for items in result.memory_changes.values() for item in items]

    assert str(mem_team_a.id) in all_ids

    assert str(mem_team_b.id) not in all_ids


@pytest.mark.django_db
def test_without_team_id_all_project_memories_considered(
    f_org: Organization,
    f_project: Project,
) -> None:
    team_a = Team.objects.create(organization=f_org, name='Team A', slug='team-a')

    team_b = Team.objects.create(organization=f_org, name='Team B', slug='team-b')

    mem_team_a = _make_memory(f_org, f_project, title='team-a-mem', team=team_a)

    Memory.objects.filter(id=mem_team_a.id).update(created_at=_in_window())

    mem_team_b = _make_memory(f_org, f_project, title='team-b-mem', team=team_b)

    Memory.objects.filter(id=mem_team_b.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    all_ids = [item['id'] for items in result.memory_changes.values() for item in items]

    assert str(mem_team_a.id) in all_ids

    assert str(mem_team_b.id) in all_ids


@pytest.mark.django_db
def test_team_scoped_digest_has_independent_content_hash_from_unscoped(
    f_org: Organization,
    f_project: Project,
) -> None:
    team_a = Team.objects.create(organization=f_org, name='Team A', slug='team-a')

    mem_team_a = _make_memory(f_org, f_project, title='team-a-mem', team=team_a)

    Memory.objects.filter(id=mem_team_a.id).update(created_at=_in_window())

    mem_no_team = _make_memory(f_org, f_project, title='no-team-mem')

    Memory.objects.filter(id=mem_no_team.id).update(created_at=_in_window())

    unscoped_result = _run(f_org, f_project)

    team_scoped_result = _run(f_org, f_project, team_id=team_a.id)

    assert unscoped_result.digest_memory.id != team_scoped_result.digest_memory.id

    team_scoped_ids = [item['id'] for items in team_scoped_result.memory_changes.values() for item in items]

    assert str(mem_no_team.id) not in team_scoped_ids
