from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth.models import User
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
from engram.core.models import AuditEvent, Organization, Project, Team
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope

VALIDATE_URL = '/v1/admin/model-policies/validate'


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
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
    for cap_code in capability_codes:
        RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(cap_code))

    return role


def _client_for_org(username: str, org: Organization, capabilities: tuple[str, ...]) -> APIClient:
    from rest_framework.authtoken.models import Token

    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(f'role_{username}', capabilities)
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


def _make_secret(organization: Organization, team: Team | None = None) -> ProviderSecret:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'secret-{uuid.uuid4()}',
        provider='openai',
        scope='team' if team is not None else 'organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )

    return secret


def _make_policy(
    organization: Organization,
    project: Project,
    secret: ProviderSecret,
    *,
    task_type: str = 'generation',
    name: str = 'policy',
    active: bool = True,
) -> ModelPolicy:
    return ModelPolicy.objects.create(
        organization=organization,
        project=project,
        name=name,
        scope='project',
        task_type=task_type,
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
        active=active,
    )


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='ValidateViewOrg', slug='validate-view-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Proj', slug='validate-view-proj')


@pytest.fixture
def f_admin_client(f_org: Organization) -> APIClient:
    return _client_for_org('validate-admin', f_org, ('model_policy:*', 'secrets:*'))


@pytest.mark.django_db
def test_validate_all_active_policies_returns_results(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='gen')
    _make_policy(f_org, f_project, secret, task_type='curation', name='cur')

    response = f_admin_client.post(VALIDATE_URL, {}, format='json')

    assert response.status_code == 200
    results = response.json()['results']
    assert len(results) == 2
    for item in results:
        assert item['ok'] is True
        assert item['provider'] == 'openai'
        assert item['model'] == 'gpt-4o-mini'
        assert 'latency_ms' in item
        assert item.get('error_code') is None


@pytest.mark.django_db
def test_validate_single_policy_by_id(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    target = _make_policy(f_org, f_project, secret, task_type='generation', name='gen')
    _make_policy(f_org, f_project, secret, task_type='curation', name='cur')

    response = f_admin_client.post(VALIDATE_URL, {'policy_id': str(target.id)}, format='json')

    assert response.status_code == 200
    results = response.json()['results']
    assert len(results) == 1
    assert results[0]['policy_id'] == str(target.id)
    assert results[0]['ok'] is True


@pytest.mark.django_db
def test_validate_sanitizes_provider_error(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='gen')

    class _FailingGateway:
        def call(self, _data: object) -> object:
            raise ModelPolicyError('provider_http_error', 'provider returned 402 raw-key-sk-leak', http_status=402)

    monkeypatch.setattr(
        'engram.model_policy.validation.get_provider_gateway',
        lambda _policy, **_: _FailingGateway(),
    )

    response = f_admin_client.post(VALIDATE_URL, {}, format='json')

    assert response.status_code == 200
    body = json.dumps(response.json())
    assert '402' not in body
    assert 'raw-key-sk-leak' not in body
    assert 'returned' not in body
    results = response.json()['results']
    assert results[0]['ok'] is False
    assert results[0]['error_code'] == 'provider_http_error'
    assert results[0]['public_error']


@pytest.mark.django_db
def test_validate_records_audit_event(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='gen')

    response = f_admin_client.post(VALIDATE_URL, {}, format='json')

    assert response.status_code == 200
    audit = AuditEvent.objects.filter(
        organization=f_org,
        event_type='ModelPolicyValidated',
    ).first()
    assert audit is not None
    assert audit.metadata.get('policy_count') == 1


@pytest.mark.django_db
def test_validate_unknown_policy_id_returns_404(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    response = f_admin_client.post(VALIDATE_URL, {'policy_id': str(uuid.uuid4())}, format='json')

    assert response.status_code == 404


@pytest.mark.django_db
def test_validate_ignores_inactive_policies(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='active-gen')
    _make_policy(f_org, f_project, secret, task_type='curation', name='inactive', active=False)

    response = f_admin_client.post(VALIDATE_URL, {}, format='json')

    results = response.json()['results']
    assert len(results) == 1
    assert results[0]['task_type'] == 'generation'


@pytest.mark.django_db
def test_validate_scopes_to_active_organization(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='own')

    other_org = Organization.objects.create(name='Other', slug='validate-other-org')
    other_project = Project.objects.create(organization=other_org, name='Other', slug='validate-other-proj')
    other_secret = _make_secret(other_org)
    _make_policy(other_org, other_project, other_secret, task_type='generation', name='foreign')

    response = f_admin_client.post(VALIDATE_URL, {}, format='json')

    results = response.json()['results']
    assert len(results) == 1


@pytest.mark.django_db
def test_validate_requires_admin_capability(
    f_org: Organization,
    f_project: Project,
) -> None:
    secret = _make_secret(f_org)
    _make_policy(f_org, f_project, secret, task_type='generation', name='gen')
    read_client = _client_for_org('validate-reader', f_org, ('model_policy:read',))

    response = read_client.post(VALIDATE_URL, {}, format='json')

    assert response.status_code == 403


@pytest.mark.django_db
def test_validate_denied_without_capability(
    f_org: Organization,
    f_project: Project,
) -> None:
    no_cap_client = _client_for_org('validate-nocap', f_org, ('memories:read',))

    response = no_cap_client.post(VALIDATE_URL, {}, format='json')

    assert response.status_code == 403
