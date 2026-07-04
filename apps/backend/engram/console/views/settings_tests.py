from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.db.models.query import QuerySet
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
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    Team,
    VisibilityScope,
)
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import generated_embedding


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


def _make_provider_secret(organization: Organization) -> ProviderSecret:
    secret = ProviderSecret.objects.create(
        organization=organization,
        name='test-secret',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-key',
        hmac_digest='hmac-digest',
        active=True,
    )

    return secret


RAW_API_KEY = 'egk_settings_test_0123456789abcdefghijklmnopqrstuvwxyz'


def _make_api_key_for_context(
    organization: Organization,
    team: Team,
    project: Project,
    owner: Identity,
) -> ApiKey:
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Settings context key',
        key_prefix=api_key_prefix(RAW_API_KEY),
        key_hash=hash_api_key(RAW_API_KEY),
        key_fingerprint=api_key_fingerprint(RAW_API_KEY),
        team=team,
        project=project,
    )
    mem_read_cap, _ = Capability.objects.get_or_create(
        code='memories:read',
        defaults={'description': 'memories:read'},
    )
    ApiKeyCapability.objects.create(api_key=api_key, capability=mem_read_cap)

    return api_key


@pytest.fixture
def f_org_admin_client() -> APIClient:
    org = Organization.objects.create(name='SettingsOrg', slug='settings-org')

    return _client_for_org('settings-admin', org, ('organizations:admin',))


@pytest.fixture
def f_memories_admin_client() -> APIClient:
    org = Organization.objects.create(name='PurgeOrg', slug='purge-org')

    return _client_for_org('purge-admin', org, ('memories:admin',))


@pytest.fixture
def f_reader_client() -> APIClient:
    org = Organization.objects.create(name='SettingsReaderOrg', slug='settings-reader-org')

    return _client_for_org('settings-reader', org, ('memories:read',))


# ─── Retrieval settings ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_retrieval_settings_get_returns_defaults(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert response.status_code == 200
    body = response.json()
    assert body['hybrid_retrieval_enabled'] is True
    assert body['require_provenance'] is False


@pytest.mark.django_db
def test_retrieval_settings_put_round_trip(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'hybrid_retrieval_enabled': False, 'require_provenance': True},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['hybrid_retrieval_enabled'] is False
    assert body['require_provenance'] is True

    get_response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert get_response.status_code == 200
    get_body = get_response.json()
    assert get_body['hybrid_retrieval_enabled'] is False
    assert get_body['require_provenance'] is True


@pytest.mark.django_db
def test_retrieval_settings_requires_org_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/settings/retrieval')

    assert response.status_code == 403


