from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from rest_framework.test import APIClient

from engram.access.models import ApiKeyCapability, Capability, Identity, OrganizationMembership, ProjectGrant, Role
from engram.context.context_api_tests import auth_headers, create_project_scope, create_scoped_api_key
from engram.core.models import AuditEvent, Team
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    FakeProviderGateway,
    ModelPolicyError,
    ProviderCallInput,
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    encryption_key,
)

POLICY_RAW_KEY = 'egk_test_model_policy_admin_0123456789abcdefghijklmnopqrstuvwxyz'
RAW_PROVIDER_SECRET = 'sk-test_model_policy_secret_1234567890abcdef'


def create_policy_admin_key(project_team_scope: tuple[object, Team, object, object, object]) -> None:
    organization, team, project, _owner, _api_key = project_team_scope
    admin = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-model-policy-admin',
        display_name='Model policy admin',
    )
    admin_role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=admin, role=admin_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=admin, role=admin_role)
    api_key = create_scoped_api_key(
        organization,
        team,
        project,
        admin,
        raw_key=POLICY_RAW_KEY,
        capabilities=('secrets:*', 'model_policy:*', 'projects:*', 'teams:*'),
    )
    for code in ('secrets:*', 'model_policy:*'):
        ApiKeyCapability.objects.get_or_create(
            api_key=api_key,
            capability=Capability.objects.get(code=code),
        )


