from __future__ import annotations

from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    SessionStatus,
    Team,
)
from engram.hooks.services import HookEventInput, IngestHookEvent

RAW_KEY = 'egk_test_hook_ingest_0123456789abcdefghijklmnopqrstuvwxyz'
HOOK_PAYLOAD_MAX_BYTES = 65536
HOOK_OBSERVATION_BODY_MAX_LENGTH = 16000
HOOK_PATH_MAX_LENGTH = 1024
HOOK_PATH_LIST_MAX_ITEMS = 100


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


@pytest.fixture
def f_capture_on_commit(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> DjangoCaptureOnCommitCallbacks:
    return django_capture_on_commit_callbacks


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
        external_id='svc-hooks',
        display_name='Hook service account',
    )
    role, _created = Role.objects.get_or_create(
        code='developer',
        defaults={'name': 'Developer', 'built_in': True},
    )
    developer_capability_descriptions = {
        'observations:write': 'Submit observations.',
        'observations:read': 'Read observations.',
        'memories:read': 'Read approved memory.',
        'memories:propose': 'Propose memory updates.',
        'search:query': 'Query memory search.',
    }
    developer_capabilities: dict[str, Capability] = {}
    for capability_code, description in developer_capability_descriptions.items():
        capability, _created = Capability.objects.get_or_create(
            code=capability_code,
            defaults={'description': description},
        )
        developer_capabilities[capability_code] = capability
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Hook key',
        key_prefix=api_key_prefix(RAW_KEY),
        key_hash=hash_api_key(RAW_KEY),
        key_fingerprint=api_key_fingerprint(RAW_KEY),
        team=team,
        project=project,
    )
    for capability_code in ('observations:write', 'memories:read'):
        ApiKeyCapability.objects.create(
            api_key=api_key,
            capability=developer_capabilities[capability_code],
        )

    return organization, team, project, owner, api_key


def create_hook_scope() -> tuple[Organization, Project, Team, str]:
    organization, team, project, _owner, _api_key = create_project_scope()

    return organization, project, team, RAW_KEY


def auth_headers(raw_key: str = RAW_KEY) -> dict[str, str]:
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


def valid_hook_payload(project: Project, team: Team, **overrides: Any) -> dict[str, Any]:
    payload = {
        'project_id': str(project.id),
        'team_id': str(team.id),
        'agent_runtime': 'codex',
        'agent_version': '0.1.0',
        'agent_external_id': 'codex-local',
        'session_id': 'session-1',
        'event_id': 'event-1',
        'idempotency_key': 'idem-1',
        'event_type': 'post_tool_use',
        'payload_schema_version': 'v1',
        'sequence_number': 1,
        'occurred_at': '2026-06-25T00:00:00Z',
        'content_hash': 'hash-event-1',
        'request_id': 'request-event-1',
        'repository_url': 'https://example.test/engram.git',
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
        'payload': {
            'tool_name': 'bash',
            'tool_input': {'command': 'pytest'},
            'tool_response': {'exit_code': 0},
        },
        'observation': {
            'type': 'tool_use',
            'title': 'bash completed',
            'body': 'pytest exited 0',
            'files_read': ['apps/backend/engram/core/models.py'],
            'files_modified': ['apps/backend/engram/hooks/services.py'],
        },
    }
    payload.update(overrides)

    return payload


def hook_event_input(project: Project, team: Team, **overrides: Any) -> HookEventInput:
    fields = {
        'raw_key': RAW_KEY,
        'project_id': project.id,
        'team_id': team.id,
        'agent_runtime': 'codex',
        'agent_version': '0.1.0',
        'agent_external_id': 'codex-local',
        'session_id': 'session-1',
        'event_id': 'event-1',
        'idempotency_key': 'idem-1',
        'event_type': 'post_tool_use',
        'payload_schema_version': 'v1',
        'sequence_number': 1,
        'occurred_at': timezone.now(),
        'content_hash': 'hash-event-1',
        'request_id': 'request-event-1',
        'correlation_id': '',
        'trace_id': '',
        'repository_url': 'https://example.test/engram.git',
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
        'payload': {'tool_name': 'bash', 'tool_input': {'command': 'pytest'}},
        'observation': {
            'type': 'tool_use',
            'title': 'bash completed',
            'body': 'pytest exited 0',
            'files_read': [],
            'files_modified': [],
        },
    }
    fields.update(overrides)

    return HookEventInput(**fields)


