from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone
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
    AuditResult,
    CandidateStatus,
    ContextBundle,
    ContextBundleItem,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Observation,
    Organization,
    OrganizationSettings,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWorkType,
)
from engram.memory.digest_visibility_tests import make_source_memory
from engram.memory.tasks import generate_weekly_digest_work_v1
from engram.memory.workflow_work import CreateWorkflowWorkInput
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
    confidence: Decimal | None = None,
    kind: str = '',
) -> tuple[Memory, MemoryVersion, RetrievalDocument]:
    metadata: dict[str, Any] = {
        'file_paths': file_paths or ['apps/backend/engram/context/services.py'],
        'symbols': symbols or ['BuildContextBundle'],
        'exact_terms': exact_terms or ['context bundle', 'authorization before ranking'],
    }
    if kind:
        metadata['kind'] = kind
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        confidence=confidence,
        metadata=metadata,
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


def build_ordered_proven_digest(organization: Organization, project: Project, *, schedule_key: str) -> Memory:
    import engram.memory.digest_work as digest_work

    make_source_memory(organization, project, title=f'Source {schedule_key}', body=f'source body {schedule_key}')
    now = timezone.now()
    with transaction.atomic():
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=now - timedelta(days=7),
            window_end=now + timedelta(minutes=5),
            schedule_key=schedule_key,
        )
        work, _created = digest_work.create_digest_work_and_signal(
            data=CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.WEEKLY_DIGEST,
                subject_type=WorkflowSubjectType.PROJECT,
                subject_id=project.id,
                input_snapshot=snapshot,
                occurrence_key=schedule_key,
            ),
            signal_task=generate_weekly_digest_work_v1,
        )
    generate_weekly_digest_work_v1(str(work.id))

    return Memory.objects.get(
        organization=organization,
        project=project,
        kind='digest',
        metadata__digest_visibility__workflow_work_id=str(work.id),
    )


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
    assert body['status'] == 'injected'
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
            'confidence': None,
            'kind': '',
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


CONTEXT_AGENT_RAW_KEY = 'egk_test_context_agent_0123456789abcdefghijklmnopqrstuvwxyz'
CONTEXT_AGENT_CAPS = ('memories:read', 'projects:agent')


def create_context_org_agent_key(organization: Organization) -> None:
    from engram.access.models import IdentityType, RoleCapability

    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in CONTEXT_AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='context-agent',
        display_name='Context agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='context agent key',
        key_prefix=api_key_prefix(CONTEXT_AGENT_RAW_KEY),
        key_hash=hash_api_key(CONTEXT_AGENT_RAW_KEY),
        key_fingerprint=api_key_fingerprint(CONTEXT_AGENT_RAW_KEY),
        active=True,
    )
    for code in CONTEXT_AGENT_CAPS:
        ApiKeyCapability.objects.get_or_create(
            api_key=api_key,
            capability=Capability.objects.get(code=code),
        )


@pytest.mark.django_db
def test_session_start_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/context-demo.git'
    project.save(update_fields=['repository_url'])
    create_context_org_agent_key(organization)
    create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            project_id=None,
            team_id=None,
            repository_url='https://github.com/acme/context-demo',
            request_id='request-context-repo-url',
        ),
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {CONTEXT_AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()


@pytest.mark.django_db
def test_session_start_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-context',
        repository_url='git@github.com:acme/foreign-context.git',
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            project_id=None,
            repository_url='https://github.com/acme/foreign-context',
            request_id='request-context-foreign-inorg',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    audit = AuditEvent.objects.get(event_type='AccessScopeResolved', project_id=foreign_project.id)
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['resolved_project_id'] == str(foreign_project.id)
    assert ContextBundle.objects.count() == 0


@pytest.mark.django_db
def test_session_start_bound_key_with_agent_capability_denied_for_foreign_project() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-context-footgun',
        repository_url='git@github.com:acme/foreign-context-footgun.git',
    )
    agent_raw_key = 'egk_test_context_footgun_0123456789abcdefghijklmnopqrstuv'
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=agent_raw_key,
        capabilities=('memories:read', 'projects:agent'),
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            project_id=None,
            repository_url='https://github.com/acme/foreign-context-footgun',
            request_id='request-context-footgun-denied',
        ),
        format='json',
        **auth_headers(agent_raw_key),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_session_start_bound_key_with_agent_capability_allowed_for_own_project() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/own-context-project.git'
    project.save(update_fields=['repository_url'])
    agent_raw_key = 'egk_test_context_footgun_own_0123456789abcdefghijklmnopqrstuv'
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=agent_raw_key,
        capabilities=('memories:read', 'projects:agent'),
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            project_id=None,
            repository_url='https://github.com/acme/own-context-project',
            request_id='request-context-footgun-allowed',
        ),
        format='json',
        **auth_headers(agent_raw_key),
    )

    assert response.status_code == 200, response.json()


