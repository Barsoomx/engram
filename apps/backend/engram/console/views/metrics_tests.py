from __future__ import annotations

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
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    ContextBundle,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    Organization,
    Project,
    Runtime,
    SessionStatus,
)


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='test-pass-123')  # noqa: S106


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


def _make_client(token: str, organization: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(organization.id),
    )

    return client


def _make_agent(organization: Organization, external_id: str = 'agent-1') -> Agent:
    return Agent.objects.create(
        organization=organization,
        runtime=Runtime.UNKNOWN,
        external_id=external_id,
        display_name=external_id,
    )


def _make_project(organization: Organization, slug: str = 'proj') -> Project:
    return Project.objects.create(organization=organization, name=slug, slug=slug)


def _make_session(
    organization: Organization,
    project: Project,
    agent: Agent,
    external_id: str = 'sess-1',
) -> AgentSession:
    return AgentSession.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        external_session_id=external_id,
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
    )


def _make_memory(
    organization: Organization,
    project: Project,
    title: str = 'mem',
    status: str = MemoryStatus.APPROVED,
) -> Memory:
    return Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body='body',
        status=status,
    )


def _make_context_bundle(
    organization: Organization,
    project: Project,
    agent: Agent,
    session: AgentSession,
    request_id: str = 'req-1',
) -> ContextBundle:
    return ContextBundle.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        session=session,
        request_id=request_id,
        purpose='context',
    )


def _make_audit_event(
    organization: Organization,
    project: Project | None = None,
    event_type: str = 'TestEvent',
) -> AuditEvent:
    return AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type=event_type,
        actor_type='user',
        actor_id='actor-1',
        target_type='memory',
        target_id='target-1',
    )


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Metrics Org', slug='metrics-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return _make_project(f_org, slug='main-project')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return _make_agent(f_org, external_id='test-agent-1')


@pytest.fixture
def f_admin_client(f_org: Organization) -> APIClient:
    user = _make_user('metrics-admin')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities(
        'metrics_admin_role',
        ('memories:read', 'projects:*'),
    )
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_limited_org() -> Organization:
    return Organization.objects.create(name='Limited Org', slug='limited-org')


@pytest.fixture
def f_project_a(f_limited_org: Organization) -> Project:
    return _make_project(f_limited_org, slug='project-a')


@pytest.fixture
def f_project_b(f_limited_org: Organization) -> Project:
    return _make_project(f_limited_org, slug='project-b')


@pytest.fixture
def f_limited_client(
    f_limited_org: Organization,
    f_project_a: Project,
) -> APIClient:
    user = _make_user('metrics-limited')
    identity = _make_identity(user, f_limited_org)
    role = _make_role_with_capabilities(
        'metrics_limited_role',
        ('memories:read',),
    )
    OrganizationMembership.objects.create(
        organization=f_limited_org,
        identity=identity,
        role=role,
    )
    ProjectGrant.objects.create(
        organization=f_limited_org,
        project=f_project_a,
        identity=identity,
        role=role,
    )
    token = Token.objects.create(user=user).key

    return _make_client(token, f_limited_org)


@pytest.mark.django_db
def test_overview_returns_approved_memory_count(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, title='m1')
    _make_memory(f_org, f_project, title='m2')
    _make_memory(f_org, f_project, title='archived', status=MemoryStatus.ARCHIVED)

    response = f_admin_client.get('/v1/admin/metrics/overview')

    assert response.status_code == 200
    assert response.data['memories_indexed'] == 2
    assert 'memories_indexed_delta' in response.data
    assert response.data['avg_retrieval_latency_ms'] is None
    assert response.data['avg_retrieval_latency_measured'] is False


