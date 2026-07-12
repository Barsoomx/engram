from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.tasks import generate_daily_digest_work_v1, generate_weekly_digest_work_v1
from engram.memory.workflow_work import CreateWorkflowWorkInput

_DAILY_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'
_WEEKLY_TASK_NAME = 'engram.memory.generate_weekly_digest_work_v1'


def _load_digest_work() -> object:
    import engram.memory.digest_work as digest_work

    return digest_work


class _SignalFailureError(RuntimeError):
    pass


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in '0123456789abcdef' for character in value)


def _make_scope(suffix: str) -> tuple[Organization, Project]:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')

    return organization, project


def _make_team(organization: Organization, project: Project, suffix: str) -> Team:
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    ProjectTeam.objects.create(organization=organization, project=project, team=team)

    return team


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    title: str,
    body: str,
    team: Team | None = None,
    visibility: str = VisibilityScope.PROJECT,
    status: str = MemoryStatus.APPROVED,
    kind: str = '',
) -> tuple[Memory, MemoryVersion]:
    metadata = {'kind': kind} if kind else {}
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
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


def _daily_window() -> tuple[object, object]:
    now = timezone.now()

    return now - timedelta(days=1), now + timedelta(minutes=5)


def _weekly_window() -> tuple[object, object]:
    now = timezone.now()

    return now - timedelta(days=7), now + timedelta(minutes=5)


def _daily_data(
    organization: Organization,
    project: Project,
    snapshot: dict[str, object],
    schedule_key: str,
) -> CreateWorkflowWorkInput:
    return CreateWorkflowWorkInput(
        organization_id=organization.id,
        project_id=project.id,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        subject_type=WorkflowSubjectType.PROJECT,
        subject_id=project.id,
        input_snapshot=snapshot,
        occurrence_key=schedule_key,
    )


def _weekly_data(
    organization: Organization,
    project: Project,
    team: Team | None,
    snapshot: dict[str, object],
    schedule_key: str,
) -> CreateWorkflowWorkInput:
    subject_type = WorkflowSubjectType.TEAM if team is not None else WorkflowSubjectType.PROJECT
    subject_id = team.id if team is not None else project.id

    return CreateWorkflowWorkInput(
        organization_id=organization.id,
        project_id=project.id,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        subject_type=subject_type,
        subject_id=subject_id,
        input_snapshot=snapshot,
        occurrence_key=schedule_key,
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_daily_creates_leave_one_immutable_snapshot() -> None:
    digest_work = _load_digest_work()
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    organization, project = _make_scope('daily-concurrent')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _daily_window()
    schedule_key = 'daily:2026-07-10'
    barrier = Barrier(2)

    def produce() -> tuple[bool, dict[str, object]]:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            with transaction.atomic():
                snapshot = digest_work.freeze_daily_digest_input(
                    organization_id=organization.id,
                    project_id=project.id,
                    window_start=window_start,
                    window_end=window_end,
                    schedule_key=schedule_key,
                    max_sources=10,
                )
                work, created = digest_work.create_digest_work_and_signal(
                    data=_daily_data(organization, project, snapshot, schedule_key),
                    signal_task=generate_daily_digest_work_v1,
                )

                return created, work.input_snapshot
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(produce) for _index in range(2)]
        results = [future.result(timeout=15) for future in futures]

    created_flags = [created for created, _snapshot in results]
    assert sum(1 for flag in created_flags if flag) == 1
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    frozen = WorkflowWork.objects.get()
    original_snapshot = frozen.input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed', body='body-a changed')
    frozen.refresh_from_db()
    assert frozen.input_snapshot == original_snapshot


