from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKeyCapability, Capability
from engram.context.context_api_tests import (
    OTHER_RAW_KEY,
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_embedding_policy,
    create_project_scope,
    create_scoped_api_key,
)
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import Memory, MemoryStatus, MemoryVersion, Project, Team, VisibilityScope


def search_payload(project: Project, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        'project_id': str(project.id),
        'query': 'authorization ranking',
        'file_paths': [],
        'symbols': [],
        'limit': 5,
    }
    payload.update(overrides)

    return payload


def grant_search_capability(raw_key: str) -> None:
    from engram.access.models import ApiKey
    from engram.access.services import hash_api_key

    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='search:query'),
    )


@pytest.mark.django_db
def test_search_returns_ranked_cited_matches() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    memory, version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, file_paths=document.file_paths),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['request_id']
    assert len(body['items']) == 1
    item = body['items'][0]
    assert item['citation'] == 'M1'
    assert item['memory_id'] == str(memory.id)
    assert item['memory_version_id'] == str(version.id)
    assert item['retrieval_document_id'] == str(document.id)
    assert item['inclusion_reason'].startswith('exact match:')
    assert item['scope_evidence']['project_id'] == str(project.id)
    assert item['scope_evidence']['visibility_scope'] == VisibilityScope.PROJECT
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_search_requires_search_query_capability() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('memories:read',),
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, query='authorization'),
        format='json',
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_search_denies_wrong_project() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-project')
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(other_project, query='authorization'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_search_excludes_other_team_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    other_team = Team.objects.create(organization=organization, name='Other', slug='other-team')
    create_approved_memory_document(
        organization,
        other_team,
        project,
        visibility_scope=VisibilityScope.TEAM,
        title='Other team private memory',
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, team_id=str(team.id), query='private'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()['items'] == []


@pytest.mark.django_db
def test_search_rejects_oversized_query() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, query='a' * 8001),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_search_requires_bearer_api_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    client = APIClient()

    response = client.post('/v1/search/', search_payload(project), format='json')

    assert response.status_code == 401
    assert response.json()['code'] == 'missing_api_key'


@pytest.mark.django_db
def test_search_returns_semantic_match_when_exact_misses() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    create_embedding_policy(organization, team, project)
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='search-semantic-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, query='color behavior optimization', file_paths=[], symbols=[]),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body['items']) == 1
    assert body['items'][0]['inclusion_reason'].startswith('semantic match: cosine')
    assert body['items'][0]['memory_id'] == str(memory.id)


AGENT_RAW_KEY = 'egk_test_search_agent_0123456789abcdefghijklmnopqrstuv'
AGENT_CAPS = ('memories:read', 'search:query', 'projects:agent')


def create_org_agent_key(organization: object) -> None:
    from engram.access.models import (
        ApiKey,
        Identity,
        IdentityType,
        OrganizationMembership,
        Role,
        RoleCapability,
    )
    from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key

    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='search-agent',
        display_name='Search agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='search agent key',
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


@pytest.mark.django_db
def test_search_routes_by_repository_url_without_project_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/search-demo.git'
    project.save(update_fields=['repository_url'])
    create_org_agent_key(organization)
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {
            'query': 'authorization ranking',
            'file_paths': document.file_paths,
            'symbols': [],
            'limit': 5,
            'repository_url': 'https://github.com/acme/search-demo',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert len(body['items']) == 1
    assert body['items'][0]['memory_id'] == str(memory.id)


@pytest.mark.django_db
def test_search_without_project_and_repository_url_is_rejected() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {'query': 'anything', 'file_paths': [], 'symbols': [], 'limit': 5},
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
