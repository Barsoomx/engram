from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.core.models import AuditEvent, Organization


def _make_user(username: str = 'alice') -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_role(code: str = 'organization_owner') -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    return role


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _make_membership(
    user: User,
    organization: Organization,
    *,
    role_code: str = 'organization_owner',
) -> OrganizationMembership:
    identity = _make_identity(user, organization)

    membership, _ = OrganizationMembership.objects.get_or_create(
        organization=organization,
        identity=identity,
        defaults={'role': _make_role(role_code)},
    )

    return membership


@pytest.fixture
def f_owner_user_token() -> str:
    user = _make_user('owner')
    _make_membership(user, Organization.objects.create(name='Acme', slug='acme'))

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_owner_user() -> User:
    return User.objects.get(username='owner')


@pytest.fixture
def f_owned_org() -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_developer_user_token() -> str:
    user = _make_user('dev')
    org = Organization.objects.create(name='Devco', slug='devco')
    _make_membership(user, org, role_code='developer')

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_other_org() -> Organization:
    return Organization.objects.create(name='Globex', slug='globex')


def _auth_client(token: str, org: Organization | None = None) -> APIClient:
    client = APIClient()

    headers: dict[str, str] = {'HTTP_AUTHORIZATION': f'Token {token}'}

    if org is not None:
        headers['HTTP_X_ENGRAM_ORGANIZATION'] = str(org.id)

    client.credentials(**headers)

    return client


@pytest.mark.django_db
def test_list_returns_member_organizations_with_pagination(f_owner_user_token: str) -> None:
    client = _auth_client(f_owner_user_token)

    response = client.get('/v1/admin/organizations/')

    assert response.status_code == 200

    assert set(response.data.keys()) == {'count', 'next', 'previous', 'results'}

    assert response.data['count'] == 1

    org = response.data['results'][0]

    assert set(org.keys()) == {'id', 'name', 'slug', 'created_at', 'updated_at'}

    assert org['slug'] == 'acme'


@pytest.mark.django_db
def test_list_available_without_active_org_header(f_developer_user_token: str) -> None:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_developer_user_token}')

    response = client.get('/v1/admin/organizations/')

    assert response.status_code == 200

    assert 'results' in response.data


@pytest.mark.django_db
def test_retrieve_member_organization(f_owner_user_token: str, f_owned_org: Organization) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/organizations/{f_owned_org.id}/')

    assert response.status_code == 200

    assert response.data['id'] == str(f_owned_org.id)

    assert response.data['name'] == 'Acme'


@pytest.mark.django_db
def test_retrieve_returns_404_for_non_member_organization(
    f_owner_user_token: str,
    f_other_org: Organization,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/organizations/{f_other_org.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_patch_updates_name_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.patch(
        f'/v1/admin/organizations/{f_owned_org.id}/',
        {'name': 'Renamed'},
    )

    assert response.status_code == 200

    assert response.data['name'] == 'Renamed'

    f_owned_org.refresh_from_db()

    assert f_owned_org.name == 'Renamed'

    assert f_owned_org.slug == 'acme'

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='OrganizationUpdated',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'organization'

    assert event.target_id == str(f_owned_org.id)

    assert event.metadata.get('fields') == ['name']


@pytest.mark.django_db
def test_patch_rejects_slug_change(f_owner_user_token: str, f_owned_org: Organization) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.patch(
        f'/v1/admin/organizations/{f_owned_org.id}/',
        {'slug': 'new-slug'},
    )

    assert response.status_code == 400

    assert 'slug' in response.data


@pytest.mark.django_db
def test_patch_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    other_org = Organization.objects.create(name='Other', slug='other')

    _make_membership(User.objects.get(username='dev'), other_org, role_code='developer')

    client = _auth_client(f_developer_user_token, org=other_org)

    response = client.patch(
        f'/v1/admin/organizations/{other_org.id}/',
        {'name': 'New'},
    )

    assert response.status_code == 403
