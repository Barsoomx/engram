from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from rest_framework.test import APIClient

from engram.access.models import ApiKeyCapability, Capability, Identity, OrganizationMembership, ProjectGrant, Role
from engram.context.context_api_tests import auth_headers, create_project_scope, create_scoped_api_key
from engram.core.models import AuditEvent, Team
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    EmbeddingCallInput,
    FakeProviderGateway,
    ModelPolicyError,
    ProviderCallInput,
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    _completion_body,
    _completion_title,
    encryption_key,
    generated_embedding,
)

POLICY_RAW_KEY = 'egk_test_model_policy_admin_0123456789abcdefghijklmnopqrstuvwxyz'
RAW_PROVIDER_SECRET = 'sk-test_model_policy_secret_1234567890abcdef'


def expected_generated_title(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    return f'Provider-generated memory {digest}'


def expected_generated_body(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    return f'Provider-generated candidate body {digest}'


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


@pytest.mark.django_db
def test_fake_provider_gateway_reuses_provider_call_for_stable_request_id() -> None:
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
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team.id,
        policy=policy,
        request_id='memory-worker:observation-1:generation',
        trace_id='trace-provider-call-duplicate-1',
        prompt='generate memory',
    )

    first = FakeProviderGateway().call(data)
    second = FakeProviderGateway().call(data)

    assert second.call_record_id == first.call_record_id
    assert ProviderCallRecord.objects.count() == 1


@pytest.mark.django_db
def test_fake_provider_gateway_returns_deterministic_generated_candidate_content() -> None:
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
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )
    prompt = 'Title: pytest failure fixed\nBody: pytest failed on missing memory worker and now exits 0'

    result = FakeProviderGateway().call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            policy=policy,
            request_id='memory-worker:observation-2:generation',
            trace_id='trace-provider-call-generation-1',
            prompt=prompt,
        ),
    )

    assert result.generated_title == expected_generated_title(prompt)
    assert result.generated_body == expected_generated_body(prompt)
    assert 'pytest failure fixed' not in result.generated_title
    assert 'pytest failed on missing memory worker' not in result.generated_body


@pytest.mark.django_db
def test_fake_provider_gateway_returns_deterministic_candidate_array_for_candidates_kind() -> None:
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
        name='Curation policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team.id,
        policy=policy,
        request_id='distill-session:session-1:curation',
        trace_id='trace-distill-candidates-1',
        prompt='Session observations:\n- Title: pytest fixed\n  Body: now exits 0',
        response_kind='candidates',
    )

    result = FakeProviderGateway().call(data)

    candidates = json.loads(result.generated_body)

    assert isinstance(candidates, list)
    assert len(candidates) == 2
    assert sorted(Decimal(str(item['confidence'])) for item in candidates) == [Decimal('0.4'), Decimal('0.9')]
    for item in candidates:
        assert item['title']
        assert item['body']
        assert 'supporting_observation_ids' in item

    replay = FakeProviderGateway().call(data)

    assert replay.generated_body == result.generated_body
    assert ProviderCallRecord.objects.filter(task_type='curation').count() == 1


def test_completion_body_passes_through_full_output_for_candidates_kind() -> None:
    pretty_json = json.dumps(
        [
            {'title': 'high', 'body': 'b1', 'confidence': 0.9, 'supporting_observation_ids': []},
            {'title': 'low', 'body': 'b2', 'confidence': 0.4, 'supporting_observation_ids': []},
        ],
        indent=2,
    )

    body = _completion_body(pretty_json, 'candidates')

    assert body == pretty_json
    assert _completion_title(pretty_json, 'candidates') == ''
    parsed = json.loads(body)
    assert len(parsed) == 2
    assert sorted(Decimal(str(item['confidence'])) for item in parsed) == [Decimal('0.4'), Decimal('0.9')]


def test_completion_body_and_title_split_first_line_for_single_kind() -> None:
    content = 'Title line\nBody line one\nBody line two'

    assert _completion_title(content, 'single') == 'Title line'
    assert _completion_body(content, 'single') == 'Body line one\nBody line two'