@pytest.mark.django_db
def test_provider_secret_create_and_detail_store_encrypted_envelope_without_raw_secret() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()

    denied_response = client.post(
        '/v1/model-policy/secrets',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Team OpenAI',
            'provider': 'openai',
            'scope': 'team',
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-secret-create-1',
        },
        format='json',
        **auth_headers(),
    )
    response = client.post(
        '/v1/model-policy/secrets',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Team OpenAI',
            'provider': 'openai',
            'scope': 'team',
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-secret-create-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert denied_response.status_code == 403
    assert denied_response.json()['code'] == 'missing_capability'

    assert response.status_code == 201
    body = response.json()
    assert body['provider'] == 'openai'
    assert body['scope'] == 'team'
    assert body['current_version'] == 1
    assert body['active'] is True
    assert 'raw_secret' not in body
    assert RAW_PROVIDER_SECRET not in str(body)

    secret = ProviderSecret.objects.get(id=body['id'])
    envelope = ProviderSecretEnvelope.objects.get(secret=secret, active=True)
    assert envelope.version == 1
    assert envelope.key_version == 'v1'
    assert envelope.ciphertext
    assert envelope.hmac_digest
    assert RAW_PROVIDER_SECRET not in envelope.ciphertext
    assert RAW_PROVIDER_SECRET not in envelope.hmac_digest
    assert RAW_PROVIDER_SECRET not in str(AuditEvent.objects.filter(target_id=str(secret.id)).values('metadata'))

    detail_response = client.get(
        f'/v1/model-policy/secrets/{secret.id}',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert detail_response.status_code == 200
    assert detail_response.json()['id'] == str(secret.id)
    assert RAW_PROVIDER_SECRET not in str(detail_response.json())


@pytest.mark.django_db
def test_provider_secret_rotation_and_disable_preserve_versions_and_block_provider_calls() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    create_response = client.post(
        '/v1/model-policy/secrets',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Team OpenAI',
            'provider': 'openai',
            'scope': 'team',
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-secret-create-2',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )
    secret_id = create_response.json()['id']
    rotated_secret = 'sk-test_model_policy_rotated_1234567890abcdef'

    rotate_response = client.post(
        f'/v1/model-policy/secrets/{secret_id}/rotate',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'raw_secret': rotated_secret,
            'request_id': 'request-secret-rotate-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert rotate_response.status_code == 200
    assert rotate_response.json()['current_version'] == 2
    assert ProviderSecretEnvelope.objects.filter(secret_id=secret_id).count() == 2
    assert ProviderSecretEnvelope.objects.get(secret_id=secret_id, version=1).active is False
    assert ProviderSecretEnvelope.objects.get(secret_id=secret_id, version=2).active is True
    assert rotated_secret not in str(ProviderSecretEnvelope.objects.filter(secret_id=secret_id).values())
    assert rotated_secret not in str(AuditEvent.objects.filter(target_id=str(secret_id)).values('metadata'))

    policy_response = client.post(
        '/v1/model-policy/policies',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Team policy',
            'scope': 'team',
            'task_type': 'embedding',
            'provider': 'openai',
            'model': 'text-embedding-3-small',
            'secret_id': secret_id,
            'request_id': 'request-policy-create-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )
    assert policy_response.status_code == 201

    disable_response = client.post(
        f'/v1/model-policy/secrets/{secret_id}/disable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-secret-disable-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert disable_response.status_code == 200
    assert disable_response.json()['active'] is False

    with pytest.raises(ModelPolicyError, match='Model policy was not found'):
        ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                task_type='embedding',
            ),
        )

    policy = ModelPolicy.objects.get(id=policy_response.json()['id'])
    with pytest.raises(ProviderSecretError, match='provider secret is disabled'):
        FakeProviderGateway().call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                policy=policy,
                request_id='request-provider-call-disabled-1',
                trace_id='trace-provider-call-disabled-1',
                prompt='embed this',
            ),
        )


@pytest.mark.django_db
def test_model_policy_resolution_prefers_project_then_team_then_organization_and_rejects_cross_scope() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    org_secret = ProviderSecret.objects.create(
        organization=organization,
        name='Org Anthropic',
        provider='anthropic',
        scope='organization',
        current_version=1,
    )
    team_secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    project_secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Project selected OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    for secret in (org_secret, team_secret, project_secret):
        ProviderSecretEnvelope.objects.create(
            organization=organization,
            team_id=secret.team_id,
            secret=secret,
            version=1,
            key_version='v1',
            ciphertext=f'encrypted-{secret.provider}',
            hmac_digest=f'hmac-{secret.id}',
            active=True,
        )
    ModelPolicy.objects.create(
        organization=organization,
        name='Org policy',
        scope='organization',
        task_type='generation',
        provider='anthropic',
        model='claude-3-5-haiku',
        secret=org_secret,
        version=1,
    )
    ModelPolicy.objects.create(
        organization=organization,
        team=team,
        name='Team policy',
        scope='team',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=team_secret,
        version=1,
    )
    project_policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Project policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1',
        secret=project_secret,
        version=1,
    )

    response = client.get(
        '/v1/model-policy/resolve',
        {'project_id': str(project.id), 'team_id': str(team.id), 'task_type': 'generation'},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200
    assert response.json()['policy_id'] == str(project_policy.id)
    assert response.json()['provider'] == 'openai'
    assert response.json()['model'] == 'gpt-4.1'
    assert response.json()['secret_id'] == str(project_secret.id)

    current_team_resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            task_type='generation',
        ),
    )
    current_team_result = FakeProviderGateway().call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            policy=current_team_resolved.policy,
            request_id='request-provider-call-openai-generation-1',
            trace_id='trace-provider-call-openai-generation-1',
            prompt='generate with project override',
        ),
    )
    assert current_team_result.provider == 'openai'
    assert current_team_result.model == 'gpt-4.1'

    other_team = Team.objects.create(organization=organization, name='Support', slug='support')
    project.team_links.create(organization=organization, team=other_team)
    other_team_resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=other_team.id,
            task_type='generation',
        ),
    )
    assert other_team_resolved.policy.secret_id == org_secret.id
    other_team_result = FakeProviderGateway().call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=other_team.id,
            policy=other_team_resolved.policy,
            request_id='request-provider-call-anthropic-generation-1',
            trace_id='trace-provider-call-anthropic-generation-1',
            prompt='generate with org fallback',
        ),
    )
    assert other_team_result.provider == 'anthropic'
    assert other_team_result.model == 'claude-3-5-haiku'

    cross_scope_response = client.post(
        '/v1/model-policy/policies',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Invalid policy',
            'scope': 'project',
            'task_type': 'generation',
            'provider': 'openai',
            'model': 'gpt-4.1',
            'secret_id': str(team_secret.id),
            'scope_team_id': str(other_team.id),
            'request_id': 'request-policy-cross-scope-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert cross_scope_response.status_code == 400
    assert cross_scope_response.json()['code'] == 'policy_scope_mismatch'


@pytest.mark.django_db
def test_provider_secret_detail_and_rotation_hide_other_team_secret() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    other_team = Team.objects.create(organization=organization, name='Support', slug='support')
    project.team_links.create(organization=organization, team=other_team)
    other_secret = ProviderSecret.objects.create(
        organization=organization,
        team=other_team,
        name='Support OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    client = APIClient()

    detail_response = client.get(
        f'/v1/model-policy/secrets/{other_secret.id}',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )
    rotate_response = client.post(
        f'/v1/model-policy/secrets/{other_secret.id}/rotate',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-cross-team-rotate-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert detail_response.status_code == 404
    assert rotate_response.status_code == 403
    assert rotate_response.json()['code'] == 'secret_scope_denied'
    assert ProviderSecretEnvelope.objects.filter(secret=other_secret).count() == 0


@pytest.mark.django_db
def test_team_scoped_secret_admin_cannot_create_rotate_or_disable_organization_secret() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    org_secret = ProviderSecret.objects.create(
        organization=organization,
        name='Org OpenAI',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        secret=org_secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )
    client = APIClient()

    create_response = client.post(
        '/v1/model-policy/secrets',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Invalid org secret',
            'provider': 'openai',
            'scope': 'organization',
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-cross-scope-org-secret-create-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )
    rotate_response = client.post(
        f'/v1/model-policy/secrets/{org_secret.id}/rotate',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': 'request-cross-scope-org-secret-rotate-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )
    disable_response = client.post(
        f'/v1/model-policy/secrets/{org_secret.id}/disable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-cross-scope-org-secret-disable-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert create_response.status_code == 403
    assert create_response.json()['code'] == 'secret_scope_denied'
    assert rotate_response.status_code == 403
    assert rotate_response.json()['code'] == 'secret_scope_denied'
    assert disable_response.status_code == 403
    assert disable_response.json()['code'] == 'secret_scope_denied'
    org_secret.refresh_from_db()
    assert org_secret.active is True
    assert ProviderSecretEnvelope.objects.filter(secret=org_secret).count() == 1


@override_settings(ENVIRONMENT='production', ENGRAM_SECRET_ENCRYPTION_KEY='')
def test_provider_secret_encryption_key_requires_dedicated_key_outside_dev() -> None:
    with pytest.raises(ImproperlyConfigured, match='ENGRAM_SECRET_ENCRYPTION_KEY'):
        encryption_key()


@pytest.mark.django_db
def test_fake_provider_gateway_records_redacted_provider_call_without_raw_secret_or_prompt_body() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
        provider='openai',
        scope='team',
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
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )

    result = FakeProviderGateway().call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            policy=policy,
            request_id='request-provider-call-1',
            trace_id='trace-provider-call-1',
            prompt=f'embedding prompt with {RAW_PROVIDER_SECRET}',
        ),
    )

    assert result.provider == 'openai'
    assert result.model == 'text-embedding-3-small'
    record = ProviderCallRecord.objects.get(id=result.call_record_id)
    assert record.policy_id == policy.id
    assert record.secret_id == secret.id
    assert record.redaction_state == 'redacted'
    assert record.token_usage == {'input_tokens': 4, 'output_tokens': 0}
    assert RAW_PROVIDER_SECRET not in str(record.__dict__)
