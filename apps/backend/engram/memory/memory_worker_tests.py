from __future__ import annotations

import hashlib
import io
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
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
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.visibility_scope == VisibilityScope.PROJECT
    assert candidate.confidence == Decimal('0.500')
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

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    memory = Memory.objects.get()
    version = MemoryVersion.objects.get()
    document = RetrievalDocument.objects.get()
    provider_call = ProviderCallRecord.objects.get()

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

    execute_worker(observation)
    first_document = RetrievalDocument.objects.get()
    first_vector = list(first_document.embedding_vector)
    first_reference = first_document.embedding_reference

    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=first_document.memory_version_id))

    second_document = RetrievalDocument.objects.get()
    assert second_document.embedding_vector == first_vector
    assert second_document.embedding_reference == first_reference
    embedding_calls = ProviderCallRecord.objects.filter(task_type='embedding')
    assert embedding_calls.count() == 1


@pytest.mark.django_db
def test_index_memory_version_skips_embedding_without_policy() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)

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

    execute_worker(observation)

    document = RetrievalDocument.objects.get()
    assert document.embedding_vector == []
    assert document.embedding_reference == ''


@pytest.mark.django_db
def test_observation_recorded_worker_dedupes_memory_for_same_content_across_sessions() -> None:
    organization, team, project, session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
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