def test_generated_embedding_is_deterministic_and_normalized() -> None:
    first = generated_embedding('authorization before ranking protects context bundles')
    second = generated_embedding('authorization before ranking protects context bundles')

    assert len(first) == EMBEDDING_DIMENSION
    assert first == second
    norm = sum(component * component for component in first) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-3)


def test_generated_embedding_returns_zero_vector_for_short_text() -> None:
    assert generated_embedding('') == [0.0] * EMBEDDING_DIMENSION
    assert generated_embedding('   ') == [0.0] * EMBEDDING_DIMENSION
    assert generated_embedding('ab') == [0.0] * EMBEDDING_DIMENSION


@pytest.mark.django_db
def test_fake_provider_gateway_embed_reuses_call_and_redacts_input() -> None:
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
    data = EmbeddingCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team.id,
        policy=policy,
        request_id='memory-indexer:embedding-1:embedding',
        trace_id='trace-embedding-1',
        text=f'embedding prompt with {RAW_PROVIDER_SECRET}',
    )

    first = FakeProviderGateway().embed(data)
    second = FakeProviderGateway().embed(data)

    assert first.provider == 'openai'
    assert first.model == 'text-embedding-3-small'
    assert len(first.embedding) == EMBEDDING_DIMENSION
    assert second.call_record_id == first.call_record_id
    record = ProviderCallRecord.objects.get(id=first.call_record_id)
    assert record.task_type == 'embedding'
    assert record.redaction_state == 'redacted'
    assert record.token_usage['input_tokens'] > 0
    assert RAW_PROVIDER_SECRET not in str(record.__dict__)
    assert RAW_PROVIDER_SECRET not in str(first.embedding)


@pytest.mark.django_db
def test_fake_provider_gateway_embed_refuses_disabled_secret() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
        active=False,
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

    with pytest.raises(ProviderSecretError, match='provider secret is disabled'):
        FakeProviderGateway().embed(
            EmbeddingCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                policy=policy,
                request_id='memory-indexer:embedding-disabled:embedding',
                trace_id='trace-embedding-disabled',
                text='text',
            ),
        )


def _create_secret(client: APIClient, project: object, team: Team, name: str, provider: str) -> str:
    response = client.post(
        '/v1/model-policy/secrets',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': name,
            'provider': provider,
            'scope': 'team',
            'raw_secret': RAW_PROVIDER_SECRET,
            'request_id': f'request-secret-{name}',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 201

    return response.json()['id']


@pytest.mark.django_db
def test_provider_secret_list_returns_scoped_secrets_without_raw_secret() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()

    _create_secret(client, project, team, 'Team OpenAI', 'openai')
    _create_secret(client, project, team, 'Team Anthropic', 'anthropic')

    denied = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(),
    )

    assert denied.status_code == 403

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200

    body = response.json()
    assert body['count'] == 2
    items = body['items']
    assert len(items) == 2
    assert {item['name'] for item in items} == {'Team OpenAI', 'Team Anthropic'}
    assert RAW_PROVIDER_SECRET not in str(body)
    assert all('raw_secret' not in item for item in items)


@pytest.mark.django_db
def test_model_policy_list_returns_scoped_policies_and_filters_by_task_type() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()

    secret_id = _create_secret(client, project, team, 'Team OpenAI', 'openai')

    created = client.post(
        '/v1/model-policy/policies',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Generation policy',
            'scope': 'team',
            'task_type': 'generation',
            'provider': 'openai',
            'model': 'gpt-4o-mini',
            'secret_id': secret_id,
            'request_id': 'request-policy-create-list',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert created.status_code == 201

    denied = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(),
    )

    assert denied.status_code == 403

    response = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200

    body = response.json()
    assert body['count'] == 1
    items = body['items']
    assert len(items) == 1
    assert items[0]['task_type'] == 'generation'
    assert items[0]['model'] == 'gpt-4o-mini'

    filtered = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id), 'team_id': str(team.id), 'task_type': 'digest'},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert filtered.status_code == 200
    assert filtered.json() == {'count': 0, 'items': []}


