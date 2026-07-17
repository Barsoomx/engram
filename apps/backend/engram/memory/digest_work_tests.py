from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest import mock

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    MemoryVersionSource,
    Organization,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.invariant_queries import InvariantId, InvariantState, evaluate_invariants
from engram.memory.services import MemoryWorkerError, weekly_digest_content_hash
from engram.memory.tasks import generate_daily_digest_work_v1, generate_weekly_digest_work_v1
from engram.memory.transitions import (
    PromoteMemoryCandidate,
    ReviseMemory,
    ReviseMemoryInput,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import execution_configuration_fingerprint
from engram.memory.work_failures import CONFIGURATION, INVALID_INPUT, PROVIDER_TRANSIENT
from engram.memory.workflow_work import CreateWorkflowWorkInput
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import FakeProviderGateway, ProviderCallInput, ProviderCallResult

_DAILY_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'
_WEEKLY_TASK_NAME = 'engram.memory.generate_weekly_digest_work_v1'
_OWNER_RE = re.compile(r'^[^:]+:[0-9]+:[0-9a-f-]{36}$')


def _digest_works() -> object:
    return WorkflowWork.objects.filter(
        work_type__in=(WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST),
    )


def _digest_outbox() -> object:
    return CeleryOutbox.objects.filter(task_name__in=(_DAILY_TASK_NAME, _WEEKLY_TASK_NAME))


def _digest_runs() -> object:
    return WorkflowRun.objects.filter(run_type__in=(WorkflowRunType.DAILY_DIGEST, WorkflowRunType.WEEKLY_DIGEST))


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
    candidate, _source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='digest-source',
        title=title,
        body=body,
        visibility_scope=visibility,
        kind=kind,
    )
    result = PromoteMemoryCandidate().execute(transition_request(candidate))
    memory = result.memory
    version = result.memory_version
    if status != MemoryStatus.APPROVED:
        Memory.objects.filter(id=memory.id).update(status=status)
        memory.refresh_from_db()

    return memory, version


def _make_legacy_memory(
    organization: Organization,
    project: Project,
    *,
    title: str,
    body: str,
) -> tuple[Memory, MemoryVersion]:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
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
    assert _digest_works().count() == 1
    assert _digest_outbox().count() == 1
    frozen = _digest_works().get()
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
    assert _digest_works().count() == 1
    assert _digest_outbox().count() == 1
    frozen = _digest_works().get()
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
    assert _digest_works().count() == 1
    assert _digest_outbox().count() == 1
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
    assert _digest_works().count() == 1
    assert _digest_outbox().count() == 1
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
def test_daily_project_digest_admits_project_visible_source_from_associated_team() -> None:
    organization, project = _make_scope('daily-project-team-source')
    team = _make_team(organization, project, 'daily-project-team-source')
    _create_digest_policy(organization, project)
    source_memory, source_version = _make_memory(
        organization,
        project,
        title='Project-visible team source',
        body='project-visible team body',
        team=team,
        visibility=VisibilityScope.PROJECT,
    )
    work, _snapshot = _make_daily_work(organization, project)

    result = generate_daily_digest_work_v1(str(work.id))

    digest = Memory.objects.get(project=project, kind='digest')
    digest_version = MemoryVersion.objects.get(memory=digest)
    provenance = MemoryVersionSource.objects.get(memory_version=digest_version)
    assert result == str(_v1_runs(work).get().id)
    assert digest.team_id is None
    assert digest.metadata['source_memory_ids'] == [str(source_memory.id)]
    assert provenance.source_memory_version_id == source_version.id
    p7 = next(
        result
        for result in evaluate_invariants(organization_id=organization.id, project_id=project.id)
        if result.invariant_id == InvariantId.P7
    )
    assert p7.state == InvariantState.HEALTHY


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

    assert _digest_works().count() == 0
    assert _digest_outbox().count() == 0


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
@pytest.mark.parametrize('work_type', (WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST))
def test_digest_snapshot_admits_only_transition_owned_sources(work_type: str) -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope(f'{work_type}-typed-admission')
    legacy_memory, _legacy_version = _make_legacy_memory(
        organization,
        project,
        title='Legacy source',
        body='legacy body',
    )
    typed_memory, _typed_version = _make_memory(
        organization,
        project,
        title='Typed source',
        body='typed body',
    )
    window_start, window_end = _daily_window() if work_type == WorkflowWorkType.DAILY_DIGEST else _weekly_window()

    if work_type == WorkflowWorkType.DAILY_DIGEST:
        snapshot = digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key='daily:typed-admission',
            max_sources=10,
        )
        refs = snapshot['sources']
        assert snapshot['eligible_source_count'] == 1
    else:
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=window_start,
            window_end=window_end,
            schedule_key='weekly:typed-admission',
        )
        refs = snapshot['changes']

    assert {ref['memory_id'] for ref in refs} == {str(typed_memory.id)}
    legacy_memory.refresh_from_db()
    assert legacy_memory.transition_contract_version == 0
    assert legacy_memory.current_transition_id is None


