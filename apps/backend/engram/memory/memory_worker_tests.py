from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid
from decimal import Decimal
from typing import Any

import pytest
import structlog
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    OrganizationSettings,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Runtime,
    Team,
    VisibilityScope,
)
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    digest_prompt,
    digest_system_prompt,
    distillation_system_prompt,
    memory_candidate_content_hash,
    provider_prompt,
)
from engram.memory.tasks import process_observation_recorded
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope

RAW_KEY = 'egk_test_memory_worker_0123456789abcdefghijklmnopqrstuvwxyz'
RAW_PROVIDER_SECRET = 'sk-test_memory_worker_secret_1234567890abcdef'
RAW_SLACK_TOKEN = 'xoxb-123456789012-123456789012-fakeSlackWorkerToken'


def expected_generated_title(observation: Observation) -> str:
    digest = hashlib.sha256(provider_prompt(observation).encode()).hexdigest()[:12]

    return f'Provider-generated memory {digest}'


def expected_generated_body(observation: Observation) -> str:
    digest = hashlib.sha256(provider_prompt(observation).encode()).hexdigest()[:12]

    return f'Provider-generated candidate body {digest}'


def create_observation_recorded_scope(
    *,
    suffix: str = '1',
) -> tuple[Organization, Team, Project, AgentSession, RawEventEnvelope, Observation]:
    slug_suffix = '' if suffix == '1' else f'-{suffix}'
    organization = Organization.objects.create(name=f'Engram {suffix}', slug=f'engram{slug_suffix}')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'codex-local-{suffix}',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
        branch='master',
        cwd='/workspace/engram',
    )
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        source_adapter=Runtime.CODEX,
        client_event_id=f'event-{suffix}',
        idempotency_key=f'idem-{suffix}',
        content_hash=f'hash-event-{suffix}',
        runtime=Runtime.CODEX,
        payload_schema_version='v1',
        payload={
            'tool_name': 'bash',
            'authorization': f'Bearer {RAW_KEY}',
            'tool_response': {'stdout': RAW_KEY},
        },
        headers={},
        request_id=f'request-event-{suffix}',
        actor_type='api_key',
        actor_id=f'api-key-{suffix}',
    )
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        raw_event=raw_event,
        observation_type='tool_use',
        title='pytest failure fixed',
        body='pytest failed on missing memory worker and now exits 0',
        files_read=['apps/backend/engram/core/models.py'],
        files_modified=['apps/backend/engram/memory/services.py'],
        content_hash=f'hash-observation-{suffix}',
        redaction_metadata={'redacted': True},
        source_metadata={'event_type': 'post_tool_use'},
        observed_at=timezone.now(),
    )
    ObservationSource.objects.create(
        organization=organization,
        project=project,
        observation=observation,
        raw_event=raw_event,
        source_type='hook_event',
        source_id=f'event-{suffix}',
        citation=f'event-{suffix}',
        metadata={'event_type': 'post_tool_use'},
    )

    return organization, team, project, session, raw_event, observation


def execute_worker(observation: Observation) -> Any:
    return ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(observation_id=observation.id, worker_id='test-worker'),
    )


def enable_auto_promote(organization: Organization, threshold: str = '0.500') -> None:
    OrganizationSettings.objects.update_or_create(
        organization=organization,
        defaults={'distillation_auto_approve_threshold': Decimal(threshold)},
    )


def create_generation_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
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

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=3,
    )


def create_embedding_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
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