def _create_policy(
    client: APIClient,
    project: object,
    team: Team,
    name: str,
    secret_id: str,
    task_type: str = 'generation',
) -> str:
    response = client.post(
        '/v1/model-policy/policies',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': name,
            'scope': 'team',
            'task_type': task_type,
            'provider': 'openai',
            'model': 'gpt-4o-mini',
            'secret_id': secret_id,
            'request_id': f'request-policy-{name}',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 201

    return response.json()['id']


@pytest.mark.django_db
def test_model_policy_detail_get_returns_policy_and_hides_cross_team() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI A', 'openai')
    policy_id = _create_policy(client, project, team, 'Team Generation', secret_id)

    response = client.get(
        f'/v1/model-policy/policies/{policy_id}',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200
    assert response.json()['policy_id'] == policy_id

    other_team = Team.objects.create(organization=organization, name='Other', slug='other-detail')
    other_secret = ProviderSecret.objects.create(
        organization=organization,
        team=other_team,
        name='Other OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    other_policy = ModelPolicy.objects.create(
        organization=organization,
        team=other_team,
        name='Other policy',
        scope='team',
        task_type='generation',
        provider='openai',
        model='gpt-4o-mini',
        secret=other_secret,
        version=1,
    )

    cross_team_response = client.get(
        f'/v1/model-policy/policies/{other_policy.id}',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert cross_team_response.status_code == 404


@pytest.mark.django_db
def test_model_policy_update_changes_fields_increments_version_and_writes_audit() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI B', 'openai')
    policy_id = _create_policy(client, project, team, 'Original Name', secret_id)

    response = client.patch(
        f'/v1/model-policy/policies/{policy_id}',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Updated Name',
            'model': 'gpt-4.1-mini',
            'request_id': 'request-policy-update-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['name'] == 'Updated Name'
    assert body['model'] == 'gpt-4.1-mini'
    assert body['version'] == 2

    policy = ModelPolicy.objects.get(id=policy_id)
    assert policy.name == 'Updated Name'
    assert policy.model == 'gpt-4.1-mini'
    assert policy.version == 2

    assert AuditEvent.objects.filter(
        target_id=str(policy_id),
        event_type='ModelPolicyUpdated',
    ).exists()


@pytest.mark.django_db
def test_model_policy_update_rejects_cross_scope_secret_rescope() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI C', 'openai')
    policy_id = _create_policy(client, project, team, 'Team Policy C', secret_id)

    other_team = Team.objects.create(organization=organization, name='Other C', slug='other-c')
    other_secret = ProviderSecret.objects.create(
        organization=organization,
        team=other_team,
        name='Other OpenAI C',
        provider='openai',
        scope='team',
        current_version=1,
    )

    response = client.patch(
        f'/v1/model-policy/policies/{policy_id}',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'secret_id': str(other_secret.id),
            'request_id': 'request-policy-update-cross-scope',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code in (400, 403)
    assert response.json()['code'] == 'policy_scope_mismatch'

    policy = ModelPolicy.objects.get(id=policy_id)
    assert str(policy.secret_id) == secret_id


@pytest.mark.django_db
def test_model_policy_disable_sets_inactive_idempotent_and_stops_resolution() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI D', 'openai')
    policy_id = _create_policy(client, project, team, 'Team Policy D', secret_id, 'generation')

    response = client.post(
        f'/v1/model-policy/policies/{policy_id}/disable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-policy-disable-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200
    assert response.json()['active'] is False

    policy = ModelPolicy.objects.get(id=policy_id)
    assert policy.active is False

    with pytest.raises(ModelPolicyError, match='Model policy was not found'):
        ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                task_type='generation',
            ),
        )

    second_response = client.post(
        f'/v1/model-policy/policies/{policy_id}/disable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-policy-disable-2',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert second_response.status_code == 200
    assert second_response.json()['active'] is False


@pytest.mark.django_db
def test_provider_secret_enable_restores_active_and_allows_resolution() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI E', 'openai')
    policy_id = _create_policy(client, project, team, 'Team Policy E', secret_id, 'generation')

    client.post(
        f'/v1/model-policy/secrets/{secret_id}/disable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-secret-disable-e',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    with pytest.raises(ModelPolicyError, match='Model policy was not found'):
        ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id,
                task_type='generation',
            ),
        )

    enable_response = client.post(
        f'/v1/model-policy/secrets/{secret_id}/enable',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'request_id': 'request-secret-enable-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert enable_response.status_code == 200
    assert enable_response.json()['active'] is True

    secret = ProviderSecret.objects.get(id=secret_id)
    assert secret.active is True

    _ = policy_id
    resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            task_type='generation',
        ),
    )
    assert resolved.policy.active is True


@pytest.mark.django_db
def test_provider_secret_rename_changes_name_without_touching_fingerprint_or_version() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Original Secret Name', 'openai')

    secret_before = ProviderSecret.objects.get(id=secret_id)
    original_fingerprint = secret_before.secret_fingerprint
    original_version = secret_before.current_version

    response = client.patch(
        f'/v1/model-policy/secrets/{secret_id}',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'name': 'Renamed Secret',
            'request_id': 'request-secret-rename-1',
        },
        format='json',
        **auth_headers(POLICY_RAW_KEY),
    )

    assert response.status_code == 200
    assert response.json()['name'] == 'Renamed Secret'

    secret_after = ProviderSecret.objects.get(id=secret_id)
    assert secret_after.name == 'Renamed Secret'
    assert secret_after.secret_fingerprint == original_fingerprint
    assert secret_after.current_version == original_version

    assert AuditEvent.objects.filter(
        target_id=str(secret_id),
        event_type='ProviderSecretRenamed',
    ).exists()


@pytest.mark.django_db
def test_model_policy_list_pagination_slices_correctly() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI F', 'openai')

    _create_policy(client, project, team, 'Policy F1', secret_id, 'generation')
    _create_policy(client, project, team, 'Policy F2', secret_id, 'embedding')
    _create_policy(client, project, team, 'Policy F3', secret_id, 'curation')

    page1 = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id), 'team_id': str(team.id), 'limit': '2', 'offset': '0'},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert page1.status_code == 200
    body1 = page1.json()
    assert body1['count'] == 3
    assert len(body1['items']) == 2

    page2 = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id), 'team_id': str(team.id), 'limit': '2', 'offset': '2'},
        **auth_headers(POLICY_RAW_KEY),
    )

    assert page2.status_code == 200
    body2 = page2.json()
    assert body2['count'] == 3
    assert len(body2['items']) == 1