@pytest.mark.django_db
@pytest.mark.parametrize('work_type', (WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST))
def test_all_legacy_digest_input_resolves_no_input_without_publication(work_type: str) -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope(f'{work_type}-legacy-no-input')
    legacy_memory, _legacy_version = _make_legacy_memory(
        organization,
        project,
        title='Legacy source',
        body='legacy body',
    )
    schedule_key = f'{work_type}:legacy-no-input'
    window_start, window_end = _daily_window() if work_type == WorkflowWorkType.DAILY_DIGEST else _weekly_window()

    with transaction.atomic():
        if work_type == WorkflowWorkType.DAILY_DIGEST:
            snapshot = digest_work.freeze_daily_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
                max_sources=10,
            )
            refs = snapshot['sources']
            data = _daily_data(organization, project, snapshot, schedule_key)
            signal_task = generate_daily_digest_work_v1
        else:
            snapshot = digest_work.freeze_weekly_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
            )
            refs = snapshot['changes']
            data = _weekly_data(organization, project, None, snapshot, schedule_key)
            signal_task = generate_weekly_digest_work_v1

        work, created = digest_work.create_digest_work_and_signal(data=data, signal_task=signal_task)

    assert refs == []
    assert created is True
    assert work.disposition == WorkflowWorkDisposition.NO_OP
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT
    assert _digest_outbox().count() == 0
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    legacy_memory.refresh_from_db()
    assert legacy_memory.transition_contract_version == 0
    assert legacy_memory.current_transition_id is None


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
    assert _digest_outbox().count() == 0
    assert _digest_runs().count() == 0
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
    assert _digest_outbox().count() == 0
    assert _digest_runs().count() == 0
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

    assert _digest_works().count() == 0
    assert _digest_runs().count() == 0
    assert _digest_outbox().count() == 0


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

    assert _digest_works().count() == 0
    assert _digest_runs().count() == 0
    assert _digest_outbox().count() == 0


