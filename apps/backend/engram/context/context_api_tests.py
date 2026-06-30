from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.services import ContextIndexError, IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Observation,
    Organization,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope

RAW_KEY = 'egk_test_context_0123456789abcdefghijklmnopqrstuvwxyz'
OTHER_RAW_KEY = 'egk_test_context_other_0123456789abcdefghijklmnopqrstuvwxyz'
CONTEXT_QUERY_MAX_LENGTH = 8000
CONTEXT_LIST_VALUE_MAX_LENGTH = 1024
CONTEXT_LIST_MAX_ITEMS = 100
CONTEXT_PATH_MAX_LENGTH = 1024
CONTEXT_AGENT_VERSION_MAX_LENGTH = 80
CONTEXT_METADATA_MAX_LENGTH = 255


def create_project_scope() -> tuple[Organization, Team, Project, Identity, ApiKey]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )
    ProjectTeam.objects.create(organization=organization, team=team, project=project)
    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-context',
        display_name='Context service account',
    )
    role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)
    api_key = create_scoped_api_key(organization, team, project, owner)

    return organization, team, project, owner, api_key


def create_scoped_api_key(
    organization: Organization,
    team: Team | None,
    project: Project | None,
    owner: Identity,
    *,
    raw_key: str = RAW_KEY,
    capabilities: tuple[str, ...] = ('memories:read',),
) -> ApiKey:
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Context key',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        team=team,
        project=project,
    )
    for capability_code in capabilities:
        ApiKeyCapability.objects.create(
            api_key=api_key,
            capability=Capability.objects.get(code=capability_code),
        )

    return api_key


def create_embedding_policy(
    organization: Organization,
    team: Team,
    project: Project,
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team Embedding OpenAI',
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
        ciphertext='encrypted-embedding-secret',
        hmac_digest='embedding-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
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


def auth_headers(raw_key: str = RAW_KEY) -> dict[str, str]:
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


def valid_context_payload(project: Project, team: Team, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'project_id': str(project.id),
        'team_id': str(team.id),
        'agent_runtime': 'codex',
        'agent_version': '0.1.0',
        'agent_external_id': 'codex-local',
        'session_id': 'session-context-1',
        'request_id': 'request-context-1',
        'correlation_id': 'correlation-context-1',
        'trace_id': 'trace-context-1',
        'repository_url': 'https://example.test/engram.git',
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
        'query': 'authorization before ranking protects context bundles',
        'file_paths': ['apps/backend/engram/context/services.py'],
        'symbols': ['BuildContextBundle'],
        'limit': 5,
        'token_budget': 2000,
    }
    payload.update(overrides)

    return payload


def create_approved_memory_document(
    organization: Organization,
    team: Team | None,
    project: Project,
    *,
    title: str = 'Authorization before ranking',
    body: str = 'Authorization before ranking protects context bundles.',
    visibility_scope: str = VisibilityScope.PROJECT,
    file_paths: list[str] | None = None,
    symbols: list[str] | None = None,
    exact_terms: list[str] | None = None,
) -> tuple[Memory, MemoryVersion, RetrievalDocument]:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        metadata={
            'file_paths': file_paths or ['apps/backend/engram/context/services.py'],
            'symbols': symbols or ['BuildContextBundle'],
            'exact_terms': exact_terms or ['context bundle', 'authorization before ranking'],
        },
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash=f'{title}-version-hash',
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=visibility_scope,
        source_observation_ids=[],
        file_paths=file_paths or ['apps/backend/engram/context/services.py'],
        symbols=symbols or ['BuildContextBundle'],
        exact_terms=exact_terms or ['context bundle', 'authorization before ranking'],
        full_text=f'{memory.title}\n\n{memory.body}',
    )

    return memory, version, document