@pytest.mark.django_db
def test_retrieval_settings_put_requires_org_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.put(
        '/v1/admin/settings/retrieval',
        {'hybrid_retrieval_enabled': False},
        format='json',
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_retrieval_settings_get_returns_extended_defaults(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert response.status_code == 200
    body = response.json()
    assert body['lexical_recall_enabled'] is False
    assert body['lexical_fusion_enabled'] is False
    assert body['curator_llm_judge_enabled'] is False
    assert body['near_dup_threshold'] == pytest.approx(0.850)
    assert body['distillation_auto_approve_threshold'] is None


@pytest.mark.django_db
def test_retrieval_settings_put_round_trip_extended(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {
            'lexical_recall_enabled': True,
            'lexical_fusion_enabled': True,
            'curator_llm_judge_enabled': True,
            'near_dup_threshold': 0.9,
            'distillation_auto_approve_threshold': 0.75,
        },
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['lexical_recall_enabled'] is True
    assert body['lexical_fusion_enabled'] is True
    assert body['curator_llm_judge_enabled'] is True
    assert body['near_dup_threshold'] == pytest.approx(0.9)
    assert body['distillation_auto_approve_threshold'] == pytest.approx(0.75)

    get_response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert get_response.status_code == 200
    get_body = get_response.json()
    assert get_body['lexical_recall_enabled'] is True
    assert get_body['lexical_fusion_enabled'] is True
    assert get_body['curator_llm_judge_enabled'] is True
    assert get_body['near_dup_threshold'] == pytest.approx(0.9)
    assert get_body['distillation_auto_approve_threshold'] == pytest.approx(0.75)


@pytest.mark.django_db
def test_retrieval_settings_get_includes_confidence_decay_enabled_default(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert response.status_code == 200
    assert response.json()['confidence_decay_enabled'] is True


@pytest.mark.django_db
def test_retrieval_settings_put_flips_confidence_decay_enabled(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'confidence_decay_enabled': False},
        format='json',
    )

    assert response.status_code == 200
    assert response.json()['confidence_decay_enabled'] is False

    get_response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert get_response.status_code == 200
    assert get_response.json()['confidence_decay_enabled'] is False


@pytest.mark.django_db
def test_retrieval_settings_put_confidence_decay_enabled_writes_audit_event(f_org_admin_client: APIClient) -> None:
    organization = Organization.objects.get(slug='settings-org')

    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'confidence_decay_enabled': False},
        format='json',
    )

    assert response.status_code == 200
    audit = AuditEvent.objects.get(organization=organization, event_type='RetrievalSettingsUpdated')
    settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
    assert audit.target_id == str(settings.id)
    assert settings.confidence_decay_enabled is False


@pytest.mark.django_db
def test_retrieval_settings_put_rejects_out_of_range_near_dup_threshold(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'near_dup_threshold': 1.5},
        format='json',
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_retrieval_settings_put_rejects_out_of_range_auto_approve_threshold(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': -0.1},
        format='json',
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_retrieval_settings_put_above_ceiling_includes_advisory(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': 0.8},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['distillation_auto_approve_threshold'] == pytest.approx(0.8)
    assert body['advisory'] == (
        'per-observation candidates will always be held; session distillation must be healthy for memory to be promoted'
    )

    organization = Organization.objects.get(slug='settings-org')
    settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
    assert float(settings.distillation_auto_approve_threshold) == pytest.approx(0.8)


@pytest.mark.django_db
def test_retrieval_settings_put_at_or_below_ceiling_has_no_advisory(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': 0.5},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['distillation_auto_approve_threshold'] == pytest.approx(0.5)
    assert 'advisory' not in body

    organization = Organization.objects.get(slug='settings-org')
    settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
    assert float(settings.distillation_auto_approve_threshold) == pytest.approx(0.5)


@pytest.mark.django_db
def test_retrieval_settings_put_null_threshold_has_no_advisory(f_org_admin_client: APIClient) -> None:
    f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': 0.8},
        format='json',
    )

    response = f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': None},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['distillation_auto_approve_threshold'] is None
    assert 'advisory' not in body

    organization = Organization.objects.get(slug='settings-org')
    settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
    assert settings.distillation_auto_approve_threshold is None


@pytest.mark.django_db
def test_retrieval_settings_get_never_includes_advisory(f_org_admin_client: APIClient) -> None:
    f_org_admin_client.put(
        '/v1/admin/settings/retrieval',
        {'distillation_auto_approve_threshold': 0.8},
        format='json',
    )

    response = f_org_admin_client.get('/v1/admin/settings/retrieval')

    assert response.status_code == 200
    assert 'advisory' not in response.json()


# ─── Hybrid retrieval gating in BuildContextBundle ───────────────────────────


@pytest.mark.django_db
def test_hybrid_disabled_skips_semantic_retrieval() -> None:
    from engram.access.models import ProjectGrant

    organization = Organization.objects.create(name='HybridOrg', slug='hybrid-org')
    team = Team.objects.create(organization=organization, name='Team', slug='team')
    project = Project.objects.create(organization=organization, name='Proj', slug='proj')
    ProjectTeam.objects.create(organization=organization, team=team, project=project)

    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-hybrid',
        display_name='hybrid svc',
    )
    developer_role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=developer_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=developer_role)
    _make_api_key_for_context(organization, team, project, owner)

    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Embedding secret',
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
        ciphertext='enc-secret',
        hmac_digest='hmac',
        active=True,
    )
    ModelPolicy.objects.create(
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

    query_text = 'zoqxwphybrid'
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Semantic-only memory',
        body='Semantic-only body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='sem-only-hash-1',
    )
    RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        source_observation_ids=[],
        file_paths=[],
        symbols=[],
        exact_terms=[],
        full_text='unrelated document content not matching the query',
        embedding_vector=generated_embedding(query_text),
    )

    client = APIClient()

    payload = {
        'project_id': str(project.id),
        'team_id': str(team.id),
        'agent_runtime': 'codex',
        'agent_version': '0.1.0',
        'agent_external_id': 'codex-hybrid-test',
        'session_id': 'session-hybrid-1',
        'request_id': 'request-hybrid-1',
        'correlation_id': 'corr-hybrid-1',
        'trace_id': 'trace-hybrid-1',
        'repository_url': '',
        'repository_root': '',
        'branch': 'main',
        'cwd': '/',
        'query': query_text,
        'file_paths': [],
        'symbols': [],
        'limit': 5,
        'token_budget': 2000,
    }

    response_enabled = client.post(
        '/v1/context/session-start',
        payload,
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {RAW_API_KEY}',
    )
    assert response_enabled.status_code == 200
    assert len(response_enabled.json()['items']) == 1

    org_settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
    org_settings.hybrid_retrieval_enabled = False
    org_settings.save(update_fields=['hybrid_retrieval_enabled', 'updated_at'])

    payload_2 = dict(payload, request_id='request-hybrid-2', session_id='session-hybrid-2')
    response_disabled = client.post(
        '/v1/context/session-start',
        payload_2,
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {RAW_API_KEY}',
    )
    assert response_disabled.status_code == 200
    assert len(response_disabled.json()['items']) == 0