def _create_digest_policy(organization: Organization, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='Org Digest OpenAI',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=None,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-digest-secret',
        hmac_digest='digest-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='Digest policy',
        scope='project',
        task_type='digest',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


class _CountingDigestGateway:
    def __init__(self, calls: list[ProviderCallInput]) -> None:
        self._calls = calls
        self._delegate = FakeProviderGateway()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self._calls.append(data)

        return self._delegate.call(data)

    def embed(self, data: object) -> object:
        return self._delegate.embed(data)


def _counting_gateway(calls: list[ProviderCallInput]) -> object:
    def stub(policy: object, **_kwargs: object) -> object:
        return _CountingDigestGateway(calls)

    return stub


class _MutatingDigestGateway:
    def __init__(self, version: MemoryVersion, calls: list[ProviderCallInput]) -> None:
        self._version = version
        self._calls = calls
        self._delegate = FakeProviderGateway()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self._calls.append(data)
        MemoryVersion.objects.filter(id=self._version.id).update(body='body mutated mid-provider-call')

        return self._delegate.call(data)

    def embed(self, data: object) -> object:
        return self._delegate.embed(data)


class _AdvancingDigestGateway:
    def __init__(self, memory: Memory, calls: list[ProviderCallInput]) -> None:
        self._memory = memory
        self._calls = calls
        self._delegate = FakeProviderGateway()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self._calls.append(data)
        self._memory.refresh_from_db()
        ReviseMemory().execute(
            ReviseMemoryInput(
                request=TransitionRequest(
                    scope=TransitionScope(
                        organization_id=self._memory.organization_id,
                        project_id=self._memory.project_id,
                        team_id=self._memory.team_id,
                    ),
                    idempotency_key=f'test:digest-source-advance:{self._memory.id}:v2',
                    actor_type='system',
                    actor_id='digest-work-test',
                    capability='memories:write',
                    request_id=f'test:digest-source-advance:{self._memory.id}',
                    correlation_id=f'test:digest-source-advance:{self._memory.id}',
                    reason='advance source during digest provider call',
                    origin='digest-work-test',
                ),
                memory_fence=build_memory_fence(self._memory),
                title=self._memory.title,
                body='body-a-v2',
            )
        )

        return ProviderCallResult(
            provider='openai',
            model='gpt-4.1-mini',
            call_record_id=self._memory.id,
            redaction_state='clean',
            generated_title='Frozen V1 digest',
            generated_body='Digest body rendered from frozen V1',
        )

    def embed(self, data: object) -> object:
        return self._delegate.embed(data)


def _make_daily_work(
    organization: Organization,
    project: Project,
    schedule_key: str = 'daily:2026-07-10',
    max_sources: int = 200,
) -> tuple[WorkflowWork, dict[str, object]]:
    digest_work = _load_digest_work()
    window_start, window_end = _daily_window()
    with transaction.atomic():
        snapshot = digest_work.freeze_daily_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
            max_sources=max_sources,
        )
        work, _created = digest_work.create_digest_work_and_signal(
            data=_daily_data(organization, project, snapshot, schedule_key),
            signal_task=generate_daily_digest_work_v1,
        )

    return work, snapshot


def _make_weekly_work(
    organization: Organization,
    project: Project,
    team: Team | None = None,
    schedule_key: str = 'weekly:2026-W28',
) -> tuple[WorkflowWork, dict[str, object]]:
    digest_work = _load_digest_work()
    window_start, window_end = _weekly_window()
    with transaction.atomic():
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id if team is not None else None,
            window_start=window_start,
            window_end=window_end,
            schedule_key=schedule_key,
        )
        work, _created = digest_work.create_digest_work_and_signal(
            data=_weekly_data(organization, project, team, snapshot, schedule_key),
            signal_task=generate_weekly_digest_work_v1,
        )

    return work, snapshot


@pytest.mark.django_db
def test_daily_work_execution_rejects_frozen_input_digest_drift_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-input-digest-drift')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    tampered = dict(work.input_snapshot)
    tampered['input_digest'] = 'deadbeef' * 8
    WorkflowWork.objects.filter(id=work.id).update(input_snapshot=tampered)

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    result = generate_daily_digest_work_v1(str(work.id))

    assert calls == []
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    assert result == str(work.id)
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_fingerprint_mismatch'
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert work.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_daily_work_execution_rejects_source_body_drift_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-body-drift')
    _create_digest_policy(organization, project)
    _memory, version = _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    MemoryVersion.objects.filter(id=version.id).update(body='body-a tampered at rest')

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    with pytest.raises(MemoryWorkerError, match='body digest'):
        generate_daily_digest_work_v1(str(work.id))

    assert calls == []
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_scope_invalid'


@pytest.mark.django_db
def test_weekly_work_execution_rejects_source_body_drift_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('weekly-body-drift')
    _memory, version = _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_weekly_work(organization, project)

    MemoryVersion.objects.filter(id=version.id).update(body='body-a tampered at rest')

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    with pytest.raises(MemoryWorkerError, match='body digest'):
        generate_weekly_digest_work_v1(str(work.id))

    assert calls == []
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_scope_invalid'


@pytest.mark.django_db
def test_weekly_team_work_fails_closed_when_project_team_link_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('weekly-team-unlink')
    team = _make_team(organization, project, 'weekly-team-unlink')
    _make_memory(
        organization,
        project,
        title='TeamA',
        body='body-team-a',
        team=team,
        visibility=VisibilityScope.TEAM,
    )
    work, _snapshot = _make_weekly_work(organization, project, team)

    ProjectTeam.objects.filter(organization=organization, project=project, team=team).delete()

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    with pytest.raises(MemoryWorkerError, match='team'):
        generate_weekly_digest_work_v1(str(work.id))

    assert calls == []
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_scope_invalid'