def create_sibling_observation(base: Observation, *, suffix: str) -> Observation:
    raw_event = RawEventEnvelope.objects.create(
        organization=base.organization,
        project=base.project,
        team=base.team,
        agent=base.agent,
        session=base.session,
        event_type='post_tool_use',
        source_adapter=Runtime.CODEX,
        client_event_id=f'event-{suffix}',
        idempotency_key=f'idem-{suffix}',
        content_hash=f'hash-event-{suffix}',
        runtime=Runtime.CODEX,
        payload_schema_version='v1',
        payload={'tool_name': 'bash'},
        headers={},
        request_id=f'request-event-{suffix}',
        actor_type='api_key',
        actor_id=f'api-key-{suffix}',
    )

    return Observation.objects.create(
        organization=base.organization,
        project=base.project,
        team=base.team,
        agent=base.agent,
        session=base.session,
        raw_event=raw_event,
        observation_type='tool_use',
        title=base.title,
        body=base.body,
        files_read=base.files_read,
        files_modified=base.files_modified,
        content_hash=f'hash-observation-{suffix}',
        redaction_metadata={'redacted': True},
        source_metadata={'event_type': 'post_tool_use'},
        observed_at=timezone.now(),
    )


def seed_provenanced_candidate(
    observation: Observation,
    *,
    title: str,
    body: str,
    confidence: str = '0.900',
) -> MemoryCandidate:
    return MemoryCandidate.objects.create(
        organization=observation.organization,
        project=observation.project,
        team=observation.team,
        source_observation=observation,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[{'observation_id': str(observation.id), 'provider_call_id': f'seed-{observation.id}'}],
        content_hash=memory_candidate_content_hash(observation),
        confidence=Decimal(confidence),
    )


def create_memory_candidate(observation: Observation) -> MemoryCandidate:
    return MemoryCandidate.objects.create(
        organization=observation.organization,
        project=observation.project,
        team=observation.team,
        source_observation=observation,
        title=observation.title,
        body=observation.body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[
            {
                'observation_id': str(observation.id),
                'raw_event_id': str(observation.raw_event_id),
                'event_type': 'post_tool_use',
                'title': observation.title,
                'files_read': observation.files_read,
                'files_modified': observation.files_modified,
            },
        ],
        content_hash=memory_candidate_content_hash(observation),
        confidence=Decimal('0.500'),
    )


@pytest.mark.django_db
def test_observation_recorded_worker_creates_candidate_with_redacted_evidence() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    policy = create_generation_policy(organization, team, project)

    result = execute_worker(observation)

    assert result.duplicate is False
    assert result.held_for_review is True
    assert result.memory is None
    candidate = MemoryCandidate.objects.get()
    provider_call = ProviderCallRecord.objects.get()

    assert result.candidate.id == candidate.id
    assert candidate.organization_id == project.organization_id
    assert candidate.project_id == project.id
    assert candidate.team_id == team.id
    assert candidate.source_observation_id == observation.id
    assert candidate.title == expected_generated_title(observation)
    assert candidate.body == expected_generated_body(observation)
    assert candidate.title != observation.title
    assert candidate.body != observation.body
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.visibility_scope == VisibilityScope.PROJECT
    assert candidate.confidence == Decimal('0.600')
    assert Memory.objects.count() == 0
    held_audit = AuditEvent.objects.get(event_type='MemoryCandidateHeldForReview')
    assert held_audit.actor_type == 'system'
    assert held_audit.target_id == str(candidate.id)
    assert held_audit.metadata['confidence'] == '0.600'
    assert held_audit.metadata['threshold'] == '0.800'
    assert candidate.content_hash == memory_candidate_content_hash(observation)
    assert candidate.evidence == [
        {
            'observation_id': str(observation.id),
            'raw_event_id': str(raw_event.id),
            'event_type': 'post_tool_use',
            'title': expected_generated_title(observation),
            'files_read': ['apps/backend/engram/core/models.py'],
            'files_modified': ['apps/backend/engram/memory/services.py'],
            'provider_call_id': str(provider_call.id),
            'provider': 'openai',
            'model': 'gpt-4.1-mini',
            'policy_id': str(policy.id),
            'policy_version': 3,
            'task_type': 'generation',
            'redaction_state': 'clean',
        },
    ]
    assert provider_call.request_id == f'memory-worker:{observation.id}:generation'
    assert RAW_KEY not in str(candidate.evidence)