@pytest.mark.django_db
def test_session_start_returns_cited_exact_context_and_persists_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    memory, version, document = create_approved_memory_document(_organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(project, team),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'created'
    assert body['request_id'] == 'request-context-1'
    assert body['purpose'] == 'session_start'
    assert body['context_bundle_id']
    assert body['items'] == [
        {
            'citation': 'M1',
            'memory_id': str(memory.id),
            'memory_version_id': str(version.id),
            'retrieval_document_id': str(document.id),
            'title': 'Authorization before ranking',
            'body': 'Authorization before ranking protects context bundles.',
            'inclusion_reason': 'exact match: apps/backend/engram/context/services.py',
            'scope_evidence': {
                'visibility_scope': 'project',
                'project_id': str(project.id),
                'team_id': str(team.id),
            },
            'matched_terms': ['apps/backend/engram/context/services.py'],
        },
    ]
    assert 'M1' in body['rendered_context']
    assert memory.title in body['rendered_context']
    assert memory.body in body['rendered_context']
    assert body['hook_specific_output'] == {
        'hookEventName': 'SessionStart',
        'additionalContext': body['rendered_context'],
    }
    assert body['warnings'] == []

    bundle = ContextBundle.objects.get()
    item = ContextBundleItem.objects.get()
    audit = AuditEvent.objects.get(event_type='MemoryRetrieved')

    assert bundle.request_id == 'request-context-1'
    assert bundle.purpose == 'session_start'
    assert bundle.rendered_text == body['rendered_context']
    assert bundle.selected_count == 1
    assert bundle.authorization_scope['capability'] == 'memories:read'
    assert item.bundle_id == bundle.id
    assert item.memory_id == memory.id
    assert item.retrieval_document_id == document.id
    assert item.citation == 'M1'
    assert audit.target_type == 'context_bundle'
    assert audit.target_id == str(bundle.id)
    assert audit.capability == 'memories:read'
    assert audit.metadata['selected_count'] == 1
    assert RAW_KEY not in str(body)
    assert RAW_KEY not in str(bundle.metadata)
    assert RAW_KEY not in str(item.metadata)
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_session_start_requires_bearer_api_key() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(project, team, request_id='request-missing-key'),
        format='json',
    )

    assert response.status_code == 401
    assert response.json()['code'] == 'missing_api_key'
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_requires_memories_read_capability() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('observations:write',),
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(project, team, request_id='request-missing-capability'),
        format='json',
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_rejects_oversized_query_before_creating_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-oversized-query',
            query='x' * (CONTEXT_QUERY_MAX_LENGTH + 1),
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['query']['code'] == ['context_query_too_large']
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_rejects_too_many_or_too_long_file_paths_before_creating_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-oversized-file-paths',
            file_paths=[f'apps/file-{index}.py' for index in range(CONTEXT_LIST_MAX_ITEMS + 1)],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )
    long_path_response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-too-long-file-path',
            file_paths=['a' * (CONTEXT_LIST_VALUE_MAX_LENGTH + 1)],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['file_paths']['code'] == ['context_file_paths_too_many']
    assert long_path_response.status_code == 400
    assert long_path_response.json()['file_paths']['code'] == ['context_file_paths_value_too_long']
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_rejects_too_many_or_too_long_symbols_before_creating_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-oversized-symbols',
            file_paths=[],
            symbols=[f'Symbol{index}' for index in range(CONTEXT_LIST_MAX_ITEMS + 1)],
        ),
        format='json',
        **auth_headers(),
    )
    long_symbol_response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-too-long-symbol',
            file_paths=[],
            symbols=['S' * (CONTEXT_LIST_VALUE_MAX_LENGTH + 1)],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['symbols']['code'] == ['context_symbols_too_many']
    assert long_symbol_response.status_code == 400
    assert long_symbol_response.json()['symbols']['code'] == ['context_symbols_value_too_long']
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_rejects_too_long_repository_path_fields_before_creating_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-too-long-context-repository',
            repository_url='https://example.test/' + ('z' * CONTEXT_PATH_MAX_LENGTH),
            repository_root='/' + ('x' * CONTEXT_PATH_MAX_LENGTH),
            cwd='/' + ('y' * CONTEXT_PATH_MAX_LENGTH),
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['repository_url']['code'] == ['context_repository_url_too_long']
    assert response.json()['repository_root']['code'] == ['context_repository_root_too_long']
    assert response.json()['cwd']['code'] == ['context_cwd_too_long']
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_rejects_too_long_metadata_fields_before_creating_records() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-too-long-context-metadata',
            agent_version='v' * (CONTEXT_AGENT_VERSION_MAX_LENGTH + 1),
            agent_external_id='a' * (CONTEXT_METADATA_MAX_LENGTH + 1),
            correlation_id='c' * (CONTEXT_METADATA_MAX_LENGTH + 1),
            trace_id='t' * (CONTEXT_METADATA_MAX_LENGTH + 1),
            branch='b' * (CONTEXT_METADATA_MAX_LENGTH + 1),
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['agent_version']['code'] == ['context_agent_version_too_long']
    assert response.json()['agent_external_id']['code'] == ['context_agent_external_id_too_long']
    assert response.json()['correlation_id']['code'] == ['context_correlation_id_too_long']
    assert response.json()['trace_id']['code'] == ['context_trace_id_too_long']
    assert response.json()['branch']['code'] == ['context_branch_too_long']
    assert Agent.objects.count() == 0
    assert AgentSession.objects.count() == 0
    assert AuditEvent.objects.count() == 0
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_denies_wrong_project_before_retrieval() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(other_project, team, request_id='request-wrong-project'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_filters_other_team_memory_before_ranking() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Security', slug='security')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    visible_memory, _visible_version, _visible_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Visible team memory',
        body='Shared exact phrase belongs to the platform team.',
        visibility_scope=VisibilityScope.TEAM,
        exact_terms=['shared exact phrase'],
    )
    hidden_memory, _hidden_version, _hidden_document = create_approved_memory_document(
        organization,
        other_team,
        project,
        title='Hidden team memory',
        body='Shared exact phrase belongs to the security team.',
        visibility_scope=VisibilityScope.TEAM,
        exact_terms=['shared exact phrase'],
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-team-filter',
            query='shared exact phrase',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['memory_id'] for item in body['items']] == [str(visible_memory.id)]
    assert str(hidden_memory.id) not in str(body)
    assert ContextBundleItem.objects.count() == 1