@pytest.mark.django_db
def test_daily_work_discards_provider_result_when_source_invalidated_after_precall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-invalidate')
    _create_digest_policy(organization, project)
    _memory, version = _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    calls: list[ProviderCallInput] = []

    def stub(policy: object, **_kwargs: object) -> object:
        if getattr(policy, 'task_type', None) == 'digest':
            return _MutatingDigestGateway(version, calls)

        return FakeProviderGateway()

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', stub)

    with pytest.raises(MemoryWorkerError):
        generate_daily_digest_work_v1(str(work.id))

    assert len(calls) == 1
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    assert MemoryVersion.objects.filter(memory__project=project, memory__kind='digest').count() == 0
    assert RetrievalDocument.objects.filter(project=project, memory__kind='digest').count() == 0


@pytest.mark.django_db
def test_daily_work_publishes_frozen_v1_body_and_provenance_when_source_advances_during_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-frozen-v1')
    _create_digest_policy(organization, project)
    source_memory, source_v1 = _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    calls: list[ProviderCallInput] = []

    def stub(policy: object, **_kwargs: object) -> object:
        if getattr(policy, 'task_type', None) == 'digest':
            return _AdvancingDigestGateway(source_memory, calls)

        return FakeProviderGateway()

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', stub)

    result = generate_daily_digest_work_v1(str(work.id))

    digest = Memory.objects.get(project=project, kind='digest')
    digest_version = MemoryVersion.objects.get(memory=digest)
    provenance = MemoryVersionSource.objects.get(memory_version=digest_version)
    assert result == str(_v1_runs(work).get().id)
    assert len(calls) == 1
    assert 'body-a' in calls[0].prompt
    assert 'body-a-v2' not in calls[0].prompt
    assert digest.title == 'Digest Frozen V1 digest'
    assert digest.body == 'Digest body rendered from frozen V1'
    assert digest.metadata['source_memory_ids'] == [str(source_memory.id)]
    assert provenance.source_memory_version_id == source_v1.id


@pytest.mark.django_db
def test_daily_work_publishes_output_atomically_without_embedding_under_locks() -> None:
    organization, project = _make_scope('daily-publish')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    result = generate_daily_digest_work_v1(str(work.id))

    assert result == str(_v1_runs(work).get().id)
    digests = Memory.objects.filter(project=project, kind='digest')
    assert digests.count() == 1
    digest_memory = digests.get()
    versions = MemoryVersion.objects.filter(memory=digest_memory)
    assert versions.count() == 1
    assert versions.get().version == 1
    document = RetrievalDocument.objects.get(memory=digest_memory)
    assert not document.embedding_vector
    assert document.embedding_reference == ''
    assert (
        WorkflowWork.objects.filter(
            work_type=WorkflowWorkType.MEMORY_EMBEDDING,
            subject_id=document.id,
        ).count()
        == 1
    )
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_weekly_work_publishes_output_atomically_without_embedding_under_locks() -> None:
    organization, project = _make_scope('weekly-publish')
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_weekly_work(organization, project)

    result = generate_weekly_digest_work_v1(str(work.id))

    assert result == str(_v1_runs(work).get().id)
    digests = Memory.objects.filter(project=project, kind='digest')
    assert digests.count() == 1
    digest_memory = digests.get()
    versions = MemoryVersion.objects.filter(memory=digest_memory)
    assert versions.count() == 1
    assert versions.get().version == 1
    document = RetrievalDocument.objects.get(memory=digest_memory)
    assert not document.embedding_vector
    assert document.embedding_reference == ''
    assert (
        WorkflowWork.objects.filter(
            work_type=WorkflowWorkType.MEMORY_EMBEDDING,
            subject_id=document.id,
        ).count()
        == 1
    )
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_daily_work_second_automatic_delivery_reuses_output_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-reuse')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    first = generate_daily_digest_work_v1(str(work.id))
    second = generate_daily_digest_work_v1(str(work.id))

    assert first == str(_v1_runs(work).get().id)
    assert second == str(work.id)
    assert len(calls) == 1
    assert Memory.objects.filter(project=project, kind='digest').count() == 1


