from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.access.services import (
    AccessDeniedError,
    ResolveApiKeyScope,
    hash_api_key,
)
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


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


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
def f_owner_identity(f_owned_org: Organization) -> Identity:
    return Identity.objects.get(
        organization=f_owned_org,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(User.objects.get(username='owner')),
    )


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


@pytest.fixture
def f_other_org_identity(f_other_org: Organization) -> Identity:
    return Identity.objects.create(
        organization=f_other_org,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='globex-agent',
        display_name='Globex agent',
    )


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    capabilities = [_ensure_capability(code) for code in capability_codes]

    for capability in capabilities:
        from engram.access.models import RoleCapability

        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


@pytest.fixture
def f_scoped_issuer_user_token() -> str:
    user = _make_user('scoped-issuer')
    org = Organization.objects.create(name='Scopedco', slug='scopedco')
    identity = _make_identity(user, org)

    role = _make_role_with_capabilities(
        'scoped_issuer',
        ('api_keys:issue', 'observations:read'),
    )

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_scoped_issuer_org() -> Organization:
    return Organization.objects.get(slug='scopedco')


def _auth_client(token: str, org: Organization | None = None) -> APIClient:
    client = APIClient()

    headers: dict[str, str] = {'HTTP_AUTHORIZATION': f'Token {token}'}

    if org is not None:
        headers['HTTP_X_ENGRAM_ORGANIZATION'] = str(org.id)

    client.credentials(**headers)

    return client


@pytest.mark.django_db
def test_issue_returns_plaintext_once_and_persists_hash(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_owner_identity: Identity,
) -> None:
    _ensure_capability('api_keys:issue')
    _ensure_capability('observations:write')

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Agent key',
            'capabilities': ['observations:write'],
        },
    )

    assert response.status_code == 201

    plaintext = response.data['plaintext']

    assert plaintext.startswith('egk_')

    assert len(plaintext) > len('egk_')

    assert response.data['name'] == 'Agent key'

    assert response.data['key_prefix']

    assert response.data['key_fingerprint']

    assert response.data['capabilities'] == ['observations:write']

    api_key = ApiKey.objects.get(organization=f_owned_org, name='Agent key')

    assert api_key.key_hash == hash_api_key(plaintext)

    assert api_key.key_prefix == response.data['key_prefix']

    assert api_key.key_fingerprint == response.data['key_fingerprint']

    assert api_key.owner_identity_id == f_owner_identity.id

    raw_columns = {field.name for field in ApiKey._meta.get_fields()}

    assert 'plaintext' not in raw_columns

    assert ApiKeyCapability.objects.filter(api_key=api_key).count() == 1

    assert ApiKeyCapability.objects.filter(
        api_key=api_key,
        capability__code='observations:write',
    ).exists()


@pytest.mark.django_db
def test_issue_writes_audit_event(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    _ensure_capability('observations:read')

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Audited key',
            'capabilities': ['observations:read'],
        },
    )

    assert response.status_code == 201

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='ApiKeyIssued',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.target_type == 'api_key'

    assert event.target_id == response.data['id']


@pytest.mark.django_db
def test_issue_rejects_capability_outside_issuer_scope(
    f_scoped_issuer_user_token: str,
    f_scoped_issuer_org: Organization,
) -> None:
    _ensure_capability('secrets:admin')

    client = _auth_client(f_scoped_issuer_user_token, org=f_scoped_issuer_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Wide key',
            'capabilities': ['secrets:admin'],
        },
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_issue_rejects_capability_outside_issuer_scope_writes_denial_audit(
    f_scoped_issuer_user_token: str,
    f_scoped_issuer_org: Organization,
) -> None:
    _ensure_capability('secrets:admin')

    client = _auth_client(f_scoped_issuer_user_token, org=f_scoped_issuer_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Wide key',
            'capabilities': ['secrets:admin'],
        },
    )

    assert response.status_code == 400

    audit = AuditEvent.objects.filter(
        organization=f_scoped_issuer_org,
        event_type='ApiKeyIssueDenied',
    )

    assert audit.count() == 1

    event = audit.get()

    assert event.result == AuditResult.DENIED

    assert event.metadata['requested_capabilities'] == ['secrets:admin']

    assert not ApiKey.objects.filter(name='Wide key').exists()


@pytest.mark.django_db
def test_issue_rejects_unknown_capability_code(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Bogus key',
            'capabilities': ['api_keys:nonexistent_capability'],
        },
    )

    assert response.status_code == 400

    assert response.data['code'] == 'unknown_capability'

    assert 'api_keys:nonexistent_capability' in response.data['detail']

    assert not ApiKey.objects.filter(name='Bogus key').exists()


@pytest.mark.django_db
def test_issue_allows_wildcard_issuer_capability(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    _ensure_capability('api_keys:issue')

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Scoped key',
            'capabilities': ['api_keys:issue'],
        },
    )

    assert response.status_code == 201

    assert response.data['capabilities'] == ['api_keys:issue']


