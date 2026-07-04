from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
    RoleCapability,
)
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


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='test-secret-456')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(
        code=code,
        defaults={'description': code},
    )

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for cap_code in capability_codes:
        capability = _ensure_capability(cap_code)
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_admin_client(org: Organization, username: str = 'debug-admin') -> APIClient:
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(f'debug_reader_{username}', ('memories:read',))
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


def _make_org_project(slug: str) -> tuple[Organization, Project]:
    org = Organization.objects.create(name=slug, slug=slug)
    project = Project.objects.create(organization=org, name='Main', slug='main')

    return org, project


def _make_retrieval_doc(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    title: str,
    status: str = MemoryStatus.APPROVED,
    stale: bool = False,
    refuted: bool = False,
    visibility_scope: str = VisibilityScope.PROJECT,
    exact_terms: list[str] | None = None,
    kind: str = '',
    confidence: Decimal | None = None,
) -> tuple[Memory, RetrievalDocument]:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=f'Body for {title}',
        status=status,
        visibility_scope=visibility_scope,
        stale=stale,
        refuted=refuted,
        metadata={'kind': kind} if kind else {},
        confidence=confidence,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash=f'{title}-hash',
    )
    terms = exact_terms or [title.lower()]
    doc = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=visibility_scope,
        source_observation_ids=[],
        file_paths=[],
        symbols=[],
        exact_terms=terms,
        full_text=f'{title}\n\nBody for {title}',
        stale=stale,
        refuted=refuted,
    )

    return memory, doc


@pytest.fixture
def f_org_project() -> tuple[Organization, Project]:
    return _make_org_project('debug-org')


@pytest.fixture
def f_admin_client(f_org_project: tuple[Organization, Project]) -> APIClient:
    org, _project = f_org_project

    return _make_admin_client(org)


@pytest.mark.django_db
def test_happy_path_returns_scope_filters_exact_matches_and_excluded(
    f_admin_client: APIClient,
    f_org_project: tuple[Organization, Project],
) -> None:
    org, project = f_org_project

    matched_memory, _doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='cache invalidation',
        exact_terms=['cache invalidation'],
    )
    stale_memory, _stale_doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='stale rule',
        stale=True,
        exact_terms=['stale rule'],
    )
    archived_memory, _archived_doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='old archived rule',
        status=MemoryStatus.ARCHIVED,
        exact_terms=['old archived rule'],
    )

    response = f_admin_client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': 'cache invalidation'},
        format='json',
    )

    assert response.status_code == 200
    data = response.data

    assert data['scope_filters']['organization_id'] == str(org.id)
    assert data['scope_filters']['project_id'] == str(project.id)

    assert data['candidate_universe_count'] == 3

    matched_ids = [m['memory_id'] for m in data['exact_matches']]
    assert str(matched_memory.id) in matched_ids

    excluded_map = {e['memory_id']: e['reason'] for e in data['excluded']}
    assert str(stale_memory.id) in excluded_map
    assert excluded_map[str(stale_memory.id)] == 'stale'
    assert str(archived_memory.id) in excluded_map
    assert excluded_map[str(archived_memory.id)] == 'not_approved'

    packed_ids = [p['memory_id'] for p in data['packed_context']]
    assert str(matched_memory.id) in packed_ids


@pytest.mark.django_db
def test_response_exposes_kind_confidence_and_lexical_fields(
    f_admin_client: APIClient,
    f_org_project: tuple[Organization, Project],
) -> None:
    org, project = f_org_project

    matched_memory, _doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='cache invalidation',
        exact_terms=['cache invalidation'],
        kind='gotcha',
        confidence=Decimal('0.910'),
    )

    response = f_admin_client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': 'cache invalidation'},
        format='json',
    )

    assert response.status_code == 200
    data = response.data

    assert 'lexical_enabled' in data
    assert data['lexical_enabled'] is False
    assert data['lexical_candidates'] == []

    exact_match = next(m for m in data['exact_matches'] if m['memory_id'] == str(matched_memory.id))
    assert exact_match['kind'] == 'gotcha'
    assert exact_match['confidence'] == '0.910'

    packed_item = next(p for p in data['packed_context'] if p['memory_id'] == str(matched_memory.id))
    assert packed_item['kind'] == 'gotcha'
    assert packed_item['confidence'] == '0.910'


@pytest.mark.django_db
def test_cross_project_denied_returns_404(
    f_admin_client: APIClient,
) -> None:
    other_org, other_project = _make_org_project('other-org-debug')

    response = f_admin_client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(other_project.id), 'query': 'anything'},
        format='json',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_stale_and_refuted_appear_in_excluded_with_correct_reason(
    f_admin_client: APIClient,
    f_org_project: tuple[Organization, Project],
) -> None:
    org, project = f_org_project

    stale_memory, _s_doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='staleness test memory',
        stale=True,
    )
    refuted_memory, _r_doc = _make_retrieval_doc(
        org,
        project,
        None,
        title='refuted test memory',
        refuted=True,
    )

    response = f_admin_client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': ''},
        format='json',
    )

    assert response.status_code == 200
    data = response.data

    excluded_map = {e['memory_id']: e['reason'] for e in data['excluded']}

    assert excluded_map.get(str(stale_memory.id)) == 'stale'
    assert excluded_map.get(str(refuted_memory.id)) == 'refuted'


@pytest.mark.django_db
def test_other_team_memory_excluded_as_team_not_in_scope(
    f_admin_client: APIClient,
    f_org_project: tuple[Organization, Project],
) -> None:
    org, project = f_org_project

    other_team = Team.objects.create(organization=org, name='Other Team', slug='other-team')

    team_memory, _doc = _make_retrieval_doc(
        org,
        project,
        other_team,
        title='team only knowledge',
        visibility_scope=VisibilityScope.TEAM,
    )

    project_memory, _pdoc = _make_retrieval_doc(
        org,
        project,
        None,
        title='project knowledge',
        visibility_scope=VisibilityScope.PROJECT,
        exact_terms=['project knowledge'],
    )

    response = f_admin_client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': ''},
        format='json',
    )

    assert response.status_code == 200
    data = response.data

    excluded_map = {e['memory_id']: e['reason'] for e in data['excluded']}

    assert excluded_map.get(str(team_memory.id)) == 'team_not_in_scope'

    packed_ids = {p['memory_id'] for p in data['packed_context']}
    assert str(project_memory.id) in packed_ids


@pytest.mark.django_db
def test_unauthenticated_returns_401() -> None:
    org, project = _make_org_project('unauth-org-debug')
    client = APIClient()

    response = client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': 'test'},
        format='json',
    )

    assert response.status_code in {401, 403}


@pytest.mark.django_db
def test_missing_capability_returns_403() -> None:
    org, project = _make_org_project('nocap-org-debug')
    user = _make_user('nocap-debug-user')
    identity = _make_identity(user, org)
    role, _ = Role.objects.get_or_create(code='empty_role_debug', defaults={'name': 'empty_role_debug'})
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    response = client.post(
        '/v1/admin/search-debug/',
        {'project_id': str(project.id), 'query': 'test'},
        format='json',
    )

    assert response.status_code == 403