@pytest.mark.django_db
def test_execute_frozen_digest_work_returns_none_for_terminal_no_op_daily_work() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-noop-exec')
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
        work, _created = digest_work.create_digest_work_and_signal(
            data=_daily_data(organization, project, snapshot, schedule_key),
            signal_task=generate_daily_digest_work_v1,
        )

    assert work.disposition == WorkflowWorkDisposition.NO_OP

    result = digest_work.execute_frozen_digest_work(work, None)

    assert result is None
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


def _linked_daily_run(organization: Organization, project: Project, work: WorkflowWork) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization=organization,
        project=project,
        work=work,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.QUEUED,
        input_snapshot=work.input_snapshot,
        request_id=f'daily-digest:{work.id}',
        correlation_id=f'daily-digest:{work.id}',
    )


def _digest_output_for_work(project: Project, work: WorkflowWork) -> int:
    return Memory.objects.filter(
        project=project,
        kind='digest',
        metadata__digest_visibility__workflow_work_id=str(work.id),
    ).count()


@pytest.mark.django_db
def test_daily_work_explicit_v1_run_ends_succeeded_with_result_memory_id() -> None:
    organization, project = _make_scope('daily-run-succeeded')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    run = queue_work_attempt(
        work_id=work.id,
        now=timezone.now(),
        origin=WorkflowRunOrigin.RECONCILIATION,
    )

    result = generate_daily_digest_work_v1(str(work.id), str(run.id))

    run.refresh_from_db()
    digest = Memory.objects.get(project=project, kind='digest')
    assert result == str(run.id)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.result_memory_id == digest.id
    assert run.finished_at is not None


@pytest.mark.django_db
def test_daily_work_explicit_v1_frozen_drift_terminalizes_at_claim_without_provider() -> None:
    organization, project = _make_scope('daily-run-failed')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    run = queue_work_attempt(
        work_id=work.id,
        now=timezone.now(),
        origin=WorkflowRunOrigin.RECONCILIATION,
    )

    tampered = dict(work.input_snapshot)
    tampered['input_digest'] = 'deadbeef' * 8
    WorkflowWork.objects.filter(id=work.id).update(input_snapshot=tampered)

    result = generate_daily_digest_work_v1(str(work.id), str(run.id))

    assert result == str(work.id)
    run.refresh_from_db()
    assert run.status == WorkflowRunStatus.QUEUED
    terminal = _v1_runs(work).exclude(id=run.id).get()
    assert terminal.status == WorkflowRunStatus.FAILED
    assert terminal.failure_class == INVALID_INPUT
    assert terminal.failure_code == 'work_fingerprint_mismatch'
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


@pytest.mark.django_db
def test_daily_explicit_v1_attempt_is_claimed_and_completed_with_a_fence() -> None:
    organization, project = _make_scope('daily-explicit-v1-fence')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    run = queue_work_attempt(
        work_id=work.id,
        now=timezone.now(),
        origin=WorkflowRunOrigin.RECONCILIATION,
    )

    result = generate_daily_digest_work_v1(str(work.id), str(run.id))

    work.refresh_from_db()
    run.refresh_from_db()
    assert result == str(run.id)
    assert run.execution_contract_version == 1
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.fencing_token == work.fencing_token == 1
    assert run.lease_owner != ''
    assert run.heartbeat_at is not None
    assert run.lease_expires_at is not None


@pytest.mark.django_db
def test_daily_explicit_delivery_rejects_legacy_v0_before_domain_access() -> None:
    organization, project = _make_scope('daily-explicit-v0-rejected')
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    run = _linked_daily_run(organization, project, work)

    with mock.patch(
        'engram.memory.digest_work.execute_frozen_digest_work',
        return_value=None,
    ) as m_execute:
        with pytest.raises(ValueError, match='queued v1 attempt'):
            generate_daily_digest_work_v1(str(work.id), str(run.id))

    m_execute.assert_not_called()
    work.refresh_from_db()
    run.refresh_from_db()
    assert run.execution_contract_version == 0
    assert run.status == WorkflowRunStatus.QUEUED
    assert run.fencing_token is None
    assert work.fencing_token == 0


