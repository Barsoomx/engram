from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.context_api_tests import (
    OTHER_RAW_KEY,
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
    create_scoped_api_key,
)
from engram.core.models import MemoryVersion, Organization, Project, Team, VisibilityScope

AGENT_RAW_KEY = 'egk_test_memory_diff_agent_0123456789abcdefghijklmnopqrstuvwxyz'
AGENT_CAPS = ('memories:read', 'projects:agent')


def create_org_agent_key(organization: Organization) -> None:
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='memory-diff-agent',
        display_name='Memory diff agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='memory diff agent key',
        key_prefix=api_key_prefix(AGENT_RAW_KEY),
        key_hash=hash_api_key(AGENT_RAW_KEY),
        key_fingerprint=api_key_fingerprint(AGENT_RAW_KEY),
        active=True,
    )
    for code in AGENT_CAPS:
        ApiKeyCapability.objects.get_or_create(
            api_key=api_key,
            capability=Capability.objects.get(code=code),
        )


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


@pytest.mark.django_db
def test_memory_diff_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _v1, _doc = create_approved_memory_document(organization, team, project, body='Repo url body v1')
    _create_version(memory, organization, project, 2, 'Repo url body v2')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {'repository_url': project.repository_url, 'from_version': 1, 'to_version': 2},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body['from']['version'] == 1
    assert body['to']['version'] == 2


@pytest.mark.django_db
def test_memory_diff_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {'repository_url': 'https://github.com/acme/never-created-diff', 'from_version': 1, 'to_version': 1},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_memory_diff_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-diff')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-diff',
        repository_url='git@github.com:acme/foreign-diff.git',
    )
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {'repository_url': 'https://github.com/acme/foreign-diff', 'from_version': 1, 'to_version': 1},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_memory_diff_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-diff-inorg',
        repository_url='git@github.com:acme/foreign-in-org-diff.git',
    )
    memory, _v1, _doc = create_approved_memory_document(organization, team, foreign_project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {'repository_url': 'https://github.com/acme/foreign-in-org-diff', 'from_version': 1, 'to_version': 1},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_memory_diff_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {'from_version': 1, 'to_version': 1},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_memory_diff_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_read_capability()
    memory, _v1, _doc = create_approved_memory_document(organization, team, project)
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-diff',
        repository_url='git@github.com:acme/decoy-diff.git',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'project_id': str(project.id),
            'repository_url': 'https://github.com/acme/decoy-diff',
            'from_version': 1,
            'to_version': 1,
        },
        **auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()['from']['version'] == 1


@pytest.mark.django_db
def test_memory_diff_repository_url_resolving_elsewhere_never_leaks_object_from_another_project() -> None:
    organization, team, project_a, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    Project.objects.create(
        organization=organization,
        name='Project B',
        slug='project-b-diff-leak-probe',
        repository_url='git@github.com:acme/project-b-diff.git',
    )
    memory, _v1, _doc = create_approved_memory_document(
        organization,
        team,
        project_a,
        body='Project A secret memory body',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/diff',
        {
            'repository_url': 'https://github.com/acme/project-b-diff',
            'from_version': 1,
            'to_version': 1,
        },
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'memory_not_found'
    assert 'Project A secret memory body' not in str(response.json())
