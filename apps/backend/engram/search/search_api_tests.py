from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from engram.access.models import ApiKeyCapability, Capability
from engram.context.context_api_tests import (
    OTHER_RAW_KEY,
    RAW_KEY,
    auth_headers,
    complete_transition_embedding,
    create_approved_memory_document,
    create_embedding_policy,
    create_project_scope,
    create_scoped_api_key,
)
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    MemoryCandidate,
    MemoryLink,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.memory.transitions import PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request


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
def test_search_does_not_bulk_load_embedding_columns() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    with CaptureQueriesContext(connection) as captured:
        response = client.post(
            '/v1/search/',
            search_payload(project),
            format='json',
            **auth_headers(),
        )

    assert response.status_code == 200
    document_load_queries = [
        query['sql']
        for query in captured.captured_queries
        if 'core_retrievaldocument' in query['sql'] and 'JOIN "core_memory"' in query['sql']
    ]
    assert document_load_queries
    assert all('embedding_vector' not in sql for sql in document_load_queries)


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
    assert body['warnings'] == []


@pytest.mark.django_db
def test_search_item_includes_confidence_and_kind() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    memory, _version, document = create_approved_memory_document(
        organization,
        team,
        project,
        confidence=Decimal('0.950'),
        kind='gotcha',
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, file_paths=document.file_paths),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['memory_id'] == str(memory.id)
    assert item['confidence'] == '0.950'
    assert item['kind'] == 'gotcha'


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
    candidate, _source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='search-semantic',
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation',
        visibility_scope=VisibilityScope.PROJECT,
    )
    result = PromoteMemoryCandidate().execute(transition_request(candidate))
    memory = result.memory
    version = result.memory_version
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))
    complete_transition_embedding(result.retrieval_document)
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


@pytest.mark.django_db
def test_search_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-search',
        repository_url='git@github.com:acme/foreign-search.git',
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {
            'query': 'authorization',
            'file_paths': [],
            'symbols': [],
            'limit': 5,
            'repository_url': 'https://github.com/acme/foreign-search',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    audit = AuditEvent.objects.get(event_type='AccessScopeResolved', project_id=foreign_project.id)
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['resolved_project_id'] == str(foreign_project.id)


@pytest.mark.django_db
def test_search_bound_key_with_agent_capability_denied_for_foreign_project() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-search-footgun',
        repository_url='git@github.com:acme/foreign-search-footgun.git',
    )
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('search:query', 'projects:agent'),
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {
            'query': 'authorization',
            'file_paths': [],
            'symbols': [],
            'limit': 5,
            'repository_url': 'https://github.com/acme/foreign-search-footgun',
        },
        format='json',
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_search_bound_key_with_agent_capability_allowed_for_own_project() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/own-search-project.git'
    project.save(update_fields=['repository_url'])
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('search:query', 'projects:agent'),
    )
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {
            'query': 'authorization ranking',
            'file_paths': document.file_paths,
            'symbols': [],
            'limit': 5,
            'repository_url': 'https://github.com/acme/own-search-project',
        },
        format='json',
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 200, response.json()
    assert len(response.json()['items']) == 1
    assert response.json()['items'][0]['memory_id'] == str(memory.id)


@pytest.mark.django_db
def test_search_bound_key_unknown_repository_url_returns_404_without_creating_project() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    project_count_before = Project.objects.filter(organization=organization).count()
    client = APIClient()

    response = client.post(
        '/v1/search/',
        {
            'query': 'anything',
            'file_paths': [],
            'symbols': [],
            'limit': 5,
            'repository_url': 'https://github.com/acme/never-created-search',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'
    assert Project.objects.filter(organization=organization).count() == project_count_before


@pytest.mark.django_db
def test_search_warns_about_stale_matching_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    create_embedding_policy(organization, team, project)
    stale_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Stale search note',
        body='Stale search note body',
        file_paths=[],
        symbols=[],
        exact_terms=['stale search phrase'],
    )
    stale_memory.stale = True
    stale_memory.save(update_fields=['stale'])
    RetrievalDocument.objects.filter(memory=stale_memory).update(stale=True)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, query='stale search phrase', file_paths=[], symbols=[]),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == [
        {
            'code': 'stale_match',
            'message': f'stale memory matched: "{stale_memory.title}"',
            'memory_id': str(stale_memory.id),
        },
    ]


@pytest.mark.django_db
def test_search_warns_about_unresolved_conflicting_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    memory, _version, document = create_approved_memory_document(organization, team, project)
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Conflicting candidate',
        body='Conflicting candidate body',
        status=CandidateStatus.PROPOSED,
        content_hash='search-conflict-candidate-hash',
    )
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
        label='contradiction claim',
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, file_paths=document.file_paths),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == [
        {
            'code': 'conflicting_memory',
            'message': 'memory has an unresolved contradiction claim',
            'memory_id': str(memory.id),
        },
    ]


@pytest.mark.django_db
def test_search_kinds_filter_returns_only_matching_kind() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    gotcha_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Gotcha memory',
        body='Gotcha memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds filter phrase'],
        kind='gotcha',
    )
    decision_memory, _decision_version, _decision_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Decision memory',
        body='Decision memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds filter phrase'],
        kind='decision',
    )
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, query='kinds filter phrase', file_paths=[], symbols=[], kinds=['gotcha']),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['memory_id'] for item in body['items']] == [str(gotcha_memory.id)]
    assert str(decision_memory.id) not in str(body)


@pytest.mark.django_db
def test_search_kinds_invalid_value_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, kinds=['bogus']),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert 'bogus' in str(response.json())


@pytest.mark.django_db
def test_search_kinds_max_items_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    client = APIClient()

    response = client.post(
        '/v1/search/',
        search_payload(project, kinds=['gotcha'] * 7),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