@pytest.mark.django_db
def test_daily_work_automatic_path_creates_no_run_and_publishes_once() -> None:
    organization, project = _make_scope('daily-run-automatic')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    result = generate_daily_digest_work_v1(str(work.id))

    run = _v1_runs(work).get()
    assert result == str(run.id)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert _digest_output_for_work(project, work) == 1


@pytest.mark.django_db
def test_daily_work_second_delivery_past_existing_check_publishes_one_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-double-publish')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    monkeypatch.setattr('engram.memory.digest_work._existing_output', lambda _work: None)

    digest_work.execute_frozen_digest_work(work, None)
    work.refresh_from_db()
    digest_work.execute_frozen_digest_work(work, None)

    assert _digest_output_for_work(project, work) == 1


@pytest.mark.django_db
def test_weekly_work_publishes_legacy_metadata_block() -> None:
    organization, project = _make_scope('weekly-metadata')
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, snapshot = _make_weekly_work(organization, project)

    generate_weekly_digest_work_v1(str(work.id))

    digest = Memory.objects.get(project=project, kind='digest')
    metadata = digest.metadata
    window_start = datetime.fromisoformat(str(snapshot['window_start']).replace('Z', '+00:00'))
    window_end = datetime.fromisoformat(str(snapshot['window_end']).replace('Z', '+00:00'))
    expected_hash = weekly_digest_content_hash(project.id, window_start, window_end, None)

    assert metadata['window_start'] is not None
    assert metadata['window_end'] is not None
    assert metadata['window_days'] == 7
    assert isinstance(metadata['counts'], dict)
    assert isinstance(metadata['memory_changes'], dict)
    assert metadata['content_hash'] == expected_hash


# ---------------------------------------------------------------------------
# C2.1 Zone-D digest execution-registry cutover (RED)
#
# These specify the NEW registry-backed behavior of the digest adapters. Each
# automatic delivery must lease through claim_work (240s), append a v1
# WorkflowRun, then either publish + finish_work_claim inside execute_frozen_
# digest_work's publication TX2 (success) or translate_failure + fail_work_claim
# at the adapter boundary (failure). The legacy no-claim behavior of
# execute_frozen_digest_work stays byte-identical when claim is None.
# ---------------------------------------------------------------------------


class _RaisingDigestGateway:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self._delegate = FakeProviderGateway()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise self._error

    def embed(self, data: object) -> object:
        return self._delegate.embed(data)


def _raising_gateway(error: Exception) -> object:
    def stub(policy: object, **_kwargs: object) -> object:
        return _RaisingDigestGateway(error)

    return stub


def _v1_runs(work: WorkflowWork) -> object:
    return WorkflowRun.objects.filter(work=work, execution_contract_version=1)


@pytest.mark.django_db
def test_daily_automatic_delivery_leases_and_settles_with_run_evidence() -> None:
    organization, project = _make_scope('daily-lease-settle')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    generate_daily_digest_work_v1(str(work.id))

    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    digest = Memory.objects.get(project=project, kind='digest')
    assert run.origin == WorkflowRunOrigin.AUTOMATIC
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.fencing_token == 1
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.result_memory_id == digest.id
    assert _OWNER_RE.match(run.lease_owner) is not None

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    assert work.fencing_token == 1
    assert work.lease_owner == ''
    assert work.lease_expires_at is None
    assert work.heartbeat_at is None
    assert work.next_retry_at is None


@pytest.mark.django_db
def test_weekly_automatic_delivery_leases_and_settles_with_run_evidence() -> None:
    organization, project = _make_scope('weekly-lease-settle')
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_weekly_work(organization, project)

    generate_weekly_digest_work_v1(str(work.id))

    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    digest = Memory.objects.get(project=project, kind='digest')
    assert run.origin == WorkflowRunOrigin.AUTOMATIC
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.fencing_token == 1
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.result_memory_id == digest.id
    assert _OWNER_RE.match(run.lease_owner) is not None

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    assert work.fencing_token == 1
    assert work.lease_owner == ''
    assert work.lease_expires_at is None