@pytest.mark.django_db(transaction=True)
def test_concurrent_weekly_creates_leave_one_immutable_snapshot() -> None:
    digest_work = _load_digest_work()
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    organization, project = _make_scope('weekly-concurrent')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _weekly_window()
    schedule_key = 'weekly:2026-W28'
    barrier = Barrier(2)

    def produce() -> bool:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            with transaction.atomic():
                snapshot = digest_work.freeze_weekly_digest_input(
                    organization_id=organization.id,
                    project_id=project.id,
                    team_id=None,
                    window_start=window_start,
                    window_end=window_end,
                    schedule_key=schedule_key,
                )
                _work, created = digest_work.create_digest_work_and_signal(
                    data=_weekly_data(organization, project, None, snapshot, schedule_key),
                    signal_task=generate_weekly_digest_work_v1,
                )

                return created
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(produce) for _index in range(2)]
        created_flags = [future.result(timeout=15) for future in futures]

    assert sum(1 for flag in created_flags if flag) == 1
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    frozen = WorkflowWork.objects.get()
    original_snapshot = frozen.input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed', body='body-a changed')
    frozen.refresh_from_db()
    assert frozen.input_snapshot == original_snapshot


@pytest.mark.django_db
def test_daily_source_change_after_create_does_not_rewrite_snapshot() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-immutable')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _daily_window()
    schedule_key = 'daily:2026-07-10'

    with transaction.atomic():
        snapshot = digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
            max_sources=10,
        )
        work, created = digest_work.create_digest_work_and_signal(
            data=_daily_data(organization, project, snapshot, schedule_key),
            signal_task=generate_daily_digest_work_v1,
        )

    assert created is True
    original_snapshot = WorkflowWork.objects.get(id=work.id).input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed')
    _add_version(memory, body='body-a-v2')

    with transaction.atomic():
        rebuilt = digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
            max_sources=10,
        )
        reused, created_again = digest_work.create_digest_work_and_signal(
            data=_daily_data(organization, project, rebuilt, schedule_key),
            signal_task=generate_daily_digest_work_v1,
        )

    assert reused.id == work.id
    assert created_again is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    assert WorkflowWork.objects.get(id=work.id).input_snapshot == original_snapshot


@pytest.mark.django_db
def test_weekly_source_change_after_create_does_not_rewrite_snapshot() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-immutable')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _weekly_window()
    schedule_key = 'weekly:2026-W28'

    with transaction.atomic():
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
        )
        work, created = digest_work.create_digest_work_and_signal(
            data=_weekly_data(organization, project, None, snapshot, schedule_key),
            signal_task=generate_weekly_digest_work_v1,
        )

    assert created is True
    original_snapshot = WorkflowWork.objects.get(id=work.id).input_snapshot

    Memory.objects.filter(id=memory.id).update(title='Alpha changed')
    _add_version(memory, body='body-a-v2')
    _make_memory(organization, project, title='Beta', body='body-b')

    with transaction.atomic():
        rebuilt = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
        )
        reused, created_again = digest_work.create_digest_work_and_signal(
            data=_weekly_data(organization, project, None, rebuilt, schedule_key),
            signal_task=generate_weekly_digest_work_v1,
        )

    assert reused.id == work.id
    assert created_again is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    assert WorkflowWork.objects.get(id=work.id).input_snapshot == original_snapshot


@pytest.mark.django_db
def test_daily_snapshot_records_exact_versions_and_body_digests() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-exact')
    memory_a, version_a = _make_memory(organization, project, title='Alpha', body='body-a')
    memory_b, version_b = _make_memory(organization, project, title='Beta', body='body-b')
    window_start, window_end = _daily_window()

    snapshot = digest_work.freeze_daily_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        window_start=window_start,
        window_end=window_end,
        schedule_key='daily:2026-07-10',
        max_sources=10,
    )

    assert snapshot['schema'] == 'daily_digest_input/v1'
    assert snapshot['output_visibility_scope'] == 'project'
    assert snapshot['output_team_id'] is None
    assert snapshot['allowed_team_ids'] == []
    assert snapshot['eligible_source_count'] == 2
    assert snapshot['sources_truncated'] is False
    assert _is_sha256(snapshot['input_digest'])

    sources = sorted(snapshot['sources'], key=lambda source: source['render_position'])
    assert [source['memory_id'] for source in sources] == [str(memory_a.id), str(memory_b.id)]
    positions = [source['render_position'] for source in sources]
    assert positions == sorted(set(positions))
    expected_versions = {str(memory_a.id): version_a, str(memory_b.id): version_b}
    for source in sources:
        expected_version = expected_versions[source['memory_id']]
        assert source['memory_version_id'] == str(expected_version.id)
        assert source['version'] == expected_version.version
        assert source['visibility_scope'] == 'project'
        assert source['team_id'] is None
        assert _is_sha256(source['server_body_digest'])
    assert sources[0]['source_title'] == 'Alpha'
    assert sources[1]['source_title'] == 'Beta'