@pytest.mark.django_db
def test_overview_counts_context_bundles_7d(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = _make_session(f_org, f_project, f_agent, external_id='sess-cb-1')
    _make_context_bundle(f_org, f_project, f_agent, session, request_id='req-cb-1')

    response = f_admin_client.get('/v1/admin/metrics/overview')

    assert response.status_code == 200
    assert response.data['context_bundles_7d'] >= 1


@pytest.mark.django_db
def test_overview_counts_connected_agents(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    _make_session(f_org, f_project, f_agent, external_id='sess-connected-1')

    response = f_admin_client.get('/v1/admin/metrics/overview')

    assert response.status_code == 200
    assert response.data['connected_agents'] >= 1


@pytest.mark.django_db
def test_memory_ingest_returns_daily_list(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    MemoryCandidate.objects.create(
        organization=f_org,
        project=f_project,
        title='candidate-1',
        body='body',
        content_hash='hash-ingest-1',
    )

    response = f_admin_client.get('/v1/admin/metrics/memory-ingest')

    assert response.status_code == 200
    assert isinstance(response.data, list)
    assert len(response.data) >= 1
    entry = response.data[0]
    assert 'date' in entry
    assert 'count' in entry
    assert entry['count'] >= 1


@pytest.mark.django_db
def test_sessions_returns_session_list(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    _make_session(f_org, f_project, f_agent, external_id='sess-list-1')

    response = f_admin_client.get('/v1/admin/metrics/sessions')

    assert response.status_code == 200
    assert isinstance(response.data, list)
    assert len(response.data) >= 1

    item = response.data[0]
    assert 'session_id' in item
    assert 'agent_name' in item
    assert 'model_id' in item
    assert 'status' in item
    assert 'last_seen' in item
    assert item['model_id'] == ''


@pytest.mark.django_db
def test_sessions_status_is_idle_for_old_session(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    session = _make_session(f_org, f_project, f_agent, external_id='sess-old-1')
    AgentSession.objects.filter(id=session.id).update(
        updated_at=timezone.now() - timedelta(hours=1),
    )

    response = f_admin_client.get('/v1/admin/metrics/sessions')

    assert response.status_code == 200
    statuses = [item['status'] for item in response.data if item['session_id'] == str(session.id)]
    assert statuses == ['idle']


@pytest.mark.django_db
def test_activity_returns_audit_events(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_audit_event(f_org, f_project, event_type='MemoryReviewed')

    response = f_admin_client.get('/v1/admin/metrics/activity')

    assert response.status_code == 200
    assert isinstance(response.data, list)
    assert len(response.data) >= 1

    item = response.data[0]
    assert 'event_type' in item
    assert 'actor_type' in item
    assert 'actor_id' in item
    assert 'target_type' in item
    assert 'target_id' in item
    assert 'result' in item
    assert 'created_at' in item


@pytest.mark.django_db
def test_project_limited_admin_overview_excludes_other_project(
    f_limited_client: APIClient,
    f_limited_org: Organization,
    f_project_a: Project,
    f_project_b: Project,
) -> None:
    _make_memory(f_limited_org, f_project_a, title='visible-mem')
    _make_memory(f_limited_org, f_project_b, title='hidden-mem')

    response = f_limited_client.get('/v1/admin/metrics/overview')

    assert response.status_code == 200
    assert response.data['memories_indexed'] == 1


@pytest.mark.django_db
def test_project_limited_admin_activity_excludes_other_project(
    f_limited_client: APIClient,
    f_limited_org: Organization,
    f_project_a: Project,
    f_project_b: Project,
) -> None:
    _make_audit_event(f_limited_org, f_project_a, event_type='VisibleEvent')
    _make_audit_event(f_limited_org, f_project_b, event_type='HiddenEvent')

    response = f_limited_client.get('/v1/admin/metrics/activity')

    assert response.status_code == 200
    event_types = [item['event_type'] for item in response.data]
    assert 'VisibleEvent' in event_types
    assert 'HiddenEvent' not in event_types


@pytest.mark.django_db
def test_overview_narrows_to_project_id(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    other = _make_project(f_org, slug='other-overview')
    _make_memory(f_org, f_project, title='in-scope')
    _make_memory(f_org, other, title='other-project')

    response = f_admin_client.get('/v1/admin/metrics/overview', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    assert response.data['memories_indexed'] == 1


@pytest.mark.django_db
def test_overview_project_id_outside_scope_returns_empty_not_forbidden(
    f_limited_client: APIClient,
    f_limited_org: Organization,
    f_project_a: Project,
    f_project_b: Project,
) -> None:
    _make_memory(f_limited_org, f_project_a, title='visible')
    _make_memory(f_limited_org, f_project_b, title='hidden')

    response = f_limited_client.get('/v1/admin/metrics/overview', {'project_id': str(f_project_b.id)})

    assert response.status_code == 200
    assert response.data['memories_indexed'] == 0


@pytest.mark.django_db
def test_activity_narrows_to_project_id(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    other = _make_project(f_org, slug='other-activity')
    _make_audit_event(f_org, f_project, event_type='VisibleEvent')
    _make_audit_event(f_org, other, event_type='HiddenEvent')

    response = f_admin_client.get('/v1/admin/metrics/activity', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    event_types = [item['event_type'] for item in response.data]
    assert 'VisibleEvent' in event_types
    assert 'HiddenEvent' not in event_types


@pytest.mark.django_db
def test_sessions_narrows_to_project_id(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    other = _make_project(f_org, slug='other-sessions')
    _make_session(f_org, f_project, f_agent, external_id='sess-in-scope')
    other_agent = _make_agent(f_org, external_id='agent-other-sessions')
    _make_session(f_org, other, other_agent, external_id='sess-other')

    response = f_admin_client.get('/v1/admin/metrics/sessions', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    external_ids = {item['session_id'] for item in response.data}
    assert len(external_ids) == 1


@pytest.mark.django_db
def test_overview_invalid_project_id_returns_400(
    f_admin_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_admin_client.get('/v1/admin/metrics/overview', {'project_id': 'not-a-uuid'})

    assert response.status_code == 400


@pytest.mark.django_db
def test_requires_authentication(f_org: Organization) -> None:
    client = APIClient()

    assert client.get('/v1/admin/metrics/overview').status_code == 401
    assert client.get('/v1/admin/metrics/memory-ingest').status_code == 401
    assert client.get('/v1/admin/metrics/sessions').status_code == 401
    assert client.get('/v1/admin/metrics/activity').status_code == 401