@pytest.mark.django_db
def test_hybrid_disabled_exact_match_still_works() -> None:
    from engram.access.models import ProjectGrant

    organization = Organization.objects.create(name='HybridExactOrg', slug='hybrid-exact-org')
    team = Team.objects.create(organization=organization, name='Team', slug='team')
    project = Project.objects.create(organization=organization, name='Proj', slug='proj')
    ProjectTeam.objects.create(organization=organization, team=team, project=project)

    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-hybrid-exact',
        display_name='hybrid exact svc',
    )
    developer_role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=developer_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=developer_role)
    _make_api_key_for_context(organization, team, project, owner)

    OrganizationSettings.objects.create(
        organization=organization,
        hybrid_retrieval_enabled=False,
    )

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Exact-match memory',
        body='Body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='exact-match-hash-1',
    )
    RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        source_observation_ids=[],
        file_paths=['src/exact-match.py'],
        symbols=[],
        exact_terms=['exact-match memory'],
        full_text='Exact-match memory\n\nBody',
    )

    client = APIClient()

    payload = {
        'project_id': str(project.id),
        'team_id': str(team.id),
        'agent_runtime': 'codex',
        'agent_version': '0.1.0',
        'agent_external_id': 'codex-exact-test',
        'session_id': 'session-exact-1',
        'request_id': 'request-exact-1',
        'correlation_id': 'corr-exact-1',
        'trace_id': 'trace-exact-1',
        'repository_url': '',
        'repository_root': '',
        'branch': 'main',
        'cwd': '/',
        'query': 'exact-match memory',
        'file_paths': [],
        'symbols': [],
        'limit': 5,
        'token_budget': 2000,
    }

    response = client.post(
        '/v1/context/session-start',
        payload,
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {RAW_API_KEY}',
    )
    assert response.status_code == 200
    assert len(response.json()['items']) == 1


# ─── Embedding settings ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_embedding_settings_get_returns_nulls_when_no_policy(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.get('/v1/admin/settings/embedding')

    assert response.status_code == 200
    body = response.json()
    assert body['provider'] is None
    assert body['model'] is None


@pytest.mark.django_db
def test_embedding_settings_put_then_get(f_org_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='settings-org')
    secret = _make_provider_secret(org)

    put_response = f_org_admin_client.put(
        '/v1/admin/settings/embedding',
        {'provider': 'openai', 'model': 'text-embedding-3-small', 'secret_id': str(secret.id)},
        format='json',
    )

    assert put_response.status_code == 200
    put_body = put_response.json()
    assert put_body['provider'] == 'openai'
    assert put_body['model'] == 'text-embedding-3-small'

    get_response = f_org_admin_client.get('/v1/admin/settings/embedding')

    assert get_response.status_code == 200
    get_body = get_response.json()
    assert get_body['provider'] == 'openai'
    assert get_body['model'] == 'text-embedding-3-small'


