from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.management import call_command

from engram.core.management.commands.engram_export_memories import export_memories
from engram.core.models import (
    LinkType,
    Memory,
    MemoryLink,
    MemoryStatus,
    Organization,
    Project,
    Team,
    VisibilityScope,
)


@pytest.fixture
def f_scope() -> tuple[Organization, Project, Team]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    team = Team.objects.create(organization=organization, name='Core', slug='core')

    return organization, project, team


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    title: str = 'Memory',
    status: str = MemoryStatus.APPROVED,
    confidence: str = '0.900',
    stale: bool = False,
    refuted: bool = False,
    kind: str = '',
) -> Memory:
    return Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body='memory body',
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
        confidence=confidence,
        stale=stale,
        refuted=refuted,
        metadata={'kind': kind} if kind else {},
    )


@pytest.mark.django_db
def test_export_includes_trust_fields_and_links(
    f_scope: tuple[Organization, Project, Team],
) -> None:
    organization, project, team = f_scope
    memory = _make_memory(
        organization,
        project,
        team=team,
        title='Alpha',
        confidence='0.850',
        stale=True,
        refuted=True,
        kind='gotcha',
    )
    target = _make_memory(organization, project, title='Beta')
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type=LinkType.SUPERSEDED_BY,
        target=str(target.id),
    )

    payload = export_memories(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
    )

    serialized = {row['title']: row for row in payload['memories']}
    alpha = serialized['Alpha']

    assert alpha['confidence'] == '0.850'
    assert alpha['status'] == MemoryStatus.APPROVED
    assert alpha['stale'] is True
    assert alpha['refuted'] is True
    assert alpha['kind'] == 'gotcha'
    assert alpha['team_id'] == str(team.id)
    assert len(alpha['links']) == 1

    link = alpha['links'][0]

    assert link['link_type'] == LinkType.SUPERSEDED_BY
    assert link['target'] == str(target.id)
    assert 'created_at' in link


@pytest.mark.django_db
def test_export_defaults_to_approved_only(
    f_scope: tuple[Organization, Project, Team],
) -> None:
    organization, project, _team = f_scope
    _make_memory(organization, project, title='ApprovedRow', status=MemoryStatus.APPROVED)
    _make_memory(organization, project, title='RefutedRow', status=MemoryStatus.REFUTED, refuted=True)

    payload = export_memories(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
    )

    titles = {row['title'] for row in payload['memories']}

    assert titles == {'ApprovedRow'}


@pytest.mark.django_db
def test_export_all_statuses_flag_includes_refuted_status_rows(
    f_scope: tuple[Organization, Project, Team],
) -> None:
    organization, project, _team = f_scope
    _make_memory(organization, project, title='ApprovedRow', status=MemoryStatus.APPROVED)
    _make_memory(organization, project, title='RefutedRow', status=MemoryStatus.REFUTED, refuted=True)

    payload = export_memories(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        all_statuses=True,
    )

    titles = {row['title'] for row in payload['memories']}

    assert titles == {'ApprovedRow', 'RefutedRow'}


@pytest.mark.django_db
def test_command_writes_backup_with_all_statuses_flag(
    f_scope: tuple[Organization, Project, Team],
    tmp_path: Path,
) -> None:
    organization, project, _team = f_scope
    _make_memory(organization, project, title='ApprovedRow')
    _make_memory(organization, project, title='RefutedRow', status=MemoryStatus.REFUTED, refuted=True)
    output_path = tmp_path / 'backup.json'

    call_command(
        'engram_export_memories',
        '--organization-id',
        str(organization.id),
        '--project-id',
        str(project.id),
        '--output',
        str(output_path),
        '--all-statuses',
    )

    payload = json.loads(output_path.read_text(encoding='utf-8'))

    assert payload['memory_count'] == 2

    titles = {row['title'] for row in payload['memories']}

    assert titles == {'ApprovedRow', 'RefutedRow'}