@pytest.mark.django_db
def test_issue_denied_without_issue_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    _ensure_capability('observations:read')

    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.post(
        '/v1/admin/api-keys/',
        {
            'name': 'Forbidden key',
            'capabilities': ['observations:read'],
        },
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_list_never_exposes_plaintext_or_hash(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    _ensure_capability('observations:read')

    issue_response = _auth_client(f_owner_user_token, org=f_owned_org).post(
        '/v1/admin/api-keys/',
        {
            'name': 'Listed key',
            'capabilities': ['observations:read'],
        },
    )

    plaintext = issue_response.data['plaintext']

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/api-keys/')

    assert response.status_code == 200

    payload = str(response.data['results'])

    assert plaintext not in payload

    for forbidden in ('plaintext', 'key_hash'):
        assert forbidden not in payload

    member = response.data['results'][0]

    assert set(member.keys()) == {
        'id',
        'name',
        'key_prefix',
        'key_fingerprint',
        'owner_identity',
        'capabilities',
        'created_at',
        'expires_at',
        'last_used_at',
        'active',
        'revoked_at',
    }


@pytest.mark.django_db
def test_retrieve_never_exposes_plaintext(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    _ensure_capability('observations:read')

    issue_response = _auth_client(f_owner_user_token, org=f_owned_org).post(
        '/v1/admin/api-keys/',
        {
            'name': 'Detail key',
            'capabilities': ['observations:read'],
        },
    )

    plaintext = issue_response.data['plaintext']

    key_id = issue_response.data['id']

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/api-keys/{key_id}/')

    assert response.status_code == 200

    payload = str(response.data)

    assert plaintext not in payload

    assert 'plaintext' not in response.data

    assert 'key_hash' not in response.data


@pytest.mark.django_db
def test_list_scoped_to_active_organization(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_other_org: Organization,
    f_other_org_identity: Identity,
) -> None:
    _ensure_capability('observations:read')

    ApiKey.objects.create(
        organization=f_other_org,
        owner_identity=f_other_org_identity,
        name='Leaked key',
        key_prefix='egk_leakedxxx',
        key_hash='hash_leaked',
        key_fingerprint='egk_leakedxxx...leakeddigest',
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get('/v1/admin/api-keys/')

    assert response.status_code == 200

    names = [entry['name'] for entry in response.data['results']]

    assert 'Leaked key' not in names


@pytest.mark.django_db
def test_retrieve_returns_404_for_other_org_key(
    f_owner_user_token: str,
    f_owned_org: Organization,
    f_other_org: Organization,
    f_other_org_identity: Identity,
) -> None:
    other_key = ApiKey.objects.create(
        organization=f_other_org,
        owner_identity=f_other_org_identity,
        name='Foreign key',
        key_prefix='egk_foreignxx',
        key_hash='hash_foreign',
        key_fingerprint='egk_foreignxx...foreigndigest',
    )

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    response = client.get(f'/v1/admin/api-keys/{other_key.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_revoke_blocks_auth_via_resolve_scope(
    f_owner_user_token: str,
    f_owned_org: Organization,
) -> None:
    _ensure_capability('observations:read')

    issue_response = _auth_client(f_owner_user_token, org=f_owned_org).post(
        '/v1/admin/api-keys/',
        {
            'name': 'Revocable key',
            'capabilities': ['observations:read'],
        },
    )

    plaintext = issue_response.data['plaintext']

    key_id = issue_response.data['id']

    client = _auth_client(f_owner_user_token, org=f_owned_org)

    revoke_response = client.post(f'/v1/admin/api-keys/{key_id}/revoke/')

    assert revoke_response.status_code == 200

    api_key = ApiKey.objects.get(id=key_id)

    assert api_key.revoked_at is not None

    audit = AuditEvent.objects.filter(
        organization=f_owned_org,
        event_type='ApiKeyRevoked',
    )

    assert audit.count() == 1

    assert audit.get().target_id == str(key_id)

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=plaintext,
            required_capability='observations:read',
        )

    assert exc_info.value.code == 'revoked_key'


@pytest.mark.django_db
def test_revoke_denied_without_revoke_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
    f_other_org_identity: Identity,
) -> None:
    key = ApiKey.objects.create(
        organization=f_other_org,
        owner_identity=f_other_org_identity,
        name='Protected key',
        key_prefix='egk_protected',
        key_hash='hash_protected',
        key_fingerprint='egk_protected...protecteddigest',
    )

    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.post(f'/v1/admin/api-keys/{key.id}/revoke/')

    assert response.status_code == 403

    key.refresh_from_db()

    assert key.revoked_at is None


@pytest.mark.django_db
def test_list_denied_without_read_capability(
    f_developer_user_token: str,
    f_other_org: Organization,
) -> None:
    client = _auth_client(f_developer_user_token, org=f_other_org)

    response = client.get('/v1/admin/api-keys/')

    assert response.status_code == 403