@pytest.mark.django_db
def test_embedding_settings_put_missing_fields_returns_400(f_org_admin_client: APIClient) -> None:
    response = f_org_admin_client.put(
        '/v1/admin/settings/embedding',
        {'provider': 'openai'},
        format='json',
    )

    assert response.status_code == 400
    body = response.json()
    assert body['code'] == 'embedding_fields_required'
    assert body['error_code'] == 'embedding_fields_required'


@pytest.mark.django_db
def test_embedding_settings_put_unknown_secret_returns_400(f_org_admin_client: APIClient) -> None:
    import uuid

    response = f_org_admin_client.put(
        '/v1/admin/settings/embedding',
        {'provider': 'openai', 'model': 'ada', 'secret_id': str(uuid.uuid4())},
        format='json',
    )

    assert response.status_code == 400
    body = response.json()
    assert body['code'] == 'embedding_secret_not_found'
    assert body['error_code'] == 'embedding_secret_not_found'


@pytest.mark.django_db
def test_embedding_settings_requires_org_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/settings/embedding')

    assert response.status_code == 403


@pytest.mark.django_db
def test_embedding_settings_put_logs_updated_event(f_org_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='settings-org')
    secret = _make_provider_secret(org)

    with structlog.testing.capture_logs() as captured_logs:
        response = f_org_admin_client.put(
            '/v1/admin/settings/embedding',
            {'provider': 'openai', 'model': 'text-embedding-3-small', 'secret_id': str(secret.id)},
            format='json',
        )

    assert response.status_code == 200
    policy = ModelPolicy.objects.get(organization=org, task_type='embedding', active=True)
    events = [entry for entry in captured_logs if entry['event'] == 'embedding_settings_updated']
    assert len(events) == 1
    assert events[0]['organization_id'] == str(org.id)
    assert events[0]['provider'] == 'openai'
    assert events[0]['model'] == 'text-embedding-3-small'
    assert policy.provider == 'openai'