@pytest.mark.django_db
def test_observation_recorded_worker_auto_promotes_memory_and_indexes_retrieval() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    policy = create_generation_policy(organization, team, project)
    enable_auto_promote(organization)

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    memory = Memory.objects.get()
    version = MemoryVersion.objects.get()
    document = RetrievalDocument.objects.get()
    provider_call = ProviderCallRecord.objects.get()

    assert result.duplicate is False
    assert result.curated_decision == 'promoted'
    assert result.candidate.id == candidate.id
    assert result.memory.id == memory.id
    assert result.memory_version.id == version.id
    assert result.retrieval_document.id == document.id
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id == memory.id
    assert memory.organization_id == project.organization_id
    assert memory.project_id == project.id
    assert memory.team_id == team.id
    assert memory.status == MemoryStatus.APPROVED
    assert memory.title == candidate.title
    assert memory.body == candidate.body
    assert observation.body not in memory.body
    assert memory.metadata['provider_call_id'] == str(provider_call.id)
    assert memory.metadata['provider'] == 'openai'
    assert memory.metadata['model'] == 'gpt-4.1-mini'
    assert memory.metadata['policy_id'] == str(policy.id)
    assert memory.metadata['policy_version'] == 3
    assert memory.metadata['task_type'] == 'generation'
    assert memory.metadata['redaction_state'] == 'clean'
    assert version.memory_id == memory.id
    assert version.source_observation_id == observation.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.file_paths == observation.files_read + observation.files_modified
    assert RAW_KEY not in f'{candidate.evidence} {memory.title} {memory.body} {document.full_text}'


@pytest.mark.django_db
def test_observation_recorded_worker_redacts_candidate_content_and_evidence() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)
    observation.title = f'Bearer {RAW_KEY}'
    observation.body = f'command printed {RAW_KEY} and {RAW_SLACK_TOKEN}'
    observation.files_read = [f'apps/backend/{RAW_SLACK_TOKEN}.txt']
    observation.files_modified = [f'apps/backend/{RAW_KEY}-modified.py']
    observation.save(update_fields=['title', 'body', 'files_read', 'files_modified', 'updated_at'])

    execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    memory = Memory.objects.get()
    document = RetrievalDocument.objects.get()
    provider_call = ProviderCallRecord.objects.get()
    persisted = (
        f'{candidate.title} {candidate.body} {candidate.evidence} {memory.body} '
        f'{memory.metadata} {document.file_paths} {provider_call.__dict__}'
    )

    assert RAW_KEY not in persisted
    assert RAW_SLACK_TOKEN not in persisted
    assert '[REDACTED]' in str(candidate.evidence)
    assert '[REDACTED]' in str(memory.metadata['file_paths'])
    assert '[REDACTED]' in str(document.file_paths)
    assert provider_call.redaction_state == 'redacted'


@pytest.mark.django_db
def test_provider_prompt_masks_token_shaped_values_before_provider_boundary() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    observation.title = f'Bearer {RAW_KEY}'
    observation.body = f'command printed {RAW_KEY} and {RAW_SLACK_TOKEN}'
    observation.files_read = [f'apps/backend/{RAW_KEY}.txt']
    observation.files_modified = [f'apps/backend/{RAW_SLACK_TOKEN}-modified.py']
    observation.source_metadata = {'providerApiKey': RAW_PROVIDER_SECRET}
    observation.save(
        update_fields=['title', 'body', 'files_read', 'files_modified', 'source_metadata', 'updated_at'],
    )

    prompt = provider_prompt(observation)

    assert RAW_KEY not in prompt
    assert RAW_PROVIDER_SECRET not in prompt
    assert RAW_SLACK_TOKEN not in prompt
    assert '[REDACTED]' in prompt


@pytest.mark.django_db
def test_observation_recorded_worker_is_idempotent_for_duplicate_delivery() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)
    first = execute_worker(observation)

    second = execute_worker(observation)

    assert second.duplicate is True
    assert second.candidate.id == first.candidate.id
    assert second.memory.id == first.memory.id
    assert second.memory_version.id == first.memory_version.id
    assert second.retrieval_document.id == first.retrieval_document.id
    assert MemoryCandidate.objects.count() == 1
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1
    assert ProviderCallRecord.objects.count() == 1