@pytest.mark.django_db
def test_hook_dry_run_resolves_scope_without_echoing_raw_key() -> None:
    organization, team, project, _owner, api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/hooks/dry-run',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'agent_runtime': 'codex',
            'agent_version': '0.1.0',
            'request_id': 'dry-run-1',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['request_id'] == 'dry-run-1'
    assert body['resolved_actor'] == {'type': 'api_key', 'id': str(api_key.id)}
    assert body['scope']['organization_id'] == str(organization.id)
    assert body['scope']['project_ids'] == [str(project.id)]
    assert body['scope']['team_ids'] == [str(team.id)]
    assert 'observations:write' in body['scope']['capabilities']
    assert body['server'] == {'health': 'ok'}
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_hook_dry_run_requires_bearer_api_key() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/hooks/dry-run',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'agent_runtime': 'codex',
            'agent_version': '0.1.0',
            'request_id': 'dry-run-missing-key',
        },
        format='json',
    )

    assert response.status_code == 401
    assert response.json()['code'] == 'missing_api_key'


@pytest.mark.django_db
def test_hook_dry_run_denies_wrong_project() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    client = APIClient()

    response = client.post(
        '/v1/hooks/dry-run',
        {
            'project_id': str(other_project.id),
            'team_id': str(team.id),
            'agent_runtime': 'codex',
            'agent_version': '0.1.0',
            'request_id': 'dry-run-wrong-project',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_hook_dry_run_denied_response_matches_global_domain_error_shape() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    client = APIClient()

    response = client.post(
        '/v1/hooks/dry-run',
        {
            'project_id': str(other_project.id),
            'team_id': str(team.id),
            'agent_runtime': 'codex',
            'agent_version': '0.1.0',
            'request_id': 'dry-run-shape-check',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    body = response.json()
    assert body['code'] == 'project_scope_denied'
    assert body['error_code'] == 'project_scope_denied'
    assert body['detail']


@pytest.mark.django_db
def test_post_tool_use_ingests_raw_event_observation_source_and_queues_worker_task() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.post(
        '/v1/hooks/post-tool-use',
        valid_hook_payload(project, team),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body['status'] == 'accepted'
    assert body['duplicate'] is False
    assert body['request_id'] == 'request-event-1'

    agent = Agent.objects.get()
    session = AgentSession.objects.get()
    raw_event = RawEventEnvelope.objects.get()
    observation = Observation.objects.get()
    source = ObservationSource.objects.get()

    assert body['raw_event_id'] == str(raw_event.id)
    assert body['observation_id'] == str(observation.id)
    assert 'outbox_event_id' not in body
    assert body['agent_session_id'] == str(session.id)
    assert agent.runtime == 'codex'
    assert agent.external_id == 'codex-local'
    assert agent.version == '0.1.0'
    assert session.external_session_id == 'session-1'
    assert session.repository_url == 'https://example.test/engram.git'
    assert session.repository_root == '/workspace/engram'
    assert session.branch == 'master'
    assert session.cwd == '/workspace/engram'
    assert raw_event.event_type == 'post_tool_use'
    assert raw_event.client_event_id == 'event-1'
    assert raw_event.idempotency_key == 'idem-1'
    assert raw_event.content_hash == 'hash-event-1'
    assert raw_event.request_id == 'request-event-1'
    assert raw_event.actor_type == 'api_key'
    assert raw_event.payload['tool_name'] == 'bash'
    assert observation.raw_event_id == raw_event.id
    assert observation.observation_type == 'tool_use'
    assert observation.title == 'bash completed'
    assert observation.body == 'pytest exited 0'
    assert observation.files_read == ['apps/backend/engram/core/models.py']
    assert observation.files_modified == ['apps/backend/engram/hooks/services.py']
    assert source.observation_id == observation.id
    assert source.raw_event_id == raw_event.id
    assert source.source_type == 'hook_event'
    assert source.source_id == 'event-1'


@pytest.mark.django_db
def test_post_tool_use_enqueues_memory_worker_task_via_celery_outbox(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    provider_secret = 'sk-test-secret123456789'

    with f_capture_on_commit(execute=True):
        response = client.post(
            '/v1/hooks/post-tool-use',
            valid_hook_payload(
                project,
                team,
                payload={
                    'tool_name': 'bash',
                    'authorization': f'Bearer {RAW_KEY}',
                    'tool_input': {
                        'api_key': provider_secret,
                        'command': f'echo {provider_secret}',
                    },
                    'tool_response': {'stdout': f'token={RAW_KEY}'},
                },
                observation={
                    'type': 'tool_use',
                    'title': 'bash printed a token',
                    'body': f'output contained {provider_secret} and {RAW_KEY}',
                    'files_read': [],
                    'files_modified': [],
                },
            ),
            format='json',
            **auth_headers(),
        )

    assert response.status_code == 202
    body = response.json()
    queued = CeleryOutbox.objects.get()

    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]
    assert queued.kwargs == {}
    transport_payload = f'{queued.args} {queued.kwargs} {queued.options}'
    assert RAW_KEY not in transport_payload
    assert provider_secret not in transport_payload


@pytest.mark.django_db
def test_ingest_hook_event_defers_worker_task_dispatch_until_transaction_commits() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    data = hook_event_input(project, team)

    with transaction.atomic():
        IngestHookEvent().execute(data)

        assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_ingest_hook_event_does_not_dispatch_worker_tasks_when_transaction_rolls_back() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    data = hook_event_input(project, team)

    class RollbackSentinelError(Exception):
        pass

    with pytest.raises(RollbackSentinelError):
        with transaction.atomic():
            IngestHookEvent().execute(data)
            raise RollbackSentinelError

    assert CeleryOutbox.objects.count() == 0
    assert RawEventEnvelope.objects.count() == 0


@pytest.mark.django_db
def test_ingest_hook_event_dispatches_worker_task_exactly_once_on_commit(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    data = hook_event_input(project, team)

    with f_capture_on_commit(execute=True):
        with transaction.atomic():
            result = IngestHookEvent().execute(data)

    assert CeleryOutbox.objects.filter(task_name='engram.memory.process_observation_recorded').count() == 1
    queued = CeleryOutbox.objects.get(task_name='engram.memory.process_observation_recorded')
    assert queued.args == [str(result.observation.id)]


@pytest.mark.django_db
def test_session_start_hook_persists_lifecycle_event_and_queues_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    organization, project, team, raw_key = create_hook_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='session_start',
        event_id='session-start-event-1',
        idempotency_key='session-start-idempotency-1',
        payload={'trigger': 'startup', 'cwd': '/workspace/engram'},
        observation={
            'type': 'session_start',
            'title': 'Session started',
            'body': 'Agent session started for backend work.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/session-start', payload, format='json', **auth_headers(raw_key))

    assert response.status_code == 202
    body = response.json()
    assert RawEventEnvelope.objects.get().event_type == 'session_start'
    assert Observation.objects.get().observation_type == 'session_start'
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]
    assert queued.kwargs == {}


@pytest.mark.django_db
def test_error_hook_persists_error_event_and_queues_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='error',
        event_id='error-event-1',
        idempotency_key='error-idempotency-1',
        payload={'message': 'Command failed', 'exit_code': 1},
        observation={
            'type': 'error',
            'title': 'Command failed',
            'body': 'pytest exited 1.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/error', payload, format='json', **auth_headers())

    assert response.status_code == 202
    body = response.json()
    assert RawEventEnvelope.objects.get().event_type == 'error'
    assert Observation.objects.get().observation_type == 'error'
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]
    assert queued.kwargs == {}


@pytest.mark.django_db
def test_decision_hook_persists_decision_event_and_queues_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='decision',
        event_id='decision-event-1',
        idempotency_key='decision-idempotency-1',
        payload={'decision': 'Use django-celery-outbox transport'},
        observation={
            'type': 'decision',
            'title': 'Outbox transport decision',
            'body': 'Use django-celery-outbox delay transport for worker dispatch.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/decision', payload, format='json', **auth_headers())

    assert response.status_code == 202
    body = response.json()
    assert RawEventEnvelope.objects.get().event_type == 'decision'
    assert Observation.objects.get().observation_type == 'decision'
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]
    assert queued.kwargs == {}


@pytest.mark.django_db
def test_hook_event_endpoint_rejects_mismatched_event_type_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='error',
        event_id='mismatched-event-1',
        idempotency_key='mismatched-idempotency-1',
        payload={'message': 'wrong endpoint'},
        observation={
            'type': 'error',
            'title': 'Wrong endpoint',
            'body': 'Event type does not match endpoint.',
            'files_read': [],
            'files_modified': [],
        },
    )

    response = APIClient().post('/v1/hooks/session-start', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json() == {'event_type': ['Expected session_start.']}
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_error_hook_replay_returns_duplicate_without_new_records_or_queued_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        event_type='error',
        event_id='error-replay-event-1',
        idempotency_key='error-replay-idempotency-1',
        payload={'message': 'Command failed', 'exit_code': 1},
        observation={
            'type': 'error',
            'title': 'Command failed',
            'body': 'pytest exited 1.',
            'files_read': [],
            'files_modified': [],
        },
    )
    replay = {
        **payload,
        'event_id': 'error-replay-event-2',
        'content_hash': 'hash-error-replay-event-2',
        'observation': {**payload['observation'], 'title': 'should not create'},
    }

    with f_capture_on_commit(execute=True):
        first = client.post('/v1/hooks/error', payload, format='json', **auth_headers())
        second = client.post('/v1/hooks/error', replay, format='json', **auth_headers())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()['duplicate'] is True
    assert second.json()['raw_event_id'] == first.json()['raw_event_id']
    assert RawEventEnvelope.objects.count() == 1
    assert Observation.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_decision_hook_denies_wrong_project_before_records_or_queued_worker_task() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    payload = valid_hook_payload(
        other_project,
        team,
        event_type='decision',
        event_id='decision-wrong-project-event-1',
        idempotency_key='decision-wrong-project-idempotency-1',
        payload={'decision': 'Try unauthorized project'},
        observation={
            'type': 'decision',
            'title': 'Unauthorized project',
            'body': 'Decision event targets a denied project.',
            'files_read': [],
            'files_modified': [],
        },
    )

    response = APIClient().post('/v1/hooks/decision', payload, format='json', **auth_headers())

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_accepts_thin_hook_payload_without_normalized_observation() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    payload.pop('observation')

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 202
    observation = Observation.objects.get()

    assert observation.observation_type == 'post_tool_use'
    assert observation.title == 'post_tool_use: bash'
    assert observation.body == ''
    assert observation.files_read == []
    assert observation.files_modified == []


@pytest.mark.django_db
def test_post_tool_use_redacts_secrets_before_persisting_payload_or_observation() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    provider_secret = 'sk-test-secret123456789'
    payload = valid_hook_payload(
        project,
        team,
        payload={
            'tool_name': 'bash',
            'authorization': f'Bearer {RAW_KEY}',
            'tool_input': {
                'api_key': provider_secret,
                'command': f'echo {provider_secret}',
            },
            'tool_response': {
                'stdout': f'token={RAW_KEY}',
            },
        },
        observation={
            'type': 'tool_use',
            'title': 'bash printed a token',
            'body': f'output contained {provider_secret} and {RAW_KEY}',
            'files_read': [],
            'files_modified': [],
        },
    )

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 202
    raw_event = RawEventEnvelope.objects.get()
    observation = Observation.objects.get()

    persisted = f'{raw_event.payload} {observation.body} {observation.redaction_metadata}'
    assert provider_secret not in persisted
    assert RAW_KEY not in persisted
    assert raw_event.payload['authorization'] == '[REDACTED]'
    assert raw_event.payload['tool_input']['api_key'] == '[REDACTED]'
    assert '[REDACTED]' in raw_event.payload['tool_input']['command']
    assert '[REDACTED]' in raw_event.payload['tool_response']['stdout']
    assert '[REDACTED]' in observation.body
    assert observation.redaction_metadata == {'redacted': True}


@pytest.mark.django_db
def test_post_tool_use_uses_key_bound_team_when_request_omits_team_id() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    payload.pop('team_id')

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 202
    session = AgentSession.objects.get()
    raw_event = RawEventEnvelope.objects.get()
    observation = Observation.objects.get()

    assert session.team_id == team.id
    assert raw_event.team_id == team.id
    assert observation.team_id == team.id


@pytest.mark.django_db
def test_post_tool_use_replay_by_idempotency_key_returns_existing_rows(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    replay = {
        **payload,
        'event_id': 'event-2',
        'content_hash': 'hash-event-2',
        'observation': {**payload['observation'], 'title': 'should not create'},
    }

    with f_capture_on_commit(execute=True):
        first = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())
        second = client.post('/v1/hooks/post-tool-use', replay, format='json', **auth_headers())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()['duplicate'] is True
    assert second.json()['raw_event_id'] == first.json()['raw_event_id']
    assert RawEventEnvelope.objects.count() == 1
    assert Observation.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_post_tool_use_replay_by_session_event_id_returns_existing_rows(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    replay = {
        **payload,
        'idempotency_key': 'idem-2',
    }

    with f_capture_on_commit(execute=True):
        first = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())
        second = client.post('/v1/hooks/post-tool-use', replay, format='json', **auth_headers())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()['duplicate'] is True
    assert second.json()['raw_event_id'] == first.json()['raw_event_id']
    assert RawEventEnvelope.objects.count() == 1
    assert Observation.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_post_tool_use_replay_race_returns_existing_rows(m_monkeypatch: pytest.MonkeyPatch) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    first = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())
    original_find_duplicate = IngestHookEvent._find_duplicate
    calls = {'count': 0}

    def miss_once_then_load(
        service: IngestHookEvent,
        organization: Organization,
        project: Project,
        data: Any,
    ) -> RawEventEnvelope | None:
        calls['count'] += 1
        if calls['count'] == 1:
            return None

        return original_find_duplicate(service, organization, project, data)

    m_monkeypatch.setattr(IngestHookEvent, '_find_duplicate', miss_once_then_load)

    second = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()['duplicate'] is True
    assert second.json()['raw_event_id'] == first.json()['raw_event_id']
    assert RawEventEnvelope.objects.count() == 1
    assert Observation.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_post_tool_use_denies_cross_project_before_writes() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    client = APIClient()

    response = client.post(
        '/v1/hooks/post-tool-use',
        valid_hook_payload(other_project, team),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_non_object_payload() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team, payload=['not', 'an', 'object'])

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert 'payload' in response.json()
    assert RawEventEnvelope.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_oversized_nested_payload_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        payload={'tool_input': {'nested': 'x' * HOOK_PAYLOAD_MAX_BYTES}},
    )

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json()['payload']['code'] == ['hook_payload_too_large']
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_oversized_observation_body_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        observation={
            'type': 'tool_use',
            'title': 'bash completed',
            'body': 'x' * (HOOK_OBSERVATION_BODY_MAX_LENGTH + 1),
            'files_read': [],
            'files_modified': [],
        },
    )

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json()['observation']['body']['code'] == ['hook_observation_body_too_large']
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_too_many_or_too_long_observation_file_paths_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        observation={
            'type': 'tool_use',
            'title': 'bash completed',
            'body': 'pytest exited 0',
            'files_read': [f'apps/file-{index}.py' for index in range(HOOK_PATH_LIST_MAX_ITEMS + 1)],
            'files_modified': ['a' * (HOOK_PATH_MAX_LENGTH + 1)],
        },
    )

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json()['observation']['files_read']['code'] == ['hook_observation_files_read_too_many']
    assert response.json()['observation']['files_modified']['code'] == [
        'hook_observation_files_modified_path_too_long',
    ]
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_too_long_repository_path_fields_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        repository_url='https://example.test/' + ('z' * HOOK_PATH_MAX_LENGTH),
        repository_root='/' + ('x' * HOOK_PATH_MAX_LENGTH),
        cwd='/' + ('y' * HOOK_PATH_MAX_LENGTH),
    )

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json()['repository_url']['code'] == ['hook_repository_url_too_long']
    assert response.json()['repository_root']['code'] == ['hook_repository_root_too_long']
    assert response.json()['cwd']['code'] == ['hook_cwd_too_long']
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_post_tool_use_rejects_malformed_payload() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(project, team)
    payload.pop('content_hash')

    response = client.post('/v1/hooks/post-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert 'content_hash' in response.json()
    assert RawEventEnvelope.objects.count() == 0


@pytest.mark.django_db
def test_session_end_marks_session_ended_and_writes_durable_event(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        event_id='event-stop-1',
        idempotency_key='idem-stop-1',
        event_type='session_end',
        content_hash='hash-stop-1',
        request_id='request-stop-1',
        observation={
            'type': 'session_end',
            'title': 'Session ended',
            'body': 'Agent stopped with unresolved work.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = client.post('/v1/hooks/session-end', payload, format='json', **auth_headers())

    assert response.status_code == 202
    session = AgentSession.objects.get()
    raw_event = RawEventEnvelope.objects.get()
    observation = Observation.objects.get()

    assert session.status == SessionStatus.ENDED
    assert session.ended_at is not None
    assert raw_event.event_type == 'session_end'
    assert observation.observation_type == 'session_end'
    assert response.json()['agent_session_id'] == str(session.id)
    outbox_tasks = {row.task_name: row for row in CeleryOutbox.objects.all()}
    assert set(outbox_tasks) == {
        'engram.memory.process_observation_recorded',
        'engram.memory.distill_session',
    }
    process_task = outbox_tasks['engram.memory.process_observation_recorded']
    assert process_task.args == [response.json()['observation_id']]
    assert process_task.kwargs == {}
    distill_task = outbox_tasks['engram.memory.distill_session']
    assert distill_task.args == [str(session.id)]
    assert distill_task.kwargs == {}


@pytest.mark.django_db
def test_hook_dry_run_denied_when_organization_suspended() -> None:
    from engram.core.models import OrganizationStatus

    organization, team, project, _owner, _api_key = create_project_scope()
    organization.status = OrganizationStatus.SUSPENDED
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.post(
        '/v1/hooks/dry-run',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'agent_runtime': 'codex',
            'agent_version': '0.1.0',
            'request_id': 'dry-run-suspended',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'organization_suspended'


@pytest.mark.django_db
def test_pre_tool_use_ingests_raw_event_observation_source_and_queues_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()
    payload = valid_hook_payload(
        project,
        team,
        event_type='pre_tool_use',
        event_id='pre-tool-use-event-1',
        idempotency_key='pre-tool-use-idempotency-1',
        observation={
            'type': 'pre_tool_use',
            'title': 'bash about to run',
            'body': 'about to run pytest',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = client.post('/v1/hooks/pre-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 202
    body = response.json()
    assert body['status'] == 'accepted'
    assert body['duplicate'] is False
    raw_event = RawEventEnvelope.objects.get()
    observation = Observation.objects.get()
    assert raw_event.event_type == 'pre_tool_use'
    assert observation.observation_type == 'pre_tool_use'
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]


@pytest.mark.django_db
def test_pre_tool_use_rejects_mismatched_event_type_before_writes() -> None:
    _organization, team, project, _owner, _api_key = create_project_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='post_tool_use',
        event_id='pre-tool-use-mismatch-event-1',
        idempotency_key='pre-tool-use-mismatch-idempotency-1',
    )

    response = APIClient().post('/v1/hooks/pre-tool-use', payload, format='json', **auth_headers())

    assert response.status_code == 400
    assert response.json() == {'event_type': ['Expected pre_tool_use.']}
    assert RawEventEnvelope.objects.count() == 0


@pytest.mark.django_db
def test_session_start_hook_with_model_id_persists_it_on_agent_session(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    organization, project, team, raw_key = create_hook_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='session_start',
        event_id='session-start-model-id-event-1',
        idempotency_key='session-start-model-id-idempotency-1',
        payload={'trigger': 'startup', 'cwd': '/workspace/engram', 'model_id': 'claude-sonnet-4-5'},
        observation={
            'type': 'session_start',
            'title': 'Session started',
            'body': 'Agent session started for backend work.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/session-start', payload, format='json', **auth_headers(raw_key))

    assert response.status_code == 202
    session = AgentSession.objects.get()
    assert session.model_id == 'claude-sonnet-4-5'


@pytest.mark.django_db
def test_session_start_hook_without_model_id_keeps_it_blank(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    organization, project, team, raw_key = create_hook_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='session_start',
        event_id='session-start-no-model-id-event-1',
        idempotency_key='session-start-no-model-id-idempotency-1',
        payload={'trigger': 'startup', 'cwd': '/workspace/engram'},
        observation={
            'type': 'session_start',
            'title': 'Session started',
            'body': 'Agent session started for backend work.',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/session-start', payload, format='json', **auth_headers(raw_key))

    assert response.status_code == 202
    session = AgentSession.objects.get()
    assert session.model_id == ''


@pytest.mark.django_db
def test_user_prompt_submit_hook_persists_event_and_queues_worker_task(
    f_capture_on_commit: DjangoCaptureOnCommitCallbacks,
) -> None:
    organization, project, team, raw_key = create_hook_scope()
    payload = valid_hook_payload(
        project,
        team,
        event_type='user_prompt_submit',
        event_id='user-prompt-submit-event-1',
        idempotency_key='user-prompt-submit-idempotency-1',
        payload={'prompt': 'how does authorization work?'},
        observation={
            'type': 'user_prompt_submit',
            'title': 'User prompt submitted',
            'body': 'how does authorization work?',
            'files_read': [],
            'files_modified': [],
        },
    )

    with f_capture_on_commit(execute=True):
        response = APIClient().post('/v1/hooks/user-prompt-submit', payload, format='json', **auth_headers(raw_key))

    assert response.status_code == 202
    body = response.json()
    assert RawEventEnvelope.objects.get().event_type == 'user_prompt_submit'
    assert Observation.objects.get().observation_type == 'user_prompt_submit'
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == 'engram.memory.process_observation_recorded'
    assert queued.args == [body['observation_id']]
    assert queued.kwargs == {}