@pytest.mark.django_db
def test_session_start_bound_key_unknown_repository_url_returns_404_without_creating_project() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    project_count_before = Project.objects.filter(organization=organization).count()
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            project_id=None,
            repository_url='https://github.com/acme/never-created-context',
            request_id='request-context-unknown-repo',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'
    assert Project.objects.filter(organization=organization).count() == project_count_before


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
def test_session_start_filter_only_keeps_single_most_recent_digest() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    digest_one = build_ordered_proven_digest(organization, project, schedule_key='weekly:digest-one')
    digest_two = build_ordered_proven_digest(organization, project, schedule_key='weekly:digest-two')
    digest_three = build_ordered_proven_digest(organization, project, schedule_key='weekly:digest-three')
    non_digest_one, _v4, _d4 = create_approved_memory_document(
        organization,
        team,
        project,
        title='Non digest one',
        body='First non-digest memory.',
    )
    non_digest_two, _v5, _d5 = create_approved_memory_document(
        organization,
        team,
        project,
        title='Non digest two',
        body='Second non-digest memory.',
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-filter-only-digest-cap',
            query='',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    memory_ids = {item['memory_id'] for item in body['items']}
    assert memory_ids == {str(digest_three.id), str(non_digest_one.id), str(non_digest_two.id)}
    assert str(digest_one.id) not in memory_ids
    assert str(digest_two.id) not in memory_ids


@pytest.mark.django_db
def test_session_start_orders_same_tier_matches_by_confidence_before_recency() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    older_high_confidence, _v1, _d1 = create_approved_memory_document(
        organization,
        team,
        project,
        title='Older high confidence',
        body='Confidence tiebreak fixture body one.',
        file_paths=[],
        symbols=[],
        exact_terms=['confidence tiebreak fixture'],
        confidence=Decimal('0.900'),
    )
    newer_low_confidence, _v2, _d2 = create_approved_memory_document(
        organization,
        team,
        project,
        title='Newer low confidence',
        body='Confidence tiebreak fixture body two.',
        file_paths=[],
        symbols=[],
        exact_terms=['confidence tiebreak fixture'],
        confidence=Decimal('0.500'),
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-confidence-tiebreak',
            query='confidence tiebreak fixture',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['memory_id'] for item in body['items']] == [
        str(older_high_confidence.id),
        str(newer_low_confidence.id),
    ]


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
def test_session_start_item_and_rendered_context_include_confidence_and_kind() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        confidence=Decimal('0.950'),
        kind='gotcha',
    )
    client = APIClient()
    payload = valid_context_payload(project, team, request_id='request-confidence-kind')

    first = client.post('/v1/context/session-start', payload, format='json', **auth_headers())
    second = client.post('/v1/context/session-start', payload, format='json', **auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    for response in (first, second):
        body = response.json()
        assert body['items'][0]['confidence'] == '0.950'
        assert body['items'][0]['kind'] == 'gotcha'
        assert f'{memory.title} (gotcha, confidence 0.950)' in body['rendered_context']


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
        observation_sequence_cursor=1,
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
        session_sequence=1,
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


@pytest.mark.skipif(VectorField is None, reason='pgvector not installed')
@pytest.mark.django_db
def test_context_bundle_applies_lexical_fusion_when_enabled() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    OrganizationSettings.objects.create(organization=organization, lexical_fusion_enabled=True)
    for index, (title, body) in enumerate(
        (
            ('Colour behaviour optimisation', 'Colour behaviour optimisation'),
            ('Behaviour optimisation colour', 'Behaviour optimisation colour'),
        ),
        start=1,
    ):
        memory = Memory.objects.create(
            organization=organization,
            project=project,
            team=team,
            title=title,
            body=body,
            status=MemoryStatus.APPROVED,
            visibility_scope=VisibilityScope.PROJECT,
        )
        version = MemoryVersion.objects.create(
            organization=organization,
            project=project,
            memory=memory,
            version=1,
            body=body,
            content_hash=f'hash-fusion-{index}',
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
    items = response.json()['items']
    assert len(items) == 2
    assert all(item['inclusion_reason'].startswith('semantic match: cosine') for item in items)
    assert sorted(item['title'] for item in items) == ['Behaviour optimisation colour', 'Colour behaviour optimisation']


def _seed_recall_document(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    title: str,
    body: str,
    exact_terms: list[str],
    sequence: int,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'recall-hash-{sequence}',
    )

    return RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        source_observation_ids=[],
        file_paths=[],
        symbols=[],
        exact_terms=exact_terms,
        full_text=f'{title}\n\n{body}',
    )


def _seed_lexical_recall_scope() -> tuple[Organization, Team, Project, RetrievalDocument, RetrievalDocument]:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    anchor = _seed_recall_document(
        organization,
        team,
        project,
        title='Authorization anchor',
        body='Authorization anchor',
        exact_terms=['authorization'],
        sequence=1,
    )
    fuzzy = _seed_recall_document(
        organization,
        team,
        project,
        title='authorisation',
        body='authorisation',
        exact_terms=[],
        sequence=2,
    )

    return organization, team, project, anchor, fuzzy


@pytest.mark.django_db
def test_context_bundle_flag_off_lexical_recall_is_byte_identical() -> None:
    organization, team, project, anchor, fuzzy = _seed_lexical_recall_scope()
    assert 'authorization' not in fuzzy.full_text.casefold()

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, query='authorization', file_paths=[], symbols=[], limit=5),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    assert [item['retrieval_document_id'] for item in items] == [str(anchor.id)]
    assert [item['inclusion_reason'] for item in items] == ['exact match: authorization']
    assert str(fuzzy.id) not in {item['retrieval_document_id'] for item in items}
    assert all(not item['inclusion_reason'].startswith('lexical match:') for item in items)
    bundle = ContextBundle.objects.get(request_id='request-context-1')
    assert bundle.metadata['retrieval_strategy'] == 'exact'