# ─── Purge ────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_purge_wrong_confirmation_returns_400(f_memories_admin_client: APIClient) -> None:
    response = f_memories_admin_client.post(
        '/v1/admin/settings/purge',
        {'confirmation': 'wrong-slug'},
        format='json',
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_purge_requires_memories_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.post(
        '/v1/admin/settings/purge',
        {'confirmation': 'settings-reader-org'},
        format='json',
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_purge_writes_audit_event_before_deletion(f_memories_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='purge-org')
    project = Project.objects.create(organization=org, name='Proj', slug='purge-proj')
    Memory.objects.create(
        organization=org,
        project=project,
        title='To purge',
        body='body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    response = f_memories_admin_client.post(
        '/v1/admin/settings/purge',
        {'confirmation': 'purge-org'},
        format='json',
    )

    assert response.status_code == 200
    audit = AuditEvent.objects.get(event_type='OrganizationMemoryPurged')
    assert audit.organization_id == org.id
    assert audit.metadata['memory_count'] == 1
    assert audit.metadata['memory_candidate_count'] == 0
    assert audit.metadata['retrieval_document_count'] == 0
    assert audit.actor_type == 'user'
    assert audit.capability == ''
    assert audit.result == AuditResult.RECORDED
    assert Memory.objects.filter(organization=org).count() == 0


@pytest.mark.django_db
def test_purge_logs_purged_event(f_memories_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='purge-org')
    project = Project.objects.create(organization=org, name='Proj', slug='purge-proj-log')
    Memory.objects.create(
        organization=org,
        project=project,
        title='To purge',
        body='body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    with structlog.testing.capture_logs() as captured_logs:
        response = f_memories_admin_client.post(
            '/v1/admin/settings/purge',
            {'confirmation': 'purge-org'},
            format='json',
        )

    assert response.status_code == 200
    events = [entry for entry in captured_logs if entry['event'] == 'organization_memory_purged']
    assert len(events) == 1
    assert events[0]['organization_id'] == str(org.id)
    assert events[0]['memory_count'] == 1


@pytest.mark.django_db
def test_purge_forced_failure_leaves_no_audit_row(f_memories_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='purge-org')
    project = Project.objects.create(organization=org, name='Proj', slug='purge-proj-fail')
    Memory.objects.create(
        organization=org,
        project=project,
        title='To purge',
        body='body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )

    original_delete = QuerySet.delete

    def _raising_delete(self: QuerySet) -> tuple[int, dict[str, int]]:
        if self.model is Memory:
            raise DatabaseError('forced failure')

        return original_delete(self)

    with patch.object(QuerySet, 'delete', _raising_delete):
        response = f_memories_admin_client.post(
            '/v1/admin/settings/purge',
            {'confirmation': 'purge-org'},
            format='json',
        )

    assert response.status_code == 500
    assert not AuditEvent.objects.filter(event_type='OrganizationMemoryPurged').exists()
    assert Memory.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_purge_response_includes_context_bundle_counts(f_memories_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='purge-org')
    project = Project.objects.create(organization=org, name='Proj', slug='purge-proj-bundle')
    agent = Agent.objects.create(
        organization=org,
        runtime=Runtime.UNKNOWN,
        external_id='agent-1',
        display_name='agent-1',
    )
    session = AgentSession.objects.create(
        organization=org,
        project=project,
        agent=agent,
        external_session_id='sess-1',
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
    )
    ContextBundle.objects.create(
        organization=org,
        project=project,
        agent=agent,
        session=session,
        request_id='req-1',
        purpose='context',
    )

    response = f_memories_admin_client.post(
        '/v1/admin/settings/purge',
        {'confirmation': 'purge-org'},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['deleted']['context_bundles'] == 1
    assert ContextBundle.objects.filter(organization=org).count() == 0


@pytest.mark.django_db
def test_purge_cross_tenant_isolation() -> None:
    org_a = Organization.objects.create(name='OrgA', slug='purge-org-a')
    org_b = Organization.objects.create(name='OrgB', slug='purge-org-b')

    proj_a = Project.objects.create(organization=org_a, name='ProjA', slug='proj-a')
    proj_b = Project.objects.create(organization=org_b, name='ProjB', slug='proj-b')

    def _make_memory(org: Organization, proj: Project, title: str) -> tuple[Memory, RetrievalDocument]:
        mem = Memory.objects.create(
            organization=org,
            project=proj,
            title=title,
            body='body',
            status=MemoryStatus.APPROVED,
            visibility_scope=VisibilityScope.PROJECT,
        )
        ver = MemoryVersion.objects.create(
            organization=org,
            project=proj,
            memory=mem,
            version=1,
            body=mem.body,
            content_hash=f'{title}-hash',
        )
        doc = RetrievalDocument.objects.create(
            organization=org,
            project=proj,
            memory=mem,
            memory_version=ver,
            visibility_scope=VisibilityScope.PROJECT,
            source_observation_ids=[],
            file_paths=[],
            symbols=[],
            exact_terms=[],
            full_text=title,
        )

        return mem, doc

    mem_a1, doc_a1 = _make_memory(org_a, proj_a, 'OrgA Memory 1')
    mem_a2, doc_a2 = _make_memory(org_a, proj_a, 'OrgA Memory 2')
    mem_b1, doc_b1 = _make_memory(org_b, proj_b, 'OrgB Memory 1')
    MemoryCandidate.objects.create(
        organization=org_a,
        project=proj_a,
        title='Candidate A',
        body='body',
        content_hash='cand-a-hash',
    )

    admin_client_a = _client_for_org('purge-a-admin', org_a, ('memories:admin',))

    response = admin_client_a.post(
        '/v1/admin/settings/purge',
        {'confirmation': 'purge-org-a'},
        format='json',
    )

    assert response.status_code == 200
    body = response.json()
    assert body['deleted']['memories'] == 2
    assert body['deleted']['memory_candidates'] == 1
    assert body['deleted']['retrieval_documents'] == 2

    assert Memory.objects.filter(organization=org_a).count() == 0
    assert MemoryCandidate.objects.filter(organization=org_a).count() == 0
    assert RetrievalDocument.objects.filter(organization=org_a).count() == 0

    assert Memory.objects.filter(organization=org_b).count() == 1
    assert Memory.objects.get(organization=org_b).title == 'OrgB Memory 1'
    assert RetrievalDocument.objects.filter(organization=org_b).count() == 1
    assert RetrievalDocument.objects.get(organization=org_b).id == doc_b1.id