@pytest.mark.django_db
def test_observation_recorded_worker_reuses_existing_candidate() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        source_observation=observation,
        title=observation.title,
        body=observation.body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[
            {
                'observation_id': str(observation.id),
                'raw_event_id': str(observation.raw_event_id),
                'event_type': 'post_tool_use',
                'title': observation.title,
                'files_read': observation.files_read,
                'files_modified': observation.files_modified,
            },
        ],
        content_hash=memory_candidate_content_hash(observation),
        confidence=Decimal('0.500'),
    )

    result = execute_worker(observation)

    candidate.refresh_from_db()
    provider_call = ProviderCallRecord.objects.get()

    assert result.duplicate is True
    assert result.candidate.id == candidate.id
    assert result.memory.title == expected_generated_title(observation)
    assert result.memory.body == expected_generated_body(observation)
    assert result.memory.metadata['provider_call_id'] == str(provider_call.id)
    assert candidate.title == expected_generated_title(observation)
    assert candidate.body == expected_generated_body(observation)
    assert candidate.evidence[0]['provider_call_id'] == str(provider_call.id)
    assert MemoryCandidate.objects.count() == 1
    assert ProviderCallRecord.objects.count() == 1

    second = execute_worker(observation)

    assert second.duplicate is True
    assert second.memory.id == result.memory.id
    assert ProviderCallRecord.objects.count() == 1


@pytest.mark.django_db
def test_observation_recorded_worker_raises_for_missing_observation() -> None:
    missing_observation_id = uuid.uuid4()

    with pytest.raises(MemoryWorkerError, match='observation not found'):
        ProcessObservationRecorded().execute(
            MemoryCandidateWorkerInput(observation_id=missing_observation_id, worker_id='test-worker'),
        )

    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_observation_recorded_worker_missing_generation_policy_fails_before_writes() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()

    with pytest.raises(MemoryWorkerError, match='Model policy was not found'):
        execute_worker(observation)

    assert MemoryCandidate.objects.count() == 0
    assert Memory.objects.count() == 0
    assert MemoryVersion.objects.count() == 0
    assert RetrievalDocument.objects.count() == 0
    assert ProviderCallRecord.objects.count() == 0


@pytest.mark.django_db
def test_observation_recorded_worker_missing_generation_policy_fails_before_promoting_existing_candidate() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)

    with pytest.raises(MemoryWorkerError, match='Model policy was not found'):
        execute_worker(observation)

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert MemoryCandidate.objects.count() == 1
    assert Memory.objects.count() == 0
    assert MemoryVersion.objects.count() == 0
    assert RetrievalDocument.objects.count() == 0
    assert ProviderCallRecord.objects.count() == 0


@pytest.mark.django_db
def test_process_observation_recorded_task_delegates_by_observation_id() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)

    memory_id = process_observation_recorded.run(str(observation.id))

    memory = Memory.objects.get()

    assert memory_id == str(memory.id)
    assert RetrievalDocument.objects.get().memory_id == memory.id


@pytest.mark.django_db
@pytest.mark.parametrize('malformed_observation_id', ['not-a-uuid', None, [], {}, b'abc'])
def test_process_observation_recorded_task_rejects_malformed_observation_id(
    malformed_observation_id: object,
) -> None:
    with pytest.raises(MemoryWorkerError, match='malformed observation id'):
        process_observation_recorded.run(malformed_observation_id)

    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_promote_memory_candidate_lock_query_locks_candidate_row_without_related_joins() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)

    with CaptureQueriesContext(connection) as queries:
        locked = PromoteMemoryCandidate()._lock_candidate(candidate.id)

    lock_sql = next(
        query['sql'] for query in queries.captured_queries if 'core_memorycandidate' in query['sql'].lower()
    )

    assert locked.id == candidate.id
    assert 'JOIN' not in lock_sql.upper()