@pytest.mark.django_db
def test_context_bundle_flag_on_surfaces_fuzzy_lexical_only_document() -> None:
    organization, team, project, anchor, fuzzy = _seed_lexical_recall_scope()
    OrganizationSettings.objects.create(organization=organization, lexical_recall_enabled=True)

    client = APIClient()
    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, query='authorization', file_paths=[], symbols=[], limit=5),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    item_ids = {item['retrieval_document_id'] for item in items}
    assert str(anchor.id) in item_ids
    assert str(fuzzy.id) in item_ids
    fuzzy_item = next(item for item in items if item['retrieval_document_id'] == str(fuzzy.id))
    assert fuzzy_item['inclusion_reason'].startswith('lexical match:')
    bundle = ContextBundle.objects.get(request_id='request-context-1')
    assert bundle.metadata['retrieval_strategy'] == 'lexical_recall'
    audit = AuditEvent.objects.get(event_type='MemoryRetrieved', target_id=str(bundle.id))
    assert audit.metadata['retrieval_strategy'] == 'lexical_recall'


@pytest.mark.skipif(VectorField is None, reason='pgvector not installed')
@pytest.mark.django_db
def test_index_memory_version_populates_embedding_pgvector() -> None:
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
        content_hash='hash-pgvector-1',
    )

    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    document = RetrievalDocument.objects.get(memory_version=version)
    assert document.embedding_vector
    assert document.embedding_pgvector is not None
    assert list(document.embedding_pgvector) == pytest.approx(document.embedding_vector, abs=1e-5)


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


