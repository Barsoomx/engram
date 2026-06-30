from __future__ import annotations

import datetime

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
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
from engram.core.models import AuditEvent, Organization, Project


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


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

    capabilities = [_ensure_capability(raw) for raw in capability_codes]

    for capability in capabilities:
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _client(token: str, org: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


def _make_project(organization: Organization, slug: str = 'main') -> Project:
    return Project.objects.create(organization=organization, name=slug, slug=slug)


def _make_audit_event(
    organization: Organization,
    project: Project | None = None,
    *,
    event_type: str = 'TestEvent',
    actor_type: str = 'system',
    actor_id: str = '',
    target_type: str = '',
    target_id: str = '',
    result: str = 'recorded',
    request_id: str = '',
) -> AuditEvent:
    return AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
    )


@pytest.fixture
def f_auditor_client() -> APIClient:
    user = _make_user('audit-reader')

    org = Organization.objects.create(name='AuditOrg', slug='audit-org')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities('auditor', ('audit:read',))

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    return _client(token, org)


@pytest.fixture
def f_auditor_org(f_auditor_client: APIClient) -> Organization:
    return Organization.objects.get(slug='audit-org')


@pytest.mark.django_db
def test_list_returns_tenant_scoped_events(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org)

    _make_audit_event(f_auditor_org, project, event_type='OwnEvent', request_id='own')

    other_org = Organization.objects.create(name='Other', slug='other-audit')

    other_project = _make_project(other_org, slug='other-main')

    _make_audit_event(other_org, other_project, event_type='LeakedEvent', request_id='leaked')

    response = f_auditor_client.get('/v1/admin/audit-events/')

    assert response.status_code == 200

    event_types = [e['event_type'] for e in response.data['results']]

    assert 'OwnEvent' in event_types

    assert 'LeakedEvent' not in event_types


@pytest.mark.django_db
def test_retrieve_returns_event_with_display_names(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org)

    actor_identity = Identity.objects.create(
        organization=f_auditor_org,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='svc-actor-1',
        display_name='Service Account',
    )

    event = _make_audit_event(
        f_auditor_org,
        project,
        event_type='MemoryCreated',
        actor_type='identity',
        actor_id=str(actor_identity.id),
        target_type='project',
        target_id=str(project.id),
    )

    response = f_auditor_client.get(f'/v1/admin/audit-events/{event.id}/')

    assert response.status_code == 200

    assert response.data['actor_display'] == 'Service Account'

    assert response.data['target_display'] == 'main'


@pytest.mark.django_db
def test_retrieve_other_org_event_returns_404(f_auditor_client: APIClient) -> None:
    other_org = Organization.objects.create(name='Foreign', slug='foreign-audit')

    other_project = _make_project(other_org, slug='fp')

    foreign_event = _make_audit_event(other_org, other_project)

    response = f_auditor_client.get(f'/v1/admin/audit-events/{foreign_event.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_list_other_org_event_absent(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    other_org = Organization.objects.create(name='Foreign2', slug='foreign-audit-2')

    other_project = _make_project(other_org, slug='fp2')

    foreign_event = _make_audit_event(other_org, other_project, request_id='foreign')

    response = f_auditor_client.get('/v1/admin/audit-events/')

    assert response.status_code == 200

    event_ids = [e['id'] for e in response.data['results']]

    assert str(foreign_event.id) not in event_ids


@pytest.mark.django_db
def test_filter_by_event_type(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org, slug='filter-proj')

    _make_audit_event(f_auditor_org, project, event_type='TypeA')

    _make_audit_event(f_auditor_org, project, event_type='TypeB')

    response = f_auditor_client.get('/v1/admin/audit-events/', {'event_type': 'TypeA'})

    assert response.status_code == 200

    types = [e['event_type'] for e in response.data['results']]

    assert types == ['TypeA']


@pytest.mark.django_db
def test_filter_by_result(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org, slug='result-proj')

    _make_audit_event(f_auditor_org, project, result='recorded')

    _make_audit_event(f_auditor_org, project, result='denied')

    response = f_auditor_client.get('/v1/admin/audit-events/', {'result': 'denied'})

    assert response.status_code == 200

    results = [e['result'] for e in response.data['results']]

    assert results == ['denied']


@pytest.mark.django_db
def test_filter_by_actor_id(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org, slug='actor-proj')

    _make_audit_event(f_auditor_org, project, actor_id='actor-1')

    _make_audit_event(f_auditor_org, project, actor_id='actor-2')

    response = f_auditor_client.get('/v1/admin/audit-events/', {'actor_id': 'actor-1'})

    assert response.status_code == 200

    actor_ids = [e['actor_id'] for e in response.data['results']]

    assert all(a == 'actor-1' for a in actor_ids)


@pytest.mark.django_db
def test_filter_by_date_range(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org, slug='date-proj')

    event = _make_audit_event(f_auditor_org, project, event_type='DateEvent')

    now = datetime.datetime.now(datetime.UTC)

    future = (now + datetime.timedelta(days=1)).isoformat()

    past = (now - datetime.timedelta(days=1)).isoformat()

    response_in = f_auditor_client.get(
        '/v1/admin/audit-events/',
        {'created_at__gte': past, 'created_at__lt': future},
    )

    assert response_in.status_code == 200

    event_ids_in = [e['id'] for e in response_in.data['results']]

    assert str(event.id) in event_ids_in

    response_out = f_auditor_client.get(
        '/v1/admin/audit-events/',
        {'created_at__lt': past},
    )

    assert response_out.status_code == 200

    event_ids_out = [e['id'] for e in response_out.data['results']]

    assert str(event.id) not in event_ids_out


@pytest.mark.django_db
def test_list_denied_without_audit_read() -> None:
    user = _make_user('no-audit')

    org = Organization.objects.create(name='NoCap', slug='no-cap-audit')

    identity = _make_identity(user, org)

    role, _ = Role.objects.get_or_create(code='no_caps_audit', defaults={'name': 'no_caps_audit'})

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    client = _client(token, org)

    response = client.get('/v1/admin/audit-events/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_list_paginates(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    from rest_framework.settings import api_settings

    page_size = int(api_settings.PAGE_SIZE)

    project = _make_project(f_auditor_org, slug='paginate-proj')

    for i in range(page_size + 1):
        _make_audit_event(f_auditor_org, project, event_type=f'PageEvent{i}')

    response = f_auditor_client.get('/v1/admin/audit-events/')

    assert response.status_code == 200

    assert response.data['count'] == page_size + 1

    assert len(response.data['results']) == page_size

    assert response.data['next'] is not None


@pytest.mark.django_db
def test_list_query_count_bounded(
    f_auditor_client: APIClient,
    f_auditor_org: Organization,
) -> None:
    project = _make_project(f_auditor_org, slug='n1-proj')

    for i in range(5):
        _make_audit_event(
            f_auditor_org,
            project,
            event_type=f'N1Event{i}',
            actor_type='identity',
            actor_id='some-id',
        )

    with CaptureQueriesContext(connection) as ctx_5:
        response = f_auditor_client.get('/v1/admin/audit-events/')

    assert response.status_code == 200

    count_5 = len(ctx_5)

    for i in range(10):
        _make_audit_event(
            f_auditor_org,
            project,
            event_type=f'N1Event2_{i}',
            actor_type='identity',
            actor_id='some-id',
        )

    with CaptureQueriesContext(connection) as ctx_15:
        response = f_auditor_client.get('/v1/admin/audit-events/')

    assert response.status_code == 200

    count_15 = len(ctx_15)

    assert count_15 == count_5