@pytest.mark.django_db
def test_promote_memory_candidate_creates_memory_version_and_retrieval_document() -> None:
    _organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)

    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    memory = Memory.objects.get()
    version = MemoryVersion.objects.get()
    document = RetrievalDocument.objects.get()

    assert result.duplicate is False
    assert result.candidate.id == candidate.id
    assert result.memory.id == memory.id
    assert result.memory_version.id == version.id
    assert result.retrieval_document.id == document.id
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id == memory.id
    assert memory.organization_id == project.organization_id
    assert memory.project_id == project.id
    assert memory.team_id == team.id
    assert memory.title == candidate.title
    assert memory.body == candidate.body
    assert memory.status == MemoryStatus.APPROVED
    assert memory.visibility_scope == candidate.visibility_scope
    assert memory.confidence == candidate.confidence
    assert memory.metadata == {
        'source': 'memory_candidate',
        'memory_candidate_id': str(candidate.id),
        'evidence': candidate.evidence,
        'file_paths': observation.files_read + observation.files_modified,
        'captured_by': {
            'agent_runtime': observation.agent.runtime,
            'agent_external_id': observation.agent.external_id,
        },
    }
    assert version.memory_id == memory.id
    assert version.version == 1
    assert version.body == candidate.body
    assert version.content_hash == candidate.content_hash
    assert version.source_observation_id == observation.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.file_paths == observation.files_read + observation.files_modified


@pytest.mark.django_db
def test_promote_memory_candidate_is_idempotent() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)
    first = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    second = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert second.duplicate is True
    assert second.memory.id == first.memory.id
    assert second.memory_version.id == first.memory_version.id
    assert second.retrieval_document.id == first.retrieval_document.id
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1


@pytest.mark.django_db
def test_promote_memory_candidate_command_outputs_json_ids() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)
    stdout = io.StringIO()

    call_command('engram_promote_memory_candidate', str(candidate.id), '--json', stdout=stdout)

    body = json.loads(stdout.getvalue())
    candidate.refresh_from_db()
    memory = candidate.promoted_memory
    version = MemoryVersion.objects.get(memory=memory)
    document = RetrievalDocument.objects.get(memory=memory)

    assert body == {
        'candidate_id': str(candidate.id),
        'memory_id': str(memory.id),
        'memory_version_id': str(version.id),
        'retrieval_document_id': str(document.id),
        'duplicate': False,
    }


@pytest.mark.django_db
def test_promote_memory_candidate_command_is_idempotent_for_duplicate_candidate() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)
    first_stdout = io.StringIO()
    second_stdout = io.StringIO()

    call_command('engram_promote_memory_candidate', str(candidate.id), '--json', stdout=first_stdout)
    call_command('engram_promote_memory_candidate', str(candidate.id), '--json', stdout=second_stdout)

    first = json.loads(first_stdout.getvalue())
    second = json.loads(second_stdout.getvalue())

    assert first['duplicate'] is False
    assert second['duplicate'] is True
    assert second['candidate_id'] == first['candidate_id']
    assert second['memory_id'] == first['memory_id']
    assert second['memory_version_id'] == first['memory_version_id']
    assert second['retrieval_document_id'] == first['retrieval_document_id']
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1


@pytest.mark.django_db
def test_promote_memory_candidate_command_accepts_candidate_id_option() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)
    stdout = io.StringIO()

    call_command('engram_promote_memory_candidate', '--candidate-id', str(candidate.id), '--json', stdout=stdout)

    body = json.loads(stdout.getvalue())

    assert body['candidate_id'] == str(candidate.id)
    assert body['duplicate'] is False


