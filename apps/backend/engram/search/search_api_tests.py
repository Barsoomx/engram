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