@pytest.mark.django_db
def test_user_prompt_submit_returns_cited_exact_context_and_persists_bundle() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    memory, version, document = create_approved_memory_document(_organization, team, project)
    client = APIClient()

    response = client.post(
        '/v1/context/user-prompt-submit',
        valid_context_payload(project, team, request_id='request-ups-1'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'injected'
    assert body['request_id'] == 'request-ups-1'
    assert body['purpose'] == 'user_prompt_submit'
    assert body['context_bundle_id']
    assert body['hook_specific_output'] == {
        'hookEventName': 'UserPromptSubmit',
        'additionalContext': body['rendered_context'],
    }
    assert 'M1' in body['rendered_context']
    assert memory.title in body['rendered_context']
    bundle = ContextBundle.objects.get(request_id='request-ups-1')
    assert bundle.purpose == 'user_prompt_submit'


@pytest.mark.django_db
def test_user_prompt_submit_empty_bundle_renders_empty_string() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    OrganizationSettings.objects.create(organization=organization, hybrid_retrieval_enabled=False)
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Unrelated memory',
        body='Nothing here relates to the prompt below.',
        file_paths=['src/unrelated.py'],
        symbols=['UnrelatedThing'],
        exact_terms=['unrelated exact term'],
    )
    client = APIClient()

    response = client.post(
        '/v1/context/user-prompt-submit',
        valid_context_payload(
            project,
            team,
            request_id='request-ups-empty',
            query='completely different topic altogether',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == []
    assert body['rendered_context'] == ''
    assert body['hook_specific_output'] == {'hookEventName': 'UserPromptSubmit', 'additionalContext': ''}
    bundle = ContextBundle.objects.get(request_id='request-ups-empty')
    assert bundle.rendered_text == ''


@pytest.mark.django_db
def test_session_start_empty_bundle_still_renders_stub_text() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    OrganizationSettings.objects.create(organization=organization, hybrid_retrieval_enabled=False)
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Unrelated memory',
        body='Nothing here relates to the prompt below.',
        file_paths=['src/unrelated.py'],
        symbols=['UnrelatedThing'],
        exact_terms=['unrelated exact term'],
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-ss-empty',
            query='completely different topic altogether',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == []
    assert body['rendered_context'] == '# Engram context\n\nNo approved memory matched this request.'
    bundle = ContextBundle.objects.get(request_id='request-ss-empty')
    assert bundle.rendered_text == '# Engram context\n\nNo approved memory matched this request.'


@pytest.mark.django_db
def test_user_prompt_submit_empty_bundle_with_warnings_renders_warnings_block_alone() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Unrelated memory',
        body='Nothing here relates to the prompt below.',
        file_paths=['src/unrelated.py'],
        symbols=['UnrelatedThing'],
        exact_terms=['unrelated exact term'],
    )
    client = APIClient()

    response = client.post(
        '/v1/context/user-prompt-submit',
        valid_context_payload(
            project,
            team,
            request_id='request-ups-empty-warnings',
            query='completely different topic altogether',
            file_paths=[],
            symbols=[],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == [
        {
            'code': 'semantic_unavailable',
            'message': 'semantic retrieval unavailable: embedding could not be resolved',
            'memory_id': None,
        },
    ]
    expected_rendered_context = '> Warnings:\n> - semantic retrieval unavailable: embedding could not be resolved'
    assert body['rendered_context'] == expected_rendered_context
    assert not body['rendered_context'].startswith('\n')


@pytest.mark.django_db
def test_session_start_warns_when_matches_dropped_for_budget() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
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

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='',
            file_paths=['src/rank1.py'],
            symbols=['RankTwo'],
            token_budget=4,
            request_id='req-warn-budget',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == [
        {'code': 'budget_dropped', 'message': '1 matching memories dropped for token budget', 'memory_id': None},
    ]
    assert '> Warnings:' in body['rendered_context']
    assert '> - 1 matching memories dropped for token budget' in body['rendered_context']
    assert [item['memory_id'] for item in body['items']] == [str(memory1.id)]


@pytest.mark.django_db
def test_session_start_warns_when_semantic_retrieval_unavailable() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='color behavior optimization',
            file_paths=[],
            symbols=[],
            request_id='req-warn-semantic',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == [
        {
            'code': 'semantic_unavailable',
            'message': 'semantic retrieval unavailable: embedding could not be resolved',
            'memory_id': None,
        },
    ]
    assert '> Warnings:' in body['rendered_context']


@pytest.mark.django_db
def test_session_start_warns_and_hides_stale_matching_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    stale_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Stale rollout notes',
        body='Stale rollout notes body',
        file_paths=[],
        symbols=[],
        exact_terms=['stale rollout phrase'],
    )
    stale_memory.stale = True
    stale_memory.save(update_fields=['stale'])
    RetrievalDocument.objects.filter(memory=stale_memory).update(stale=True)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='stale rollout phrase',
            file_paths=[],
            symbols=[],
            request_id='req-warn-stale',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['warnings'] == [
        {
            'code': 'stale_match',
            'message': f'stale memory matched: "{stale_memory.title}"',
            'memory_id': str(stale_memory.id),
        },
    ]
    assert '> - stale memory matched:' in body['rendered_context']


@pytest.mark.django_db
def test_session_start_prefers_refuted_over_stale_for_matched_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Refuted overlap notes',
        body='Refuted overlap notes body',
        file_paths=[],
        symbols=[],
        exact_terms=['refuted overlap phrase'],
    )
    memory.stale = True
    memory.refuted = True
    memory.save(update_fields=['stale', 'refuted'])
    RetrievalDocument.objects.filter(memory=memory).update(stale=True, refuted=True)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='refuted overlap phrase',
            file_paths=[],
            symbols=[],
            request_id='req-warn-refuted-precedence',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == [
        {
            'code': 'refuted_match',
            'message': f'refuted memory matched: "{memory.title}"',
            'memory_id': str(memory.id),
        },
    ]


@pytest.mark.django_db
def test_session_start_caps_stale_and_refuted_warnings_at_three() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    for index in range(4):
        memory, _version, _document = create_approved_memory_document(
            organization,
            team,
            project,
            title=f'Stale note {index}',
            body='Stale note body',
            file_paths=[],
            symbols=[],
            exact_terms=['shared stale cap phrase'],
        )
        memory.stale = True
        memory.save(update_fields=['stale'])
        RetrievalDocument.objects.filter(memory=memory).update(stale=True)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='shared stale cap phrase',
            file_paths=[],
            symbols=[],
            request_id='req-warn-cap',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    stale_warnings = [warning for warning in body['warnings'] if warning['code'] == 'stale_match']
    assert len(stale_warnings) == 3


@pytest.mark.django_db
def test_session_start_stale_warning_excludes_memory_outside_team_scope() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    other_team = Team.objects.create(organization=organization, name='Security', slug='security')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    stale_memory, _version, _document = create_approved_memory_document(
        organization,
        other_team,
        project,
        title='Security-only stale memory',
        body='Security-only stale memory body',
        visibility_scope=VisibilityScope.TEAM,
        file_paths=[],
        symbols=[],
        exact_terms=['security stale scoped phrase'],
    )
    stale_memory.stale = True
    stale_memory.save(update_fields=['stale'])
    RetrievalDocument.objects.filter(memory=stale_memory).update(stale=True)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='security stale scoped phrase',
            file_paths=[],
            symbols=[],
            request_id='req-warn-stale-team-scope',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == []
    assert stale_memory.title not in str(body)


@pytest.mark.django_db
def test_session_start_warns_about_unresolved_conflicting_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Conflicting candidate',
        body='Conflicting candidate body',
        status=CandidateStatus.PROPOSED,
        content_hash='conflict-candidate-hash',
    )
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
        label='contradiction claim',
    )
    client = APIClient()

    response = client.post(
        '/v1/context/session-start',
        valid_context_payload(project, team, request_id='req-warn-conflict'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == [
        {
            'code': 'conflicting_memory',
            'message': 'memory has an unresolved contradiction claim',
            'memory_id': str(memory.id),
        },
    ]
    assert '> - memory has an unresolved contradiction claim' in body['rendered_context']

    MemoryLink.objects.filter(memory=memory, link_type=LinkType.CONFLICTS_WITH).delete()
    response_after_resolution = client.post(
        '/v1/context/session-start',
        valid_context_payload(project, team, request_id='req-warn-conflict-resolved'),
        format='json',
        **auth_headers(),
    )

    assert response_after_resolution.status_code == 200
    assert response_after_resolution.json()['warnings'] == []


@pytest.mark.django_db
def test_session_start_replay_returns_persisted_warnings_verbatim_after_state_change() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Conflicting candidate',
        body='Conflicting candidate body',
        status=CandidateStatus.PROPOSED,
        content_hash='conflict-candidate-hash-replay',
    )
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
        label='contradiction claim',
    )
    client = APIClient()
    payload = valid_context_payload(project, team, request_id='req-warn-replay')
    expected_warnings = [
        {
            'code': 'conflicting_memory',
            'message': 'memory has an unresolved contradiction claim',
            'memory_id': str(memory.id),
        },
    ]

    first = client.post('/v1/context/session-start', payload, format='json', **auth_headers())
    MemoryLink.objects.filter(memory=memory, link_type=LinkType.CONFLICTS_WITH).delete()
    second = client.post('/v1/context/session-start', payload, format='json', **auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['warnings'] == expected_warnings
    assert second.json()['warnings'] == expected_warnings
    assert ContextBundle.objects.filter(request_id='req-warn-replay').count() == 1


@pytest.mark.django_db
def test_session_start_kinds_filter_returns_only_matching_kind() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    gotcha_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Gotcha memory',
        body='Gotcha memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds filter phrase'],
        kind='gotcha',
    )
    decision_memory, _decision_version, _decision_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Decision memory',
        body='Decision memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds filter phrase'],
        kind='decision',
    )
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='kinds filter phrase',
            file_paths=[],
            symbols=[],
            request_id='req-kinds-filter',
            kinds=['gotcha'],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['memory_id'] for item in body['items']] == [str(gotcha_memory.id)]
    assert str(decision_memory.id) not in str(body)