@pytest.mark.django_db
def test_promote_memory_candidate_command_can_promote_latest_project_candidate() -> None:
    _organization, _team, project, _session, _raw_event, first_observation = create_observation_recorded_scope()
    first_candidate = create_memory_candidate(first_observation)
    _other_org, _other_team, other_project, _other_session, _other_raw, other_observation = (
        create_observation_recorded_scope(suffix='2')
    )
    other_candidate = create_memory_candidate(other_observation)
    stdout = io.StringIO()

    call_command(
        'engram_promote_memory_candidate',
        '--project-id',
        str(project.id),
        '--latest',
        '--json',
        stdout=stdout,
    )

    body = json.loads(stdout.getvalue())

    assert body['candidate_id'] == str(first_candidate.id)
    assert body['duplicate'] is False
    first_candidate.refresh_from_db()
    other_candidate.refresh_from_db()
    assert first_candidate.status == CandidateStatus.PROMOTED
    assert other_candidate.status == CandidateStatus.PROPOSED
    assert other_project.id != project.id


@pytest.mark.django_db
def test_index_memory_version_writes_embedding_vector_and_reference() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    enable_auto_promote(organization)

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert len(document.embedding_vector) == 64
    assert document.embedding_reference.startswith('provider:')
    assert document.embedding_vector == document.embedding_vector


@pytest.mark.django_db
def test_index_memory_version_embedding_is_idempotent_across_reindex() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    enable_auto_promote(organization)

    execute_worker(observation)
    first_document = RetrievalDocument.objects.get()
    first_vector = list(first_document.embedding_vector)
    first_reference = first_document.embedding_reference

    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=first_document.memory_version_id))

    second_document = RetrievalDocument.objects.get()
    assert second_document.embedding_vector == first_vector
    assert second_document.embedding_reference == first_reference
    indexer_calls = ProviderCallRecord.objects.filter(
        task_type='embedding',
        request_id=f'memory-indexer:{first_document.memory_version_id}:embedding',
    )
    assert indexer_calls.count() == 1


@pytest.mark.django_db
def test_index_memory_version_skips_embedding_without_policy() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert document.embedding_vector == []
    assert document.embedding_reference == ''


@pytest.mark.django_db
def test_index_memory_version_skips_embedding_when_secret_disabled() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    embedding_policy = create_embedding_policy(organization, team, project)
    embedding_policy.secret.active = False
    embedding_policy.secret.save(update_fields=['active'])
    enable_auto_promote(organization)

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert document.embedding_vector == []
    assert document.embedding_reference == ''


@pytest.mark.django_db
def test_observation_recorded_worker_dedupes_memory_for_same_content_across_sessions() -> None:
    organization, team, project, session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    enable_auto_promote(organization)
    execute_worker(observation)

    second_session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=session.agent,
        external_session_id='session-dedup',
        runtime=Runtime.CODEX,
    )
    second_raw = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=session.agent,
        session=second_session,
        event_type='post_tool_use',
        source_adapter=Runtime.CODEX,
        client_event_id='event-dedup',
        idempotency_key='idem-dedup',
        content_hash='hash-event-dedup',
        runtime=Runtime.CODEX,
        payload_schema_version='v1',
        payload={'tool_name': 'bash'},
        headers={},
        request_id='request-event-dedup',
        actor_type='api_key',
        actor_id='api-key-dedup',
    )
    second_observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=session.agent,
        session=second_session,
        raw_event=second_raw,
        observation_type='tool_use',
        title=observation.title,
        body=observation.body,
        files_read=observation.files_read,
        files_modified=observation.files_modified,
        content_hash=observation.content_hash,
        redaction_metadata={'redacted': True},
        source_metadata={'event_type': 'post_tool_use'},
        observed_at=timezone.now(),
    )

    result = execute_worker(second_observation)

    assert result.duplicate is True
    assert Memory.objects.count() == 1
    assert MemoryCandidate.objects.count() == 1
    assert MemoryVersion.objects.count() == 1


