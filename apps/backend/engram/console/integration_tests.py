from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    ApiKey,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.access.services import AccessDeniedError, ResolveApiKeyScope
from engram.core.models import AuditEvent, AuditResult, Organization


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
def f_owned_org() -> Organization:
    return Organization.objects.create(name='Acme', slug='acme')


@pytest.fixture
def f_owner_token(f_owned_org: Organization) -> str:
    user = _make_user('owner')
    _make_membership(user, f_owned_org)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


def _auth_client(token: str, org: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.mark.django_db
def test_full_admin_flow_writes_audit_events_and_revokes_key(
    f_owner_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_token, f_owned_org)

    team_response = client.post(
        '/v1/admin/teams/',
        {'name': 'Platform', 'slug': 'platform'},
    )

    assert team_response.status_code == 201

    project_response = client.post(
        '/v1/admin/projects/',
        {
            'name': 'Core',
            'slug': 'core',
            'repository_url': 'https://example.test/core.git',
            'default_branch': 'main',
        },
    )

    assert project_response.status_code == 201

    member_response = client.post(
        '/v1/admin/members/',
        {
            'external_id': 'bob@acme.test',
            'display_name': 'Bob',
            'email': 'bob@acme.test',
            'role': 'developer',
        },
    )

    assert member_response.status_code == 201

    key_response = client.post(
        '/v1/admin/api-keys/',
        {'name': 'Agent key', 'capabilities': ['observations:read']},
    )

    assert key_response.status_code == 201

    plaintext = key_response.data['plaintext']

    key_id = key_response.data['id']

    revoke_response = client.post(f'/v1/admin/api-keys/{key_id}/revoke/')

    assert revoke_response.status_code == 200

    expected_event_types = {
        'TeamCreated': 'team',
        'ProjectCreated': 'project',
        'MemberInvited': 'member',
        'ApiKeyIssued': 'api_key',
        'ApiKeyRevoked': 'api_key',
    }

    events = AuditEvent.objects.filter(
        organization=f_owned_org,
        actor_type='user',
    )

    actual_event_types = {event.event_type: event.target_type for event in events}

    for event_type, target_type in expected_event_types.items():
        assert event_type in actual_event_types, f'missing audit event {event_type}'

        assert actual_event_types[event_type] == target_type

    for event in events:
        assert event.metadata is not None

        assert event.metadata != {}

    revoked_audit = events.get(event_type='ApiKeyRevoked')

    assert revoked_audit.result == AuditResult.RECORDED

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=plaintext,
            required_capability='observations:read',
        )

    assert exc_info.value.code == 'revoked_key'

    api_key = ApiKey.objects.get(id=key_id)

    assert api_key.revoked_at is not None


@pytest.mark.django_db
def test_openapi_schema_lists_admin_paths() -> None:
    client = APIClient()

    response = client.get('/api/schema/', HTTP_ACCEPT='application/json')

    assert response.status_code == 200

    payload = json.loads(response.content)

    paths = set(payload.get('paths', {}).keys())

    assert any(path.startswith('/v1/admin/') for path in paths)


@pytest.mark.django_db
def test_denied_capability_writes_denied_audit_event(
    f_owned_org: Organization,
) -> None:
    from rest_framework.authtoken.models import Token

    dev_user = _make_user('dev-user')

    _make_membership(dev_user, f_owned_org, role_code='developer')

    dev_token = Token.objects.get_or_create(user=dev_user)[0].key

    dev_client = APIClient()

    dev_client.credentials(
        HTTP_AUTHORIZATION=f'Token {dev_token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id),
    )

    denied_response = dev_client.post(
        '/v1/admin/api-keys/',
        {'name': 'Forbidden', 'capabilities': ['observations:read']},
    )

    assert denied_response.status_code == 403

    denied_audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='AccessDenied',
        result=AuditResult.DENIED,
    )

    assert denied_audit.count() == 1

    event = denied_audit.get()

    assert event.metadata.get('required_capability') == 'api_keys:issue'
