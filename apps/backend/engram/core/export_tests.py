from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from django.core.management import call_command

from engram.core.export import iter_export_memories_json
from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)

LEAKED_TOKEN = 'egk_export_secret_0123456789abcdefghijklmnopqrstuvwxyz'


def create_organization_project_team() -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name='Export Org', slug='export-org')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )

    return organization, team, project


def create_approved_memory(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    title: str = 'Authorization before ranking',
    body: str = 'Authorization before ranking protects context bundles.',
    visibility_scope: str = VisibilityScope.PROJECT,
) -> tuple[Memory, MemoryVersion, RetrievalDocument]:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        metadata={'exact_terms': ['authorization before ranking']},
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash=f'{title}-hash',
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=visibility_scope,
        file_paths=['apps/backend/engram/context/services.py'],
        symbols=['BuildContextBundle'],
        exact_terms=['context bundle'],
        full_text=f'{memory.title}\n\n{memory.body}',
    )

    return memory, version, document


def run_export(
    tmp_path: Any,
    organization: Organization,
    project: Project,
    team_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    output_path = tmp_path / 'export.json'

    call_command(
        'engram_export_memories',
        organization_id=str(organization.id),
        project_id=str(project.id),
        output=str(output_path),
        team_id=str(team_id) if team_id is not None else None,
    )

    with output_path.open(encoding='utf-8') as source:
        return json.load(source)


@pytest.mark.django_db
def test_export_writes_approved_memories_with_versions_and_retrieval_documents(tmp_path: Any) -> None:
    organization, team, project = create_organization_project_team()
    memory, version, document = create_approved_memory(organization, project, team)

    payload = run_export(tmp_path, organization, project)

    assert payload['organization_id'] == str(organization.id)
    assert payload['project_id'] == str(project.id)
    assert payload['team_id'] is None
    assert payload['exported_at']
    assert payload['memory_count'] == 1

    exported = payload['memories'][0]
    assert exported['id'] == str(memory.id)
    assert exported['title'] == memory.title
    assert exported['body'] == memory.body
    assert exported['visibility_scope'] == VisibilityScope.PROJECT
    assert exported['current_version'] == 1
    assert exported['created_at'] == memory.created_at.isoformat()

    exported_version = exported['versions'][0]
    assert exported_version['version'] == 1
    assert exported_version['body'] == version.body
    assert exported_version['content_hash'] == version.content_hash
    assert exported_version['source_observation_id'] is None

    exported_document = exported['retrieval_document']
    assert exported_document['file_paths'] == ['apps/backend/engram/context/services.py']
    assert exported_document['symbols'] == ['BuildContextBundle']
    assert exported_document['exact_terms'] == ['context bundle']
    assert exported_document['visibility_scope'] == VisibilityScope.PROJECT
    assert exported_document['full_text'] == f'{memory.title}\n\n{memory.body}'


@pytest.mark.django_db
def test_export_excludes_other_organization_and_other_project_memories(tmp_path: Any) -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(organization, project, team, title='Owned memory')

    other_organization = Organization.objects.create(name='Other Org', slug='other-org')
    other_team = Team.objects.create(organization=other_organization, name='Other', slug='other')
    other_project = Project.objects.create(organization=other_organization, name='Other', slug='other')
    create_approved_memory(other_organization, other_project, other_team, title='Other org memory')

    sibling_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    create_approved_memory(organization, sibling_project, team, title='Sibling project memory')

    payload = run_export(tmp_path, organization, project)

    exported_titles = [entry['title'] for entry in payload['memories']]
    assert exported_titles == ['Owned memory']
    assert payload['memory_count'] == 1


@pytest.mark.django_db
def test_export_with_team_id_only_includes_that_team_memories(tmp_path: Any) -> None:
    organization, team, project = create_organization_project_team()
    other_team = Team.objects.create(organization=organization, name='Security', slug='security')
    create_approved_memory(organization, project, team, title='Platform memory')
    create_approved_memory(organization, project, other_team, title='Security memory')

    payload = run_export(tmp_path, organization, project, team_id=team.id)

    exported_titles = [entry['title'] for entry in payload['memories']]
    assert exported_titles == ['Platform memory']
    assert payload['team_id'] == str(team.id)


@pytest.mark.django_db
def test_export_redacts_token_shaped_values_in_title_body_and_full_text(tmp_path: Any) -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(
        organization,
        project,
        team,
        title=f'Redact title {LEAKED_TOKEN}',
        body=f'Body leaks {LEAKED_TOKEN}.',
    )

    payload = run_export(tmp_path, organization, project)

    serialized = json.dumps(payload)
    assert LEAKED_TOKEN not in serialized
    assert '[REDACTED]' in serialized

    exported = payload['memories'][0]
    assert LEAKED_TOKEN not in exported['title']
    assert LEAKED_TOKEN not in exported['body']
    assert LEAKED_TOKEN not in exported['retrieval_document']['full_text']


def stream_export(
    organization: Organization,
    project: Project,
    team_id: uuid.UUID | None = None,
    all_statuses: bool = False,
) -> dict[str, Any]:
    chunks = list(
        iter_export_memories_json(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team_id,
            all_statuses=all_statuses,
        ),
    )

    return json.loads(''.join(chunks))


@pytest.mark.django_db
def test_stream_export_matches_pure_export_shape() -> None:
    organization, team, project = create_organization_project_team()
    memory, version, document = create_approved_memory(organization, project, team)

    payload = stream_export(organization, project)

    assert payload['organization_id'] == str(organization.id)
    assert payload['project_id'] == str(project.id)
    assert payload['team_id'] is None
    assert payload['exported_at']
    assert payload['memory_count'] == 1

    exported = payload['memories'][0]
    assert exported['id'] == str(memory.id)
    assert exported['title'] == memory.title
    assert exported['body'] == memory.body
    assert exported['versions'][0]['content_hash'] == version.content_hash
    assert exported['retrieval_document']['full_text'] == document.full_text


@pytest.mark.django_db
def test_stream_export_empty_project_is_valid_json() -> None:
    organization, _team, project = create_organization_project_team()

    payload = stream_export(organization, project)

    assert payload['memory_count'] == 0
    assert payload['memories'] == []


@pytest.mark.django_db
def test_stream_export_multiple_memories_are_comma_separated() -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(organization, project, team, title='Alpha memory')
    create_approved_memory(organization, project, team, title='Beta memory')
    create_approved_memory(organization, project, team, title='Gamma memory')

    payload = stream_export(organization, project)

    assert payload['memory_count'] == 3
    assert [entry['title'] for entry in payload['memories']] == [
        'Alpha memory',
        'Beta memory',
        'Gamma memory',
    ]


@pytest.mark.django_db
def test_stream_export_excludes_non_approved_unless_all_statuses() -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(organization, project, team, title='Approved memory')
    Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Archived memory',
        body='Archived body',
        status=MemoryStatus.ARCHIVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    approved_only = stream_export(organization, project)
    assert [entry['title'] for entry in approved_only['memories']] == ['Approved memory']

    everything = stream_export(organization, project, all_statuses=True)
    assert [entry['title'] for entry in everything['memories']] == [
        'Approved memory',
        'Archived memory',
    ]


@pytest.mark.django_db
def test_stream_export_redacts_token_shaped_values() -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(
        organization,
        project,
        team,
        title=f'Redact {LEAKED_TOKEN}',
        body=f'Body leaks {LEAKED_TOKEN}.',
    )

    chunks = list(
        iter_export_memories_json(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
        ),
    )
    serialized = ''.join(chunks)

    assert LEAKED_TOKEN not in serialized
    assert '[REDACTED]' in serialized


@pytest.mark.django_db
def test_export_excludes_non_approved_memories(tmp_path: Any) -> None:
    organization, team, project = create_organization_project_team()
    create_approved_memory(organization, project, team, title='Approved memory')
    Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Archived memory',
        body='Archived memory must be excluded from the export.',
        status=MemoryStatus.ARCHIVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    payload = run_export(tmp_path, organization, project)

    exported_titles = [entry['title'] for entry in payload['memories']]
    assert exported_titles == ['Approved memory']
    assert payload['memory_count'] == 1