@pytest.mark.django_db
def test_new_mutating_endpoints_require_model_policy_capability() -> None:
    scope = create_project_scope()
    _organization, team, project, _owner, _api_key = scope
    create_policy_admin_key(scope)
    client = APIClient()
    secret_id = _create_secret(client, project, team, 'Team OpenAI G', 'openai')
    policy_id = _create_policy(client, project, team, 'Team Policy G', secret_id)

    base_params = {'project_id': str(project.id), 'team_id': str(team.id), 'request_id': 'req-cap-check'}

    get_detail = client.get(
        f'/v1/model-policy/policies/{policy_id}',
        {'project_id': str(project.id), 'team_id': str(team.id)},
        **auth_headers(),
    )
    patch_policy = client.patch(
        f'/v1/model-policy/policies/{policy_id}',
        {**base_params, 'name': 'New Name'},
        format='json',
        **auth_headers(),
    )
    disable_policy = client.post(
        f'/v1/model-policy/policies/{policy_id}/disable',
        base_params,
        format='json',
        **auth_headers(),
    )
    enable_secret = client.post(
        f'/v1/model-policy/secrets/{secret_id}/enable',
        base_params,
        format='json',
        **auth_headers(),
    )
    patch_secret = client.patch(
        f'/v1/model-policy/secrets/{secret_id}',
        {**base_params, 'name': 'New Name'},
        format='json',
        **auth_headers(),
    )

    for resp in (get_detail, patch_policy, disable_policy, enable_secret, patch_secret):
        assert resp.status_code == 403
        assert resp.json()['code'] == 'missing_capability'
