from __future__ import annotations

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
)
from engram.memory.tasks import process_observation_recorded

RAW_KEY = 'egk_test_memory_worker_0123456789abcdefghijklmnopqrstuvwxyz'


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
    _organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()

    result = execute_worker(observation)

    assert result.duplicate is False
    candidate = MemoryCandidate.objects.get()

    assert result.candidate.id == candidate.id
    assert candidate.organization_id == project.organization_id
    assert candidate.project_id == project.id
    assert candidate.team_id == team.id
    assert candidate.source_observation_id == observation.id
    assert candidate.title == 'pytest failure fixed'
    assert candidate.body == 'pytest failed on missing memory worker and now exits 0'
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.visibility_scope == VisibilityScope.PROJECT
    assert candidate.confidence == Decimal('0.500')
    assert candidate.content_hash == memory_candidate_content_hash(observation)
    assert candidate.evidence == [
        {
            'observation_id': str(observation.id),
            'raw_event_id': str(raw_event.id),
            'event_type': 'post_tool_use',
            'title': 'pytest failure fixed',
            'files_read': ['apps/backend/engram/core/models.py'],
            'files_modified': ['apps/backend/engram/memory/services.py'],
        },
    ]
    assert RAW_KEY not in str(candidate.evidence)


@pytest.mark.django_db
def test_observation_recorded_worker_auto_promotes_memory_and_indexes_retrieval() -> None:
    _organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
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
    assert memory.status == MemoryStatus.APPROVED
    assert memory.title == candidate.title
    assert memory.body == candidate.body
    assert version.memory_id == memory.id
    assert version.source_observation_id == observation.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.file_paths == observation.files_read + observation.files_modified
    assert RAW_KEY not in f'{candidate.evidence} {memory.title} {memory.body} {document.full_text}'


@pytest.mark.django_db
def test_observation_recorded_worker_redacts_candidate_content_and_evidence() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    observation.title = f'Bearer {RAW_KEY}'
    observation.body = f'command printed {RAW_KEY}'
    observation.files_read = [f'apps/backend/{RAW_KEY}.txt']
    observation.save(update_fields=['title', 'body', 'files_read', 'updated_at'])

    execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    persisted = f'{candidate.title} {candidate.body} {candidate.evidence}'

    assert RAW_KEY not in persisted
    assert '[REDACTED]' in candidate.title
    assert '[REDACTED]' in candidate.body
    assert '[REDACTED]' in str(candidate.evidence)


@pytest.mark.django_db
def test_observation_recorded_worker_is_idempotent_for_duplicate_delivery() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
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


@pytest.mark.django_db
def test_observation_recorded_worker_reuses_existing_candidate() -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
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

    assert result.duplicate is True
    assert result.candidate.id == candidate.id
    assert MemoryCandidate.objects.count() == 1


@pytest.mark.django_db
def test_observation_recorded_worker_raises_for_missing_observation() -> None:
    missing_observation_id = uuid.uuid4()

    with pytest.raises(MemoryWorkerError, match='observation not found'):
        ProcessObservationRecorded().execute(
            MemoryCandidateWorkerInput(observation_id=missing_observation_id, worker_id='test-worker'),
        )

    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_process_observation_recorded_task_delegates_by_observation_id() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()

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