@pytest.mark.django_db
def test_session_start_filter_only_returns_authorized_project_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Filter only memory',
        body='Filter-only session start can return approved project memory.',
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-filter-only',
            query='',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['memory_id'] for item in body['items']] == [str(memory.id)]
    assert body['items'][0]['inclusion_reason'] == 'filter-only authorized memory'


@pytest.mark.django_db
def test_session_start_replay_returns_existing_bundle_without_duplicate_audit() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, team, project)
    client = APIClient()
    payload = valid_context_payload(project, team, request_id='request-replay')

    first = client.post('/v1/context/session-start', payload, format='json', **auth_headers())
    second = client.post('/v1/context/session-start', payload, format='json', **auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()['context_bundle_id'] == first.json()['context_bundle_id']
    assert ContextBundle.objects.count() == 1
    assert ContextBundleItem.objects.count() == 1
    assert AuditEvent.objects.filter(event_type='MemoryRetrieved').count() == 1


@pytest.mark.django_db
def test_session_start_replay_denies_existing_bundle_outside_current_team_scope() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Security', slug='security')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    other_owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-context-security',
        display_name='Security context service account',
    )
    role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=other_owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=other_owner, role=role)
    hidden_memory, _hidden_version, _hidden_document = create_approved_memory_document(
        organization,
        other_team,
        project,
        title='Security-only memory',
        body='Replay collisions must not reveal this security-team memory.',
        visibility_scope=VisibilityScope.TEAM,
        exact_terms=['security replay collision'],
    )
    create_scoped_api_key(
        organization,
        other_team,
        project,
        other_owner,
        raw_key=OTHER_RAW_KEY,
        capabilities=('memories:read',),
    )
    client = APIClient()

    first = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            other_team,
            request_id='request-cross-team-replay',
            query='security replay collision',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(OTHER_RAW_KEY),
    )
    second = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-cross-team-replay',
            query='security replay collision',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 200
    assert [item['memory_id'] for item in first.json()['items']] == [str(hidden_memory.id)]
    assert second.status_code == 403
    assert second.json()['code'] == 'team_scope_denied'
    assert str(hidden_memory.id) not in str(second.json())
    assert ContextBundle.objects.count() == 1
    assert ContextBundleItem.objects.count() == 1