@pytest.mark.django_db
def test_observation_recorded_worker_auto_rejects_low_signal_candidate() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)
    candidate = seed_provenanced_candidate(observation, title='noise', body='noise')

    result = execute_worker(observation)

    candidate.refresh_from_db()
    assert result.curated_decision == 'rejected'
    assert result.memory is None
    assert candidate.status == CandidateStatus.REJECTED
    assert Memory.objects.count() == 0
    assert RetrievalDocument.objects.count() == 0
    reject_audit = AuditEvent.objects.get(event_type='MemoryAutoRejected')
    assert reject_audit.actor_type == 'system'
    assert reject_audit.target_id == str(candidate.id)


@pytest.mark.django_db
def test_observation_recorded_worker_supersedes_semantic_near_duplicate() -> None:
    organization, team, project, _session, _raw_event, first_observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    enable_auto_promote(organization)
    shared_title = 'Retrieval ranking pipeline'
    shared_body = 'The retrieval pipeline ranks documents by cosine similarity over embeddings.'
    seed_provenanced_candidate(first_observation, title=shared_title, body=shared_body)
    first_result = execute_worker(first_observation)

    second_observation = create_sibling_observation(first_observation, suffix='neardup')
    seed_provenanced_candidate(second_observation, title=shared_title, body=shared_body)

    second_result = execute_worker(second_observation)

    first_result.memory.refresh_from_db()
    assert first_result.curated_decision == 'promoted'
    assert second_result.curated_decision == 'superseded'
    assert second_result.memory.id != first_result.memory.id
    assert first_result.memory.stale is True
    link = MemoryLink.objects.get(link_type=LinkType.SUPERSEDED_BY)
    assert link.memory_id == first_result.memory.id
    assert link.target == str(second_result.memory.id)
    assert AuditEvent.objects.filter(event_type='MemorySuperseded').count() == 1
    assert Memory.objects.filter(stale=False).count() == 1


@pytest.mark.django_db
def test_correlation_id_from_raw_event_propagates_to_provider_call_trace_id() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    raw_event.correlation_id = 'originating-corr-id-abc123'
    raw_event.save(update_fields=['correlation_id', 'updated_at'])

    execute_worker(observation)

    provider_call = ProviderCallRecord.objects.get()
    assert provider_call.trace_id == 'originating-corr-id-abc123'


@pytest.mark.django_db
def test_correlation_id_falls_back_to_request_id_when_correlation_id_empty() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    raw_event.correlation_id = ''
    raw_event.request_id = 'originating-request-id-xyz'
    raw_event.save(update_fields=['correlation_id', 'request_id', 'updated_at'])

    execute_worker(observation)

    provider_call = ProviderCallRecord.objects.get()
    assert provider_call.trace_id == 'originating-request-id-xyz'


@pytest.mark.django_db
def test_correlation_id_falls_back_to_observation_id_when_no_raw_event() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observation.raw_event = None
    observation.save(update_fields=['raw_event', 'updated_at'])

    execute_worker(observation)

    provider_call = ProviderCallRecord.objects.get()
    assert provider_call.trace_id == str(observation.id)


@pytest.mark.django_db
def test_process_observation_recorded_binds_correlation_id_to_structlog_context() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    raw_event.correlation_id = 'ctx-bind-corr-id-test'
    raw_event.save(update_fields=['correlation_id', 'updated_at'])

    structlog.contextvars.clear_contextvars()
    execute_worker(observation)

    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get('correlation_id') == 'ctx-bind-corr-id-test'
    assert ctx.get('observation_id') == str(observation.id)

    structlog.contextvars.clear_contextvars()


@pytest.mark.django_db
def test_process_observation_recorded_task_clears_context_before_execution() -> None:
    organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    raw_event.correlation_id = 'task-corr-id'
    raw_event.save(update_fields=['correlation_id', 'updated_at'])

    structlog.contextvars.bind_contextvars(stale_key='stale_value')

    process_observation_recorded.run(str(observation.id))

    ctx = structlog.contextvars.get_contextvars()
    assert 'stale_key' not in ctx
    assert 'correlation_id' not in ctx


def test_distillation_system_prompt_contains_instructions() -> None:
    prompt = distillation_system_prompt()

    assert 'Title' in prompt
    assert 'Body' in prompt
    assert 'verbatim' in prompt
    assert 'invent' in prompt