@pytest.mark.django_db
def test_weekly_snapshot_records_exact_versions_and_body_digests() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-exact')
    memory, version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _weekly_window()

    snapshot = digest_work.freeze_weekly_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        window_start=window_start,
        window_end=window_end,
        schedule_key='weekly:2026-W28',
    )

    assert snapshot['schema'] == 'weekly_digest_input/v1'
    assert snapshot['team_id'] is None
    assert snapshot['allowed_team_ids'] == []
    assert snapshot['output_visibility_scope'] == 'project'
    assert snapshot['output_team_id'] is None
    assert _is_sha256(snapshot['input_digest'])

    changes = snapshot['changes']
    assert isinstance(changes, list)
    change_memory_ids = {change['memory_id'] for change in changes}
    assert str(memory.id) in change_memory_ids
    for change in changes:
        assert {'memory_version_id', 'version', 'server_body_digest'} <= change.keys()
        assert _is_sha256(change['server_body_digest'])
    frozen = next(change for change in changes if change['memory_id'] == str(memory.id))
    assert frozen['memory_version_id'] == str(version.id)
    assert frozen['version'] == version.version


@pytest.mark.django_db
def test_daily_input_digest_covers_body_not_memory_ids_only() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-body-digest')
    memory, _version = _make_memory(organization, project, title='Alpha', body='body-a')
    window_start, window_end = _daily_window()

    def freeze() -> dict[str, object]:
        return digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key='daily:2026-07-10',
            max_sources=10,
        )

    first = freeze()
    Memory.objects.filter(id=memory.id).update(body='mutable body only')
    unchanged = freeze()
    assert unchanged['input_digest'] == first['input_digest']

    _add_version(memory, body='body-a-v2')
    reselected = freeze()
    assert reselected['input_digest'] != first['input_digest']
    first_source = first['sources'][0]
    reselected_source = reselected['sources'][0]
    assert reselected_source['server_body_digest'] != first_source['server_body_digest']


@pytest.mark.django_db
def test_daily_project_output_excludes_team_private_session_org_and_digest_sources() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-exclusions')
    team = _make_team(organization, project, 'daily-exclusions')
    included, _version = _make_memory(organization, project, title='Included', body='body-included')
    _make_memory(organization, project, title='Team', body='body-team', team=team, visibility=VisibilityScope.TEAM)
    _make_memory(organization, project, title='Session', body='body-session', visibility=VisibilityScope.SESSION)
    _make_memory(
        organization,
        project,
        title='Org',
        body='body-org',
        visibility=VisibilityScope.ORGANIZATION,
    )
    _make_memory(organization, project, title='Digest', body='body-digest', kind='digest')
    foreign_organization, foreign_project = _make_scope('daily-exclusions-foreign')
    _make_memory(foreign_organization, foreign_project, title='Foreign', body='body-foreign')
    window_start, window_end = _daily_window()

    snapshot = digest_work.freeze_daily_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        window_start=window_start,
        window_end=window_end,
        schedule_key='daily:2026-07-10',
        max_sources=10,
    )

    assert {source['memory_id'] for source in snapshot['sources']} == {str(included.id)}
    assert snapshot['eligible_source_count'] == 1


