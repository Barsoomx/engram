from __future__ import annotations

import datetime
import uuid
from unittest import mock

import pytest
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.context.services import authorized_retrieval_documents
from engram.core.models import (
    LinkType,
    Memory,
    MemoryLink,
    MemoryStatus,
    MemoryTransition,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.services import (
    BuildWeeklyStructuredDigest,
    WeeklyDigestInput,
    WeeklyDigestResult,
    run_weekly_digest_with_tracking,
    weekly_digest_content_hash,
)
from engram.memory.transitions import PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request


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
    candidate, _source, _session = provenanced_candidate_in_scope(
        org,
        project,
        team,
        suffix='weekly-digest',
        title=title,
        body='body',
        visibility_scope=VisibilityScope.PROJECT,
    )
    memory = PromoteMemoryCandidate().execute(transition_request(candidate)).memory
    update_fields: list[str] = []
    if status != MemoryStatus.APPROVED:
        memory.status = status
        update_fields.append('status')
    if refuted:
        memory.refuted = True
        update_fields.append('refuted')
    if update_fields:
        update_fields.append('updated_at')
        memory.save(update_fields=update_fields)

    return memory


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


def _read_scope(org: Organization, project: Project) -> EffectiveScope:
    return EffectiveScope(
        organization_id=org.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(project.id,),
        team_ids=(),
        capabilities=(),
        actor_type='user',
        actor_id='reader',
        project_bound=True,
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
    current_mem = _make_memory(f_org, f_project, title='current-mem')

    Memory.objects.filter(id=old_mem.id).update(
        created_at=_out_of_window(),
        updated_at=_out_of_window(),
    )
    Memory.objects.filter(id=current_mem.id).update(created_at=_in_window())

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
    mem = _make_memory(f_org, f_project, title='ready-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

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
    mem = _make_memory(f_org, f_project, title='rerun-source')
    monday_this_week = _iso_week_monday(timezone.now().date())

    first_instant = timezone.make_aware(datetime.datetime.combine(monday_this_week, datetime.time(10, 0)))

    next_week_instant = timezone.make_aware(
        datetime.datetime.combine(monday_this_week + datetime.timedelta(days=7), datetime.time(10, 0)),
    )
    Memory.objects.filter(id=mem.id).update(created_at=first_instant - datetime.timedelta(days=1))

    with mock.patch('engram.memory.services.timezone.now', return_value=first_instant):
        result1 = _run(f_org, f_project)

    Memory.objects.filter(id=mem.id).update(created_at=next_week_instant - datetime.timedelta(days=1))

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
    mem = _make_memory(f_org, f_project, title='boundary-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    window_start = datetime.datetime.fromisoformat(result.digest_memory.metadata['window_start'])

    window_end = datetime.datetime.fromisoformat(result.digest_memory.metadata['window_end'])

    assert window_start.isoweekday() == 1

    assert window_end.isoweekday() == 1

    assert window_start.time() == datetime.time.min

    assert window_end.time() == datetime.time.min

    assert (window_end.date() - window_start.date()).days == 7


@pytest.mark.django_db
def test_weeks_back_shifts_window_to_earlier_week(
    f_org: Organization,
    f_project: Project,
) -> None:
    current_mem = _make_memory(f_org, f_project, title='current-source')
    previous_mem = _make_memory(f_org, f_project, title='previous-source')
    Memory.objects.filter(id=current_mem.id).update(created_at=_in_window())
    Memory.objects.filter(id=previous_mem.id).update(
        created_at=_window_start() - datetime.timedelta(days=1),
    )

    current = _run(f_org, f_project)

    previous = BuildWeeklyStructuredDigest().execute(
        WeeklyDigestInput(
            organization_id=f_org.id,
            project_id=f_project.id,
            weeks_back=1,
        ),
    )

    current_end = datetime.datetime.fromisoformat(current.digest_memory.metadata['window_end'])

    previous_end = datetime.datetime.fromisoformat(previous.digest_memory.metadata['window_end'])

    assert (current_end.date() - previous_end.date()).days == 7

    assert previous.digest_memory.id != current.digest_memory.id


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
    mem = _make_memory(f_org, f_project, title='tracking-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

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
def test_run_weekly_digest_with_tracking_adopts_existing_queued_run(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='adopt-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    queued = WorkflowRun.objects.create(
        organization=f_org,
        project=f_project,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        status=WorkflowRunStatus.QUEUED,
        request_id='weekly-adopt-1',
        input_snapshot={'window_days': 7},
    )

    result = run_weekly_digest_with_tracking(
        organization_id=f_org.id,
        project_id=f_project.id,
        window_days=7,
        request_id='weekly-adopt-1',
        existing_run_id=queued.id,
    )

    queued.refresh_from_db()

    assert queued.status == WorkflowRunStatus.SUCCEEDED

    assert queued.result_memory_id == result.digest_memory.id

    assert (
        WorkflowRun.objects.filter(
            organization=f_org,
            project=f_project,
            run_type=WorkflowRunType.WEEKLY_DIGEST,
        ).count()
        == 1
    )


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
def test_without_team_id_excludes_team_owned_memories(
    f_org: Organization,
    f_project: Project,
) -> None:
    team_a = Team.objects.create(organization=f_org, name='Team A', slug='team-a')

    team_b = Team.objects.create(organization=f_org, name='Team B', slug='team-b')

    mem_team_a = _make_memory(f_org, f_project, title='team-a-mem', team=team_a)

    Memory.objects.filter(id=mem_team_a.id).update(created_at=_out_of_window())

    mem_team_b = _make_memory(f_org, f_project, title='team-b-mem', team=team_b)

    Memory.objects.filter(id=mem_team_b.id).update(created_at=_out_of_window())

    mem_no_team = _make_memory(f_org, f_project, title='no-team-mem')
    Memory.objects.filter(id=mem_no_team.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    all_ids = [item['id'] for items in result.memory_changes.values() for item in items]

    assert str(mem_team_a.id) not in all_ids

    assert str(mem_team_b.id) not in all_ids

    assert str(mem_no_team.id) in all_ids


@pytest.mark.django_db
def test_team_scoped_digest_has_independent_content_hash_from_unscoped(
    f_org: Organization,
    f_project: Project,
) -> None:
    team_a = Team.objects.create(organization=f_org, name='Team A', slug='team-a')

    mem_team_a = _make_memory(f_org, f_project, title='team-a-mem', team=team_a)
    Memory.objects.filter(id=mem_team_a.id).update(created_at=_out_of_window())

    mem_no_team = _make_memory(f_org, f_project, title='no-team-mem')

    Memory.objects.filter(id=mem_no_team.id).update(created_at=_in_window())

    unscoped_result = _run(f_org, f_project)

    Memory.objects.filter(id=mem_team_a.id).update(created_at=_in_window())

    team_scoped_result = _run(f_org, f_project, team_id=team_a.id)

    assert unscoped_result.digest_memory.id != team_scoped_result.digest_memory.id

    team_scoped_ids = [item['id'] for items in team_scoped_result.memory_changes.values() for item in items]

    assert str(mem_no_team.id) not in team_scoped_ids


@pytest.mark.django_db
def test_weekly_digest_output_is_quarantined_from_retrieval_until_proven(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='quarantine-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    version = MemoryVersion.objects.get(memory=result.digest_memory)

    assert version.version == 1

    document = RetrievalDocument.objects.get(memory=result.digest_memory)

    authorized = authorized_retrieval_documents(f_org, f_project, _read_scope(f_org, f_project))

    assert document.id not in [doc.id for doc in authorized]


@pytest.mark.django_db
def test_idempotent_rerun_does_not_duplicate_version_or_document(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='idempotent-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result1 = _run(f_org, f_project)

    result2 = _run(f_org, f_project)

    assert result1.digest_memory.id == result2.digest_memory.id

    assert Memory.objects.filter(id=result1.digest_memory.id).count() == 1

    assert MemoryVersion.objects.filter(memory=result1.digest_memory).count() == 1

    assert RetrievalDocument.objects.filter(memory=result1.digest_memory).count() == 1


@pytest.mark.django_db
def test_legacy_orphan_without_version_or_document_is_not_reused_or_reduplicated(
    f_org: Organization,
    f_project: Project,
) -> None:
    source = _make_memory(f_org, f_project, title='orphan-source')
    Memory.objects.filter(id=source.id).update(created_at=_in_window())

    window_start = _window_start()

    window_end = _window_end()

    content_hash = weekly_digest_content_hash(f_project.id, window_start, window_end, None)

    orphan = Memory.objects.create(
        organization=f_org,
        project=f_project,
        title=f'Weekly Structured Digest {window_start.date()} to {window_end.date()}',
        body='legacy orphan digest with no version or retrieval document',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={
            'kind': 'digest',
            'digest_kind': 'weekly_structured',
            'window_start': window_start.isoformat(),
            'window_end': window_end.isoformat(),
            'window_days': 7,
            'memory_changes': {},
            'counts': {},
            'content_hash': content_hash,
            'ready': False,
            'reviewed_at': None,
        },
    )

    result1 = _run(f_org, f_project)

    result2 = _run(f_org, f_project)

    assert result1.digest_memory.id == result2.digest_memory.id

    assert result1.digest_memory.id != orphan.id

    assert (
        Memory.objects.filter(
            organization=f_org,
            project=f_project,
            metadata__kind='digest',
            metadata__digest_kind='weekly_structured',
        ).count()
        == 2
    )

    assert MemoryVersion.objects.filter(memory=result1.digest_memory).count() == 1

    assert RetrievalDocument.objects.filter(memory=result1.digest_memory).count() == 1


@pytest.mark.django_db
def test_weekly_digest_body_contains_bucketed_titles(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='DistinctAddedTitle')

    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    body = result.digest_memory.body

    assert '## Added' in body

    assert 'DistinctAddedTitle' in body

    assert 'memory_changes' in result.digest_memory.metadata

    assert result.digest_memory.metadata['counts']['added'] >= 1


@pytest.mark.django_db
def test_weekly_digest_retrieval_document_full_text_contains_titles(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='SearchableAddedTitle')

    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    document = RetrievalDocument.objects.get(memory=result.digest_memory)

    assert 'SearchableAddedTitle' in document.full_text


@pytest.mark.django_db
def test_weekly_digest_emits_digest_generated_audit(
    f_org: Organization,
    f_project: Project,
) -> None:
    mem = _make_memory(f_org, f_project, title='audit-source')
    Memory.objects.filter(id=mem.id).update(created_at=_in_window())

    result = _run(f_org, f_project)

    transition = MemoryTransition.objects.get(result_memory=result.digest_memory)
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'
    assert transition.audit_event.metadata['schema'] == 'memory_transition/v1'
    assert transition.audit_event.metadata['transition_type'] == 'publish_digest'
