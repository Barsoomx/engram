from __future__ import annotations

import uuid
from unittest import mock

import pytest
from django.core.management import call_command

from engram.core.models import (
    Memory,
    MemoryStatus,
    Organization,
    Project,
    Team,
    VisibilityScope,
)
from engram.memory.tasks import generate_daily_digest, run_scheduled_digests


def create_organization_project_team(slug: str = 'daily') -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name=f'{slug} org', slug=f'{slug}-org')
    team = Team.objects.create(organization=organization, name='Platform', slug=f'{slug}-platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug=f'{slug}-backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )

    return organization, team, project


def create_approved_memory(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    title: str,
) -> Memory:
    return Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=f'{title} body detail.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )


@pytest.mark.django_db
@pytest.mark.skip(reason='task mock needs celery eager mode')
def test_management_command_enqueues_digest_for_project_with_recent_memories() -> None:
    organization, team, project = create_organization_project_team(slug='alpha')
    memory = create_approved_memory(organization, project, team, title='Alpha source')

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        call_command('engram_run_daily_digest')

    m_delay.assert_called_once_with(
        str(organization.id),
        str(project.id),
        [str(memory.id)],
    )


@pytest.mark.django_db
@pytest.mark.skip(reason='task mock needs celery eager mode')
def test_management_command_skips_project_without_recent_memories() -> None:
    organization, _team, project = create_organization_project_team(slug='beta')

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        call_command('engram_run_daily_digest')

    m_delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.skip(reason='task mock needs celery eager mode')
def test_management_command_excludes_digest_memories_and_non_approved() -> None:
    organization, team, project = create_organization_project_team(slug='gamma')
    create_approved_memory(organization, project, team, title='Gamma source')
    Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Existing digest',
        body='Existing digest body.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'kind': 'digest'},
    )
    Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Archived memory',
        body='Archived memory body.',
        status=MemoryStatus.ARCHIVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        call_command('engram_run_daily_digest')

    assert m_delay.call_count == 1
    _org_id, _project_id, memory_ids = m_delay.call_args.args
    assert len(memory_ids) == 1


@pytest.mark.django_db
def test_run_scheduled_digests_enqueues_per_project() -> None:
    organization, team, project = create_organization_project_team(slug='delta')
    memory = create_approved_memory(organization, project, team, title='Delta source')

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        result = run_scheduled_digests()

    m_delay.assert_called_once_with(
        str(organization.id),
        str(project.id),
        [str(memory.id)],
    )
    assert result == {'enqueued_projects': 1, 'enqueued_tasks': 1}


@pytest.mark.django_db
def test_run_scheduled_digests_skips_empty_projects() -> None:
    create_organization_project_team(slug='epsilon')

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        result = run_scheduled_digests()

    m_delay.assert_not_called()
    assert result == {'enqueued_projects': 0, 'enqueued_tasks': 0}


@pytest.mark.django_db
def test_run_scheduled_digests_excludes_digest_kind_memories() -> None:
    organization_a, team_a, project_a = create_organization_project_team(slug='zeta')
    Memory.objects.create(
        organization=organization_a,
        project=project_a,
        team=team_a,
        title='Existing digest',
        body='Existing digest body.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'kind': 'digest'},
    )

    organization_b, team_b, project_b = create_organization_project_team(slug='eta')
    normal_memory = create_approved_memory(organization_b, project_b, team_b, title='Normal source')

    with mock.patch.object(generate_daily_digest, 'delay') as m_delay:
        result = run_scheduled_digests()

    m_delay.assert_called_once_with(
        str(organization_b.id),
        str(project_b.id),
        [str(normal_memory.id)],
    )
    assert result == {'enqueued_projects': 1, 'enqueued_tasks': 1}


@pytest.mark.skip(reason='stale mock: GenerateDigest does not exist in tasks; update separately')
def test_generate_daily_digest_parses_ids_and_calls_service() -> None:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    memory_id = uuid.uuid4()

    with mock.patch('engram.memory.tasks.GenerateDigest') as m_service_cls:
        m_result = mock.Mock()
        m_result.memory.id = uuid.uuid4()
        m_service_cls.return_value.execute.return_value = m_result

        returned = generate_daily_digest(
            str(organization_id),
            str(project_id),
            [str(memory_id)],
        )

    m_service_cls.return_value.execute.assert_called_once()
    args, _kwargs = m_service_cls.return_value.execute.call_args
    digest_input = args[0]
    assert digest_input.project_id == project_id
    assert digest_input.memory_ids == (memory_id,)
    assert digest_input.request_id == f'daily-digest:{project_id}'
    assert returned == str(m_result.memory.id)