@pytest.mark.django_db
def test_daily_caps_sources_and_marks_truncated_by_updated_order() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-cap')
    older, _older_version = _make_memory(organization, project, title='Older', body='body-older')
    newer, _newer_version = _make_memory(organization, project, title='Newer', body='body-newer')
    now = timezone.now()
    Memory.objects.filter(id=older.id).update(updated_at=now - timedelta(hours=2))
    Memory.objects.filter(id=newer.id).update(updated_at=now - timedelta(hours=1))
    window_start, window_end = _daily_window()

    snapshot = digest_work.freeze_daily_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        window_start=window_start,
        window_end=window_end,
        schedule_key='daily:2026-07-10',
        max_sources=1,
    )

    assert snapshot['max_sources'] == 1
    assert snapshot['eligible_source_count'] == 2
    assert snapshot['sources_truncated'] is True
    assert {source['memory_id'] for source in snapshot['sources']} == {str(newer.id)}


@pytest.mark.django_db
def test_daily_rejects_cross_project_source_reference() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-cross-project')
    _included, _version = _make_memory(organization, project, title='Included', body='body-included')
    foreign_org, foreign_project = _make_scope('daily-cross-project-foreign')
    foreign_memory, foreign_version = _make_memory(
        foreign_org,
        foreign_project,
        title='Foreign',
        body='body-foreign',
    )
    window_start, window_end = _daily_window()
    schedule_key = 'daily:2026-07-10'

    snapshot = digest_work.freeze_daily_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        window_start=window_start,
        window_end=window_end,
        schedule_key=schedule_key,
        max_sources=10,
    )
    tampered = dict(snapshot)
    tampered['sources'] = [
        *snapshot['sources'],
        {
            'render_position': len(snapshot['sources']),
            'memory_id': str(foreign_memory.id),
            'memory_version_id': str(foreign_version.id),
            'version': foreign_version.version,
            'server_body_digest': hashlib.sha256(b'body-foreign').hexdigest(),
            'visibility_scope': 'project',
            'team_id': None,
            'source_title': 'Foreign',
        },
    ]

    from engram.memory.workflow_work import WorkflowWorkScopeError

    with pytest.raises((ValueError, WorkflowWorkScopeError)):
        with transaction.atomic():
            digest_work.create_digest_work_and_signal(
                data=_daily_data(organization, project, tampered, schedule_key),
                signal_task=generate_daily_digest_work_v1,
            )

    assert WorkflowWork.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_weekly_team_output_admits_only_selected_team() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-team')
    team_a = _make_team(organization, project, 'weekly-team-a')
    team_b = _make_team(organization, project, 'weekly-team-b')
    project_memory, _project_version = _make_memory(organization, project, title='Project', body='body-project')
    team_a_memory, _team_a_version = _make_memory(
        organization,
        project,
        title='TeamA',
        body='body-team-a',
        team=team_a,
        visibility=VisibilityScope.TEAM,
    )
    team_b_memory, _team_b_version = _make_memory(
        organization,
        project,
        title='TeamB',
        body='body-team-b',
        team=team_b,
        visibility=VisibilityScope.TEAM,
    )
    window_start, window_end = _weekly_window()

    snapshot = digest_work.freeze_weekly_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team_a.id,
        window_start=window_start,
        window_end=window_end,
        schedule_key='weekly:2026-W28',
    )

    assert snapshot['team_id'] == str(team_a.id)
    assert snapshot['allowed_team_ids'] == [str(team_a.id)]
    assert snapshot['output_visibility_scope'] == 'team'
    assert snapshot['output_team_id'] == str(team_a.id)
    change_memory_ids = {change['memory_id'] for change in snapshot['changes']}
    assert str(project_memory.id) in change_memory_ids
    assert str(team_a_memory.id) in change_memory_ids
    assert str(team_b_memory.id) not in change_memory_ids


@pytest.mark.django_db
def test_weekly_project_output_excludes_team_private_sources() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-project-excl')
    team = _make_team(organization, project, 'weekly-project-excl')
    project_memory, _project_version = _make_memory(organization, project, title='Project', body='body-project')
    team_memory, _team_version = _make_memory(
        organization,
        project,
        title='Team',
        body='body-team',
        team=team,
        visibility=VisibilityScope.TEAM,
    )
    window_start, window_end = _weekly_window()

    snapshot = digest_work.freeze_weekly_digest_input(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        window_start=window_start,
        window_end=window_end,
        schedule_key='weekly:2026-W28',
    )

    change_memory_ids = {change['memory_id'] for change in snapshot['changes']}
    assert str(project_memory.id) in change_memory_ids
    assert str(team_memory.id) not in change_memory_ids


