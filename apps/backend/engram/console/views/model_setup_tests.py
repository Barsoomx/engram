from __future__ import annotations

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
from engram.core.models import Organization, Project
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope


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
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(f'role_{username}', capabilities)
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.fixture
def f_setup_org() -> Organization:
    return Organization.objects.create(name='SetupOrg', slug='setup-org')


@pytest.fixture
def f_setup_project(f_setup_org: Organization) -> Project:
    return Project.objects.create(organization=f_setup_org, name='Proj', slug='setup-proj')


@pytest.fixture
def f_admin_client(f_setup_org: Organization) -> APIClient:
    return _client_for_org('setup-admin', f_setup_org, ('model_policy:*', 'secrets:*'))


@pytest.fixture
def f_read_client(f_setup_org: Organization) -> APIClient:
    return _client_for_org('setup-reader', f_setup_org, ('model_policy:read',))


@pytest.fixture
def f_policy_only_client(f_setup_org: Organization) -> APIClient:
    return _client_for_org('setup-policy-only', f_setup_org, ('model_policy:*',))


# ─── Status ──────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_status_no_policies_returns_all_unconfigured(
    f_admin_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_admin_client.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(f_setup_project.id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body['ready'] is False
    assert len(body['task_types']) == 4
    for tt in body['task_types']:
        assert tt['configured'] is False
        assert tt['policy_id'] is None


@pytest.mark.django_db
def test_status_with_one_policy_shows_configured(
    f_admin_client: APIClient,
    f_setup_org: Organization,
    f_setup_project: Project,
) -> None:
    secret = ProviderSecret.objects.create(
        organization=f_setup_org,
        name='test-secret',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=f_setup_org,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted',
        hmac_digest='digest',
        active=True,
    )
    ModelPolicy.objects.create(
        organization=f_setup_org,
        project=f_setup_project,
        name='gen-policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4o',
        secret=secret,
        version=1,
    )

    response = f_admin_client.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(f_setup_project.id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body['ready'] is False
    gen_status = next(t for t in body['task_types'] if t['task_type'] == 'generation')
    assert gen_status['configured'] is True
    assert gen_status['provider'] == 'openai'
    assert gen_status['model'] == 'gpt-4o'
    others = [t for t in body['task_types'] if t['task_type'] != 'generation']
    for t in others:
        assert t['configured'] is False


@pytest.mark.django_db
def test_status_requires_model_policy_read(
    f_read_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_read_client.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(f_setup_project.id)},
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_status_denied_without_capability(
    f_setup_org: Organization,
    f_setup_project: Project,
) -> None:
    no_cap_client = _client_for_org('no-cap', f_setup_org, ('memories:read',))
    response = no_cap_client.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(f_setup_project.id)},
    )

    assert response.status_code == 403


# ─── Presets ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_presets_returns_four_presets(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/model-setup/presets')

    assert response.status_code == 200
    body = response.json()
    assert len(body['presets']) == 4
    keys = {p['key'] for p in body['presets']}
    assert keys == {'anthropic_openai', 'openai_all', 'deepseek_openai', 'glm_openai'}


@pytest.mark.django_db
def test_presets_each_has_four_task_types(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/model-setup/presets')

    body = response.json()
    for preset in body['presets']:
        assert len(preset['task_models']) == 4, f'preset {preset["key"]} has wrong task count'
        task_types = {tm['task_type'] for tm in preset['task_models']}
        assert task_types == {'generation', 'embedding', 'curation', 'digest'}


@pytest.mark.django_db
def test_presets_embedding_always_openai(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/model-setup/presets')

    body = response.json()
    for preset in body['presets']:
        embedding = next(tm for tm in preset['task_models'] if tm['task_type'] == 'embedding')
        assert embedding['provider'] == 'openai', f'preset {preset["key"]} embedding not openai'


# ─── Apply ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_apply_deepseek_openai_creates_secrets_and_policies(
    f_admin_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'deepseek_openai',
            'provider_keys': {'deepseek': 'sk-deepseek-key', 'openai': 'sk-openai-key'},
            'request_id': 'req-apply-ds-1',
        },
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body['created_secret_ids']) == 2
    assert len(body['created_policy_ids']) == 4
    assert body['status']['ready'] is True
    for tt in body['status']['task_types']:
        assert tt['configured'] is True