@pytest.mark.django_db
def test_daily_automatic_provider_transient_records_retry_wait_without_self_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-provider-transient')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    error = ModelPolicyError('provider_http_error', 'rate limited', retryable=True, http_status=429)
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _raising_gateway(error))
    m_retry = mock.Mock(side_effect=AssertionError('self.retry must not be scheduled for domain failures'))

    with mock.patch.object(generate_daily_digest_work_v1, 'retry', m_retry):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            generate_daily_digest_work_v1(str(work.id))

    m_retry.assert_not_called()
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == PROVIDER_TRANSIENT
    assert run.failure_code == 'provider_rate_limited'

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.failure_streak == 1
    assert work.next_retry_at is not None
    delay = (work.next_retry_at - run.finished_at).total_seconds()
    assert 29 <= delay <= 31
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


@pytest.mark.django_db
def test_daily_automatic_configuration_failure_blocks_with_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-config-blocked')
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)
    expected_fingerprint = execution_configuration_fingerprint(work)
    m_retry = mock.Mock(side_effect=AssertionError('configuration failure must not self.retry'))

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _raising_gateway(RuntimeError('unused')))

    with mock.patch.object(generate_daily_digest_work_v1, 'retry', m_retry):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            generate_daily_digest_work_v1(str(work.id))

    m_retry.assert_not_called()
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == CONFIGURATION
    assert run.failure_code == 'model_policy_unavailable'
    assert run.configuration_fingerprint == expected_fingerprint

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.blocked_configuration_fingerprint == expected_fingerprint
    assert work.next_retry_at is None


@pytest.mark.django_db
def test_daily_second_automatic_delivery_absorbs_at_claim_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-reuse-claim')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    generate_daily_digest_work_v1(str(work.id))
    generate_daily_digest_work_v1(str(work.id))

    assert len(calls) == 1
    assert Memory.objects.filter(project=project, kind='digest').count() == 1
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.SUCCEEDED

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE


@pytest.mark.django_db
def test_daily_non_required_publish_branch_preserves_terminal_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engram.memory.work_execution import claim_work

    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-resolved-elsewhere')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    claimed = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.DAILY_DIGEST,
        lease_owner='host:1:00000000-0000-4000-8000-000000000001',
        now=timezone.now(),
        lease_for=timedelta(seconds=240),
    )
    resolved_at = timezone.now()
    WorkflowWork.objects.filter(id=work.id).update(
        disposition=WorkflowWorkDisposition.COMPLETE,
        resolution_reason=WorkflowWorkResolutionReason.NO_SIGNAL,
        resolved_at=resolved_at,
    )
    monkeypatch.setattr('engram.memory.digest_work._existing_output', lambda _work: None)

    result = digest_work.execute_frozen_digest_work(work, None, claimed.claim)

    assert result is None
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL
    assert work.resolved_at == resolved_at
    assert Memory.objects.filter(project=project, kind='digest').count() == 0
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.SUCCEEDED


@pytest.mark.django_db
def test_daily_source_body_drift_under_claim_is_terminal_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, project = _make_scope('daily-body-drift-terminal')
    _create_digest_policy(organization, project)
    _memory, version = _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    MemoryVersion.objects.filter(id=version.id).update(body='body-a tampered at rest')

    calls: list[ProviderCallInput] = []
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', _counting_gateway(calls))

    with pytest.raises(MemoryWorkerError, match='body digest'):
        generate_daily_digest_work_v1(str(work.id))

    assert calls == []
    assert _v1_runs(work).count() == 1
    run = _v1_runs(work).get()
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_scope_invalid'

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.next_retry_at is None
    assert Memory.objects.filter(project=project, kind='digest').count() == 0


@pytest.mark.django_db
def test_execute_frozen_digest_work_without_claim_publishes_via_legacy_resolver() -> None:
    digest_work = _load_digest_work()
    organization, project = _make_scope('daily-legacy-noclaim')
    _create_digest_policy(organization, project)
    _make_memory(organization, project, title='Alpha', body='body-a')
    work, _snapshot = _make_daily_work(organization, project)

    result = digest_work.execute_frozen_digest_work(work, None)

    digest = Memory.objects.get(project=project, kind='digest')
    assert result == digest.id
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 0

    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