@pytest.mark.django_db
def test_session_start_redacts_token_shaped_query_before_persisting_bundle() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-redacted-query',
            query=f'remember this token {RAW_KEY}',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get()

    assert RAW_KEY not in bundle.query_text
    assert '[REDACTED]' in bundle.query_text


@pytest.mark.django_db
def test_session_start_redacts_token_shaped_memory_and_match_values_before_response_or_item_metadata() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    leaked_token = 'egk_memory_secret_0123456789abcdefghijklmnopqrstuvwxyz'
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title=f'Redact memory {leaked_token}',
        body=f'Memory body contains {leaked_token}.',
        file_paths=[],
        symbols=[],
        exact_terms=[f'match {leaked_token}'],
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-redacted-memory',
            query=f'match {leaked_token}',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    bundle = ContextBundle.objects.get()
    item = ContextBundleItem.objects.get()

    assert body['items'][0]['memory_id'] == str(memory.id)
    assert leaked_token not in str(body)
    assert leaked_token not in bundle.rendered_text
    assert leaked_token not in item.inclusion_reason
    assert leaked_token not in str(item.metadata)
    assert '[REDACTED]' in str(body)
    assert '[REDACTED]' in bundle.rendered_text
    assert '[REDACTED]' in item.inclusion_reason
    assert '[REDACTED]' in str(item.metadata)


@pytest.mark.django_db
def test_task_context_endpoint_uses_task_purpose() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, request_id='request-task-context'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['purpose'] == 'task'
    assert body['hook_specific_output'] == {}
    assert ContextBundle.objects.get().purpose == 'task'


@pytest.mark.django_db
def test_index_memory_version_creates_retrieval_document_for_approved_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-index')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-index',
        runtime='codex',
    )
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title='Index source files',
        files_read=['apps/backend/engram/context/views.py'],
        files_modified=['apps/backend/engram/context/services.py'],
        content_hash='observation-index-hash',
    )
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Index approved memory',
        body='Approved memory should become exact searchable context.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={
            'file_paths': ['docs/search-and-retrieval.md'],
            'symbols': ['IndexMemoryVersion'],
            'exact_terms': ['exact searchable context'],
        },
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        source_observation=observation,
        version=1,
        body=memory.body,
        content_hash='memory-index-version-hash',
    )

    result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    document = result.retrieval_document
    assert result.created is True
    assert document.organization_id == organization.id
    assert document.project_id == project.id
    assert document.team_id == team.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.visibility_scope == VisibilityScope.PROJECT
    assert document.full_text == 'Index approved memory\n\nApproved memory should become exact searchable context.'
    assert document.file_paths == [
        'docs/search-and-retrieval.md',
        'apps/backend/engram/context/views.py',
        'apps/backend/engram/context/services.py',
    ]
    assert document.symbols == ['IndexMemoryVersion']
    assert 'exact searchable context' in document.exact_terms
    assert 'index approved memory' in document.exact_terms


@pytest.mark.django_db
def test_index_memory_version_rejects_non_approved_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Rejected memory',
        body='Rejected memory must not be indexed.',
        status=MemoryStatus.ARCHIVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='memory-archived-version-hash',
    )

    with pytest.raises(ContextIndexError):
        IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    assert RetrievalDocument.objects.count() == 0


@pytest.mark.django_db
def test_index_memory_version_rejects_refuted_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Refuted memory',
        body='Refuted memory must not be indexed.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        refuted=True,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='memory-refuted-version-hash',
    )

    with pytest.raises(ContextIndexError):
        IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    assert RetrievalDocument.objects.count() == 0


@pytest.mark.django_db
def test_context_bundle_returns_semantic_fallback_when_exact_misses() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-semantic-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='color behavior optimization',
            file_paths=[],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get(request_id='request-context-1')
    assert bundle.metadata['retrieval_strategy'] == 'semantic_fallback'
    body = response.json()
    items = body['items']
    assert len(items) == 1
    assert items[0]['inclusion_reason'].startswith('semantic match: cosine')
    assert bundle.metadata['semantic_provider_call_id']
    audit = AuditEvent.objects.get(event_type='MemoryRetrieved', target_id=str(bundle.id))
    assert audit.metadata['retrieval_strategy'] == 'semantic_fallback'
    assert audit.metadata['semantic_provider_call_id'] == bundle.metadata['semantic_provider_call_id']
    assert audit.metadata['semantic_document_ids'] == [items[0]['retrieval_document_id']]