@pytest.mark.django_db
def test_apply_deepseek_openai_status_ready_via_get(
    f_admin_client: APIClient,
    f_setup_project: Project,
) -> None:
    f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'deepseek_openai',
            'provider_keys': {'deepseek': 'sk-deepseek-key', 'openai': 'sk-openai-key'},
            'request_id': 'req-apply-ds-get-1',
        },
        format='json',
    )

    status_response = f_admin_client.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(f_setup_project.id)},
    )

    assert status_response.status_code == 200
    assert status_response.json()['ready'] is True


@pytest.mark.django_db
def test_apply_glm_openai_uses_base_url_and_separate_secrets(
    f_admin_client: APIClient,
    f_setup_project: Project,
    f_setup_org: Organization,
) -> None:
    response = f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'glm_openai',
            'provider_keys': {'glm': 'sk-glm-key', 'openai': 'sk-openai-key'},
            'request_id': 'req-apply-glm-1',
        },
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body['created_secret_ids']) == 2

    gen_policy = ModelPolicy.objects.get(
        organization=f_setup_org,
        task_type='generation',
        active=True,
    )
    assert gen_policy.provider == 'openai'
    assert gen_policy.metadata.get('base_url') == 'https://api.z.ai/api/paas/v4'

    emb_policy = ModelPolicy.objects.get(
        organization=f_setup_org,
        task_type='embedding',
        active=True,
    )
    assert emb_policy.provider == 'openai'
    assert not emb_policy.metadata.get('base_url')

    assert gen_policy.secret_id != emb_policy.secret_id


@pytest.mark.django_db
def test_apply_rerun_disables_prior_active_policies(
    f_admin_client: APIClient,
    f_setup_project: Project,
    f_setup_org: Organization,
) -> None:
    f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'openai_all',
            'provider_keys': {'openai': 'sk-openai-key'},
            'request_id': 'req-apply-first',
        },
        format='json',
    )

    response = f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'openai_all',
            'provider_keys': {'openai': 'sk-openai-key-2'},
            'request_id': 'req-apply-second',
        },
        format='json',
    )

    assert response.status_code == 200
    active_count = ModelPolicy.objects.filter(
        organization=f_setup_org,
        active=True,
    ).count()
    assert active_count == 4


@pytest.mark.django_db
def test_apply_missing_provider_key_returns_400_and_nothing_created(
    f_admin_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'deepseek_openai',
            'provider_keys': {'deepseek': 'sk-deepseek-key'},
            'request_id': 'req-apply-missing',
        },
        format='json',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'missing_provider_key'
    assert ModelPolicy.objects.count() == 0
    assert ProviderSecret.objects.count() == 0


@pytest.mark.django_db
def test_apply_without_model_policy_capability_returns_403(
    f_read_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_read_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'openai_all',
            'provider_keys': {'openai': 'sk-key'},
            'request_id': 'req-apply-denied',
        },
        format='json',
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_apply_without_secrets_capability_returns_403(
    f_policy_only_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_policy_only_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'openai_all',
            'provider_keys': {'openai': 'sk-key'},
            'request_id': 'req-apply-no-secrets',
        },
        format='json',
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_apply_unknown_preset_key_returns_404(
    f_admin_client: APIClient,
    f_setup_project: Project,
) -> None:
    response = f_admin_client.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(f_setup_project.id),
            'scope': 'project',
            'preset_key': 'nonexistent_preset',
            'provider_keys': {'openai': 'sk-key'},
            'request_id': 'req-apply-unknown',
        },
        format='json',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'preset_not_found'


@pytest.mark.django_db
def test_apply_tenant_isolation() -> None:
    org_a = Organization.objects.create(name='OrgA', slug='ms-org-a')
    org_b = Organization.objects.create(name='OrgB', slug='ms-org-b')
    project_a = Project.objects.create(organization=org_a, name='ProjA', slug='ms-proj-a')
    project_b = Project.objects.create(organization=org_b, name='ProjB', slug='ms-proj-b')

    client_a = _client_for_org('ms-admin-a', org_a, ('model_policy:*', 'secrets:*'))
    client_b = _client_for_org('ms-admin-b', org_b, ('model_policy:*', 'secrets:*'))

    client_b.post(
        '/v1/admin/model-setup/apply',
        {
            'project_id': str(project_b.id),
            'scope': 'project',
            'preset_key': 'openai_all',
            'provider_keys': {'openai': 'sk-key'},
            'request_id': 'req-org-b',
        },
        format='json',
    )

    response = client_a.get(
        '/v1/admin/model-setup/status',
        {'project_id': str(project_a.id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body['ready'] is False
    for tt in body['task_types']:
        assert tt['configured'] is False