def test_distillation_system_prompt_is_runtime_neutral() -> None:
    prompt = distillation_system_prompt()

    for brand in ('Claude', 'Codex', 'claude-mem', 'OpenAI', 'GPT', 'Anthropic'):
        assert brand not in prompt


@pytest.mark.django_db
def test_provider_prompt_preserves_exact_file_path_identifier() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    observation.files_read = ['apps/backend/engram/core/models.py']
    observation.files_modified = ['apps/backend/engram/memory/services.py']
    observation.save(update_fields=['files_read', 'files_modified', 'updated_at'])

    prompt = provider_prompt(observation)

    assert 'apps/backend/engram/core/models.py' in prompt
    assert 'apps/backend/engram/memory/services.py' in prompt


def test_digest_system_prompt_contains_instructions() -> None:
    prompt = digest_system_prompt()

    assert 'Title' in prompt
    assert 'Body' in prompt
    assert 'de-duplicate' in prompt
    assert 'invent' in prompt


def test_digest_system_prompt_is_runtime_neutral() -> None:
    prompt = digest_system_prompt()

    for brand in ('Claude', 'Codex', 'claude-mem', 'OpenAI', 'GPT', 'Anthropic'):
        assert brand not in prompt


@pytest.mark.django_db
def test_digest_prompt_lists_source_titles_and_bodies() -> None:
    organization, team, project, _session, _raw_event, _observation = create_observation_recorded_scope()
    source_a = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Fix missing migration',
        body='Added 0042_user_schema migration for NOT NULL column',
        status='approved',
        visibility_scope=VisibilityScope.PROJECT,
    )
    source_b = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Update CI cache key',
        body='Changed poetry.lock hash in .github/workflows/ci.yml',
        status='approved',
        visibility_scope=VisibilityScope.PROJECT,
    )

    prompt = digest_prompt((source_a, source_b))

    assert 'Fix missing migration' in prompt
    assert 'Added 0042_user_schema migration' in prompt
    assert 'Update CI cache key' in prompt
    assert 'Changed poetry.lock hash' in prompt


@pytest.mark.django_db
def test_rich_observation_auto_promotes_at_default_threshold() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observation.observation_type = 'decision'
    observation.facts = ['use postgres for reliability']
    observation.narrative = 'We decided to use postgres.'
    observation.concepts = ['database']
    observation.save(update_fields=['observation_type', 'facts', 'narrative', 'concepts', 'updated_at'])

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    assert candidate.confidence == Decimal('0.950')
    assert result.held_for_review is False
    assert result.curated_decision is not None
    assert result.memory is not None


@pytest.mark.django_db
def test_thin_observation_held_at_default_threshold() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observation.files_read = []
    observation.files_modified = []
    observation.save(update_fields=['files_read', 'files_modified', 'updated_at'])

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    assert candidate.confidence == Decimal('0.500')
    assert result.held_for_review is True
    assert result.memory is None
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db(transaction=True)
def test_observation_recorded_worker_concurrent_duplicate_delivery_creates_exactly_one() -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires real row locking on postgres')
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization)
    observation_id = observation.id
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                ProcessObservationRecorded().execute(
                    MemoryCandidateWorkerInput(observation_id=observation_id, worker_id='race-worker'),
                ),
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for started in threads:
        started.start()
    for finished in threads:
        finished.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert MemoryCandidate.objects.count() == 1
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_observation_recorded_worker_concurrent_duplicate_delivery_with_embedding_creates_exactly_one() -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires real row locking on postgres')
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    enable_auto_promote(organization)
    observation_id = observation.id
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                ProcessObservationRecorded().execute(
                    MemoryCandidateWorkerInput(observation_id=observation_id, worker_id='race-worker'),
                ),
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for started in threads:
        started.start()
    for finished in threads:
        finished.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert MemoryCandidate.objects.count() == 1
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0