@pytest.mark.django_db
def test_weekly_rejects_team_not_linked_to_project() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-unlinked-team')
    unlinked_team = Team.objects.create(organization=organization, name='Unlinked', slug='weekly-unlinked')
    _make_memory(organization, project, title='Project', body='body-project')
    window_start, window_end = _weekly_window()

    from engram.memory.workflow_work import WorkflowWorkScopeError

    with pytest.raises((ValueError, WorkflowWorkScopeError)):
        digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=unlinked_team.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key='weekly:2026-W28',
        )


@pytest.mark.django_db
def test_empty_authorized_daily_input_resolves_no_input_without_package_or_outbox() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-empty')
    schedule_key = 'daily:2026-07-10'
    window_start, window_end = _daily_window()

    with transaction.atomic():
        snapshot = digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
            max_sources=10,
        )
        work, created = digest_work.create_digest_work_and_signal(
            data=_daily_data(organization, project, snapshot, schedule_key),
            signal_task=generate_daily_digest_work_v1,
        )

    assert snapshot['eligible_source_count'] == 0
    assert snapshot['sources'] == []
    assert created is True
    assert work.disposition == WorkflowWorkDisposition.NO_OP
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT
    assert CeleryOutbox.objects.count() == 0
    assert WorkflowRun.objects.count() == 0
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


@pytest.mark.django_db
def test_empty_authorized_weekly_input_resolves_no_input_without_outbox() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-empty')
    schedule_key = 'weekly:2026-W28'
    window_start, window_end = _weekly_window()

    with transaction.atomic():
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
        )
        work, created = digest_work.create_digest_work_and_signal(
            data=_weekly_data(organization, project, None, snapshot, schedule_key),
            signal_task=generate_weekly_digest_work_v1,
        )

    assert snapshot['changes'] == []
    assert created is True
    assert work.disposition == WorkflowWorkDisposition.NO_OP
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT
    assert CeleryOutbox.objects.count() == 0
    assert WorkflowRun.objects.count() == 0
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


@pytest.mark.django_db
def test_signal_failure_rolls_back_daily_work_run_and_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-rollback')
    _make_memory(organization, project, title='Alpha', body='body-a')
    schedule_key = 'daily:2026-07-10'
    window_start, window_end = _daily_window()

    def fail(*args: object, **kwargs: object) -> object:
        raise _SignalFailureError('signal dispatch failed')

    monkeypatch.setattr(generate_daily_digest_work_v1, 'apply_async', fail)

    with pytest.raises(_SignalFailureError):
        with transaction.atomic():
            snapshot = digest_work.freeze_daily_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
                max_sources=10,
            )
            run = WorkflowRun.objects.create(
                organization=organization,
                project=project,
                run_type=WorkflowRunType.DAILY_DIGEST,
                status=WorkflowRunStatus.QUEUED,
            )
            digest_work.create_digest_work_and_signal(
                data=_daily_data(organization, project, snapshot, schedule_key),
                signal_task=generate_daily_digest_work_v1,
                workflow_run=run,
            )

    assert WorkflowWork.objects.count() == 0
    assert WorkflowRun.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_signal_failure_rolls_back_weekly_work_and_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('weekly-rollback')
    _make_memory(organization, project, title='Alpha', body='body-a')
    schedule_key = 'weekly:2026-W28'
    window_start, window_end = _weekly_window()

    def fail(*args: object, **kwargs: object) -> object:
        raise _SignalFailureError('signal dispatch failed')

    monkeypatch.setattr(generate_weekly_digest_work_v1, 'apply_async', fail)

    with pytest.raises(_SignalFailureError):
        with transaction.atomic():
            snapshot = digest_work.freeze_weekly_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
            )
            digest_work.create_digest_work_and_signal(
                data=_weekly_data(organization, project, None, snapshot, schedule_key),
                signal_task=generate_weekly_digest_work_v1,
            )

    assert WorkflowWork.objects.count() == 0
    assert WorkflowRun.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0
