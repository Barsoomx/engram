from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
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
    org = Organization.objects.create(name='Acme', slug='acme')
    _make_membership(user, org)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_owned_org() -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_owner_membership(f_owned_org: Organization) -> OrganizationMembership:
    return OrganizationMembership.objects.get(organization=f_owned_org)


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
def test_list_returns_members_with_role_and_active(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/members/')

    assert response.status_code == 200

    assert set(response.data.keys()) == {'count', 'next', 'previous', 'results'}

    assert response.data['count'] == 1

    member = response.data['results'][0]

    assert set(member.keys()) == {
        'id',
        'external_id',
        'display_name',
        'email',
        'identity_type',
        'active',
        'status',
        'role',
        'role_name',
    }

    assert member['identity_type'] == 'user'

    assert member['role'] == 'organization_owner'

    assert member['active'] is True


@pytest.mark.django_db
def test_list_denied_without_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.get('/v1/admin/members/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_list_paginates(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    from rest_framework.settings import api_settings

    page_size = int(api_settings.PAGE_SIZE)

    extra_needed = page_size + 1 - 1

    for index in range(extra_needed):
        OrganizationMembership.objects.create(
            organization=f_owned_org,
            identity=Identity.objects.create(
                organization=f_owned_org,
                identity_type=IdentityType.USER,
                external_id=f'member-{index}',
                display_name=f'Member {index}',
            ),
            role=_make_role('developer'),
        )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/members/')

    assert response.status_code == 200

    assert response.data['count'] == page_size + 1

    assert len(response.data['results']) == page_size

    assert response.data['next'] is not None


@pytest.mark.django_db
def test_invite_creates_identity_and_membership_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/members/',
        {
            'external_id': 'bob@acme.test',
            'display_name': 'Bob',
            'email': 'bob@acme.test',
            'role': 'developer',
        },
    )

    assert response.status_code == 201

    assert response.data['external_id'] == 'bob@acme.test'

    assert response.data['role'] == 'developer'

    assert response.data['identity_type'] == 'user'

    assert response.data['active'] is True

    identity = Identity.objects.get(
        organization=f_owned_org,
        external_id='bob@acme.test',
    )

    assert identity.identity_type == IdentityType.USER

    assert identity.display_name == 'Bob'

    membership = OrganizationMembership.objects.get(
        organization=f_owned_org,
        identity=identity,
    )

    assert membership.role.code == 'developer'

    assert membership.active is True

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='MemberInvited',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'member'

    assert event.target_id == str(membership.id)


@pytest.mark.django_db
def test_invite_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.post(
        '/v1/admin/members/',
        {
            'external_id': 'sneaky@acme.test',
            'display_name': 'Sneaky',
            'role': 'developer',
        },
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_invite_rejects_duplicate_external_id(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/members/',
        {
            'external_id': f_owner_membership.identity.external_id,
            'display_name': 'Twin',
            'role': 'developer',
        },
    )

    assert response.status_code == 400

    assert 'external_id' in response.data


@pytest.mark.django_db
def test_invite_rejects_unknown_role(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/members/',
        {
            'external_id': 'carol@acme.test',
            'display_name': 'Carol',
            'role': 'ghost',
        },
    )

    assert response.status_code == 400

    assert 'role' in response.data


@pytest.mark.django_db
def test_patch_changes_role_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    membership = OrganizationMembership.objects.create(
        organization=f_owned_org,
        identity=Identity.objects.create(
            organization=f_owned_org,
            identity_type=IdentityType.USER,
            external_id='dave@acme.test',
            display_name='Dave',
        ),
        role=_make_role('developer'),
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.patch(
        f'/v1/admin/members/{membership.id}/',
        {'role': 'auditor'},
    )

    assert response.status_code == 200

    assert response.data['role'] == 'auditor'

    membership.refresh_from_db()

    assert membership.role.code == 'auditor'

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='MemberRoleChanged',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'member'

    assert event.target_id == str(membership.id)


@pytest.mark.django_db
def test_patch_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    org = Organization.objects.create(name='Devpatch', slug='devpatch')
    membership = OrganizationMembership.objects.create(
        organization=org,
        identity=Identity.objects.create(
            organization=org,
            identity_type=IdentityType.USER,
            external_id='eve@devpatch.test',
            display_name='Eve',
        ),
        role=_make_role('developer'),
    )
    _make_membership(User.objects.get(username='dev'), org, role_code='developer')

    client = _auth_client(f_developer_user_token, org=org)

    response = client.patch(
        f'/v1/admin/members/{membership.id}/',
        {'role': 'auditor'},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_delete_deactivates_membership_and_writes_audit(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    membership = OrganizationMembership.objects.create(
        organization=f_owned_org,
        identity=Identity.objects.create(
            organization=f_owned_org,
            identity_type=IdentityType.USER,
            external_id='frank@acme.test',
            display_name='Frank',
        ),
        role=_make_role('developer'),
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.delete(f'/v1/admin/members/{membership.id}/')

    assert response.status_code == 204

    membership.refresh_from_db()

    assert membership.active is False

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='MemberRemoved',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'member'

    assert event.target_id == str(membership.id)


@pytest.mark.django_db
def test_delete_denied_without_admin_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    org = Organization.objects.create(name='Devdel', slug='devdel')
    membership = OrganizationMembership.objects.create(
        organization=org,
        identity=Identity.objects.create(
            organization=org,
            identity_type=IdentityType.USER,
            external_id='gina@devdel.test',
            display_name='Gina',
        ),
        role=_make_role('developer'),
    )
    _make_membership(User.objects.get(username='dev'), org, role_code='developer')

    client = _auth_client(f_developer_user_token, org=org)

    response = client.delete(f'/v1/admin/members/{membership.id}/')

    assert response.status_code == 403

    membership.refresh_from_db()

    assert membership.active is True


@pytest.mark.django_db
def test_delete_returns_409_when_removing_last_owner(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.delete(f'/v1/admin/members/{f_owner_membership.id}/')

    assert response.status_code == 409

    assert response.data['code'] == 'last_owner'

    f_owner_membership.refresh_from_db()

    assert f_owner_membership.active is True


@pytest.mark.django_db
def test_patch_demoting_last_owner_returns_409(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.patch(
        f'/v1/admin/members/{f_owner_membership.id}/',
        {'role': 'developer'},
    )

    assert response.status_code == 409

    assert response.data['code'] == 'last_owner'

    f_owner_membership.refresh_from_db()

    assert f_owner_membership.role.code == 'organization_owner'


@pytest.mark.django_db
def test_delete_can_remove_owner_when_another_active_owner_exists(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    second_owner = OrganizationMembership.objects.create(
        organization=f_owned_org,
        identity=Identity.objects.create(
            organization=f_owned_org,
            identity_type=IdentityType.USER,
            external_id='co-owner@acme.test',
            display_name='Co-owner',
        ),
        role=_make_role('organization_owner'),
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.delete(f'/v1/admin/members/{second_owner.id}/')

    assert response.status_code == 204

    second_owner.refresh_from_db()

    assert second_owner.active is False


@pytest.mark.django_db
def test_retrieve_returns_404_for_other_org_member(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_other_org: Organization,
) -> None:
    other_membership = OrganizationMembership.objects.create(
        organization=f_other_org,
        identity=Identity.objects.create(
            organization=f_other_org,
            identity_type=IdentityType.USER,
            external_id='secret@globex.test',
            display_name='Secret',
        ),
        role=_make_role('organization_owner'),
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/members/{other_membership.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_read_serializer_never_returns_credentials(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/members/')

    assert response.status_code == 200

    member = response.data['results'][0]

    for forbidden in ('password', 'key_hash', 'token', 'secret'):
        assert forbidden not in member
        assert forbidden not in str(member).lower()


@pytest.mark.django_db
def test_list_includes_role_name(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_membership: OrganizationMembership,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/members/')

    assert response.status_code == 200

    member = response.data['results'][0]

    assert 'role_name' in member

    assert member['role_name'] == f_owner_membership.role.name


@pytest.mark.django_db
def test_invited_member_has_status_invited(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/members/',
        {
            'external_id': 'new@acme.test',
            'display_name': 'New Member',
            'email': 'new@acme.test',
            'role': 'developer',
        },
    )

    assert response.status_code == 201

    assert response.data['status'] == 'invited'


@pytest.mark.django_db
def test_existing_membership_defaults_status_active() -> None:
    org = Organization.objects.create(name='Status Org', slug='status-org')

    membership = OrganizationMembership.objects.create(
        organization=org,
        identity=Identity.objects.create(
            organization=org,
            identity_type=IdentityType.USER,
            external_id='direct@status-org.test',
            display_name='Direct',
        ),
        role=_make_role('developer'),
    )

    assert membership.status == 'active'
