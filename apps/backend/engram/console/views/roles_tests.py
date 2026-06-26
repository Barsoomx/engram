from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.core.models import Organization


def _make_user(username: str = 'alice') -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_role(code: str = 'organization_owner') -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code, 'built_in': True})

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
def f_owner_org(f_owner_user_token: str) -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_auditor_user_token(f_owner_org: Organization) -> str:
    user = _make_user('auditor')
    _make_membership(user, f_owner_org, role_code='auditor')

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_developer_user_token(f_owner_org: Organization) -> str:
    user = _make_user('dev')
    _make_membership(user, f_owner_org, role_code='developer')

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


def _auth_client(token: str, org: Organization | None = None) -> APIClient:
    client = APIClient()

    headers: dict[str, str] = {'HTTP_AUTHORIZATION': f'Token {token}'}

    if org is not None:
        headers['HTTP_X_ENGRAM_ORGANIZATION'] = str(org.id)

    client.credentials(**headers)

    return client


@pytest.mark.django_db
def test_list_returns_built_in_roles_with_nested_capabilities(
    f_auditor_user_token: str,
    f_owner_org: Organization,
) -> None:
    client = _auth_client(f_auditor_user_token, org=f_owner_org)

    response = client.get('/v1/admin/roles/')

    assert response.status_code == 200

    assert set(response.data.keys()) == {'count', 'next', 'previous', 'results'}

    codes = {role['code'] for role in response.data['results']}

    assert {'organization_owner', 'organization_admin', 'developer', 'auditor'} <= codes

    owner = next(role for role in response.data['results'] if role['code'] == 'organization_owner')

    assert set(owner.keys()) == {'id', 'code', 'name', 'built_in', 'capabilities'}

    assert owner['built_in'] is True

    assert isinstance(owner['capabilities'], list)

    assert 'roles:read' in owner['capabilities']

    assert owner['capabilities'] == sorted(owner['capabilities'])


@pytest.mark.django_db
def test_retrieve_returns_role_with_nested_capabilities(
    f_auditor_user_token: str,
    f_owner_org: Organization,
) -> None:
    role = Role.objects.get(code='auditor')

    client = _auth_client(f_auditor_user_token, org=f_owner_org)

    response = client.get(f'/v1/admin/roles/{role.id}/')

    assert response.status_code == 200

    assert response.data['id'] == str(role.id)

    assert response.data['code'] == 'auditor'

    assert response.data['built_in'] is True

    assert isinstance(response.data['capabilities'], list)

    assert 'audit:read' in response.data['capabilities']

    assert response.data['capabilities'] == sorted(response.data['capabilities'])


@pytest.mark.django_db
def test_list_denied_without_roles_read_capability(
    f_developer_user_token: str,
    f_owner_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_owner_org)

    response = client.get('/v1/admin/roles/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_denied_without_roles_read_capability(
    f_developer_user_token: str,
    f_owner_org: Organization,
) -> None:
    role = Role.objects.get(code='developer')

    client = _auth_client(f_developer_user_token, org=f_owner_org)

    response = client.get(f'/v1/admin/roles/{role.id}/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_unknown_role_returns_404(
    f_auditor_user_token: str,
    f_owner_org: Organization,
) -> None:
    client = _auth_client(f_auditor_user_token, org=f_owner_org)

    response = client.get(f'/v1/admin/roles/{uuid.uuid4()}/')

    assert response.status_code == 404