@pytest.mark.django_db
def test_session_start_kinds_invalid_value_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, request_id='req-kinds-invalid', kinds=['bogus']),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert 'bogus' in str(response.json())


@pytest.mark.django_db
def test_session_start_kinds_max_items_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(project, team, request_id='req-kinds-too-many', kinds=['gotcha'] * 7),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_session_start_kinds_narrows_stale_warning_scan() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    gotcha_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Stale gotcha memory',
        body='Stale gotcha memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds stale phrase'],
        kind='gotcha',
    )
    gotcha_memory.stale = True
    gotcha_memory.save(update_fields=['stale'])
    RetrievalDocument.objects.filter(memory=gotcha_memory).update(stale=True)
    decision_memory, _decision_version, _decision_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Stale decision memory',
        body='Stale decision memory body',
        file_paths=[],
        symbols=[],
        exact_terms=['kinds stale phrase'],
        kind='decision',
    )
    decision_memory.stale = True
    decision_memory.save(update_fields=['stale'])
    RetrievalDocument.objects.filter(memory=decision_memory).update(stale=True)
    client = APIClient()

    response = client.post(
        '/v1/context',
        valid_context_payload(
            project,
            team,
            query='kinds stale phrase',
            file_paths=[],
            symbols=[],
            request_id='req-kinds-stale-scope',
            kinds=['gotcha'],
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['warnings'] == [
        {
            'code': 'stale_match',
            'message': f'stale memory matched: "{gotcha_memory.title}"',
            'memory_id': str(gotcha_memory.id),
        },
    ]
