from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKey, ApiKeyCapability, Capability, Role, RoleCapability
from engram.access.services import hash_api_key
from engram.context.context_api_tests import (
    OTHER_RAW_KEY,
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
    create_scoped_api_key,
)
from engram.core.models import MemoryVersion, Project, Team, VisibilityScope


def grant_read_capability(raw_key: str = RAW_KEY) -> None:
    developer = Role.objects.get(code='developer')
    RoleCapability.objects.get_or_create(
        role=developer,
        capability=Capability.objects.get(code='memories:read'),
    )
    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:read'),
    )


def _create_version(
    memory: object,
    organization: object,
    project: object,
    version_number: int,
    body: str,
) -> MemoryVersion:
    return MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=version_number,
        body=body,
        content_hash=f'hash-v{version_number}',
    )


@pytest.mark.django_db
def test_memory_diff_session_auth_returns_from_and_to_versions() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    memory, _v1, _doc = create_approved_memory_document(organization, team, project, body='Original body v1')
    _create_version(memory, organization, project, 2, 'Updated body v2')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'from_version': 1,
            'to_version': 2,
        },
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['from']['version'] == 1
    assert body['from']['body'] == 'Original body v1'
    assert body['to']['version'] == 2
    assert body['to']['body'] == 'Updated body v2'
    assert 'created_at' in body['from']
    assert 'created_at' in body['to']


@pytest.mark.django_db
def test_memory_diff_bearer_auth_returns_versions() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('memories:read',),
    )
    memory, _v1, _doc = create_approved_memory_document(organization, team, project, body='Bearer body v1')
    _create_version(memory, organization, project, 2, 'Bearer body v2')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'from_version': 1,
            'to_version': 2,
        },
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['from']['version'] == 1
    assert body['to']['version'] == 2


@pytest.mark.django_db
def test_memory_diff_cross_project_denied() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-diff')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(other_project.id),
            'from_version': 1,
            'to_version': 1,
        },
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_memory_diff_missing_version_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'from_version': 1,
            'to_version': 99,
        },
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'version_not_found'


@pytest.mark.django_db
def test_memory_diff_nonexistent_memory_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    client = APIClient()

    response = client.get(
        f'/v1/memories/{uuid.uuid4()}/diff',
        {
            'project_id': str(project.id),
            'from_version': 1,
            'to_version': 2,
        },
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'memory_not_found'


@pytest.mark.django_db
def test_memory_diff_redacts_secret_shaped_body() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    secret_value = 'sk-test_super_secret_api_key_1234567890abcdef'
    memory, _v1, _doc = create_approved_memory_document(
        organization, team, project, body=f'Use {secret_value} for auth'
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'from_version': 1,
            'to_version': 1,
        },
        **auth_headers(),
    )

    assert response.status_code == 200
    body_text = str(response.json())
    assert secret_value not in body_text


@pytest.mark.django_db
def test_memory_diff_team_scoped_denied_for_other_team() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    other_team = Team.objects.create(organization=organization, name='Other', slug='other-diff-team')
    memory, _v1, _doc = create_approved_memory_document(
        organization,
        other_team,
        project,
        visibility_scope=VisibilityScope.TEAM,
        title='Team private diff memory',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'from_version': 1,
            'to_version': 1,
        },
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'team_scope_denied'