@pytest.mark.django_db
def test_context_bundle_keeps_exact_strategy_when_limit_filled() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation pattern for retrieval fallback.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-exact-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))
    document = RetrievalDocument.objects.get(memory_version=version)

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='colour',
            file_paths=[document.file_paths[0]] if document.file_paths else [],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    body = response.json()
    bundle = ContextBundle.objects.get(request_id='request-context-1')
    assert bundle.metadata['retrieval_strategy'] == 'exact'
    assert 'semantic_provider_call_id' not in bundle.metadata
    assert body['items']


@pytest.mark.django_db
def test_bundle_metadata_always_includes_token_budget_fields() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, token_budget=2000),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get()
    assert bundle.metadata['token_budget'] == 2000
    assert bundle.metadata['tokens_used'] > 0
    assert bundle.metadata['dropped_for_budget'] == 0
    assert bundle.selected_count == 1


@pytest.mark.django_db
def test_bundle_small_token_budget_trims_lower_ranked_matches() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    # memory1: file_path match -> score 100 -> rank 1
    memory1, _v1, _d1 = create_approved_memory_document(
        organization,
        team,
        project,
        title='A',
        body='B',
        file_paths=['src/rank1.py'],
        symbols=[],
        exact_terms=[],
    )
    # memory2: symbol match -> score 80 -> rank 2
    _memory2, _v2, _d2 = create_approved_memory_document(
        organization,
        team,
        project,
        title='C',
        body='D',
        file_paths=[],
        symbols=['RankTwo'],
        exact_terms=[],
    )
    client = APIClient()
    # block for rank-1: "- [M1] A\n  B" = 12 chars = 3 tokens
    # block for rank-2: "- [M2] C\n  D" = 12 chars = 3 tokens
    # budget=4 fits rank-1 (3<=4) but not both (3+3=6>4)
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='',
            file_paths=['src/rank1.py'],
            symbols=['RankTwo'],
            token_budget=4,
            request_id='req-token-trim',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get(request_id='req-token-trim')
    assert bundle.selected_count == 1
    assert bundle.metadata['token_budget'] == 4
    assert bundle.metadata['dropped_for_budget'] == 1
    assert bundle.metadata['tokens_used'] <= 4
    assert ContextBundleItem.objects.filter(bundle=bundle).count() == 1
    item = ContextBundleItem.objects.get(bundle=bundle)
    assert item.memory_id == memory1.id


@pytest.mark.django_db
def test_bundle_over_budget_top_match_is_kept() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(
        organization,
        team,
        project,
        title='BigMemory',
        body='B' * 800,
        file_paths=['src/big.py'],
        symbols=[],
        exact_terms=[],
    )
    client = APIClient()
    # The single match costs ~200 tokens; budget=1 < cost but it must still be kept
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='',
            file_paths=['src/big.py'],
            symbols=[],
            token_budget=1,
            request_id='req-over-budget',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get(request_id='req-over-budget')
    assert bundle.selected_count == 1
    assert bundle.metadata['dropped_for_budget'] == 0
    assert bundle.metadata['tokens_used'] > 1


@pytest.mark.django_db
def test_bundle_none_token_budget_metadata_present() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, token_budget=None),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    bundle = ContextBundle.objects.get()
    assert bundle.metadata['token_budget'] is None
    assert 'tokens_used' in bundle.metadata
    assert bundle.metadata['dropped_for_budget'] == 0


@pytest.mark.django_db
def test_context_bundle_skips_semantic_fallback_without_embedding_policy() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Colour behaviour optimisation',
        body='Colour behaviour optimisation pattern for retrieval fallback.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-no-embedding-policy-1',
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='color behavior optimization',
            file_paths=[],
            symbols=[],
            limit=5,
        ),
        format='json',
        **auth_headers(),
    )

    body = response.json()
    bundle = ContextBundle.objects.get(request_id='request-context-1')
    assert bundle.metadata['retrieval_strategy'] == 'exact'
    assert body['items'] == []
