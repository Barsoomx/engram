from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.core.models import AuditEvent, Organization, Team


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


def _make_team(organization: Organization, slug: str = 'platform', name: str = 'Platform') -> Team:
    return Team.objects.create(organization=organization, name=name, slug=slug)


@pytest.fixture
def f_owner_user_token() -> str:
    user = _make_user('owner')
    org = Organization.objects.create(name='Acme', slug='acme')
    _make_membership(user, org)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_owner_user() -> User:
    return User.objects.get(username='owner')


@pytest.fixture
def f_owned_org() -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_owned_team(f_owned_org: Organization) -> Team:
    return _make_team(f_owned_org)


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
def test_list_returns_active_teams_with_pagination(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owned_team: Team,
) -> None:
    Team.objects.create(organization=f_owned_org, name='Archived', slug='archived', archived_at='2026-01-01T00:00:00Z')

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/teams/')

    assert response.status_code == 200

    assert set(response.data.keys()) == {'count', 'next', 'previous', 'results'}

    assert response.data['count'] == 1

    team = response.data['results'][0]

    assert set(team.keys()) == {
        'id',
        'name',
        'slug',
        'created_at',
        'updated_at',
        'archived_at',
        'organization',
    }

    assert team['slug'] == 'platform'

    assert team['archived_at'] is None

    assert team['organization'] == f_owned_org.id


@pytest.mark.django_db
def test_list_denied_without_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.get('/v1/admin/teams/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_retrieve_active_team(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owned_team: Team,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/teams/{f_owned_team.id}/')

    assert response.status_code == 200

    assert response.data['id'] == str(f_owned_team.id)

    assert response.data['slug'] == 'platform'


@pytest.mark.django_db
def test_retrieve_returns_404_for_other_org_team(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_other_org: Organization,
) -> None:
    other_team = _make_team(f_other_org, slug='secret', name='Secret')

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/teams/{other_team.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_create_team_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/teams/',
        {'name': 'Backend', 'slug': 'backend'},
    )

    assert response.status_code == 201

    assert response.data['slug'] == 'backend'

    team = Team.objects.get(organization=f_owned_org, slug='backend')

    assert team.name == 'Backend'

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='TeamCreated',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'team'

    assert event.target_id == str(team.id)


@pytest.mark.django_db
def test_create_rejects_duplicate_slug_per_org(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owned_team: Team,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/teams/',
        {'name': 'Other', 'slug': 'platform'},
    )

    assert response.status_code == 400

    assert 'slug' in response.data


@pytest.mark.django_db
def test_create_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.post(
        '/v1/admin/teams/',
        {'name': 'X', 'slug': 'x'},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_patch_updates_team_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owned_team: Team,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.patch(
        f'/v1/admin/teams/{f_owned_team.id}/',
        {'name': 'Platform Reloaded'},
    )

    assert response.status_code == 200

    assert response.data['name'] == 'Platform Reloaded'

    assert response.data['slug'] == 'platform'

    f_owned_team.refresh_from_db()

    assert f_owned_team.name == 'Platform Reloaded'

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='TeamUpdated',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'team'

    assert event.target_id == str(f_owned_team.id)

    assert event.metadata.get('fields') == ['name']


@pytest.mark.django_db
def test_patch_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    dev_org = Organization.objects.create(name='Devother', slug='devother')
    _make_membership(User.objects.get(username='dev'), dev_org, role_code='developer')
    team = _make_team(dev_org)

    client = _auth_client(f_developer_user_token, org=dev_org)

    response = client.patch(
        f'/v1/admin/teams/{team.id}/',
        {'name': 'New'},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_delete_archives_team_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owned_team: Team,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.delete(f'/v1/admin/teams/{f_owned_team.id}/')

    assert response.status_code == 204

    f_owned_team.refresh_from_db()

    assert f_owned_team.archived_at is not None

    assert Team.objects.filter(id=f_owned_team.id).exists()

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='TeamArchived',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'team'

    assert event.target_id == str(f_owned_team.id)


@pytest.mark.django_db
def test_delete_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    dev_org = Organization.objects.create(name='Devother2', slug='devother2')
    _make_membership(User.objects.get(username='dev'), dev_org, role_code='developer')
    team = _make_team(dev_org)

    client = _auth_client(f_developer_user_token, org=dev_org)

    response = client.delete(f'/v1/admin/teams/{team.id}/')

    assert response.status_code == 403

    team.refresh_from_db()

    assert team.archived_at is None
