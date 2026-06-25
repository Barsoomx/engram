from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    MemoryCandidate,
    Observation,
    ObservationSource,
    Organization,
    OutboxEvent,
    OutboxStatus,
    Project,
    RawEventEnvelope,
    Runtime,
    Team,
    VisibilityScope,
)
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
    memory_candidate_content_hash,
)
from engram.memory.tasks import process_observation_recorded_outbox

RAW_KEY = 'egk_test_memory_worker_0123456789abcdefghijklmnopqrstuvwxyz'


def create_observation_recorded_scope(
    *,
    outbox_event_type: str = 'ObservationRecorded',
    outbox_payload: dict[str, Any] | None = None,
) -> tuple[Organization, Team, Project, AgentSession, RawEventEnvelope, Observation, OutboxEvent]:
    organization = Organization.objects.create(name='Engram', slug='engram')
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
        external_id='codex-local',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-1',
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
        client_event_id='event-1',
        idempotency_key='idem-1',
        content_hash='hash-event-1',
        runtime=Runtime.CODEX,
        payload_schema_version='v1',
        payload={
            'tool_name': 'bash',
            'authorization': f'Bearer {RAW_KEY}',
            'tool_response': {'stdout': RAW_KEY},
        },
        headers={},
        request_id='request-event-1',
        actor_type='api_key',
        actor_id='api-key-1',
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
        content_hash='hash-observation-1',
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
        source_id='event-1',
        citation='event-1',
        metadata={'event_type': 'post_tool_use'},
    )
    payload = (
        outbox_payload
        if outbox_payload is not None
        else {
            'raw_event_id': str(raw_event.id),
            'observation_id': str(observation.id),
            'agent_session_id': str(session.id),
            'event_type': raw_event.event_type,
        }
    )
    outbox = OutboxEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        aggregate_type='observation',
        aggregate_id=str(observation.id),
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        event_type=outbox_event_type,
        payload_version=1,
        payload=payload,
        idempotency_key=raw_event.idempotency_key,
        actor_type='api_key',
        actor_id='api-key-1',
        correlation_id='correlation-1',
        trace_id='trace-1',
    )

    return organization, team, project, session, raw_event, observation, outbox


def execute_worker(outbox: OutboxEvent) -> Any:
    return ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(outbox_event_id=outbox.id, worker_id='test-worker'),
    )


@pytest.mark.django_db
def test_observation_recorded_worker_creates_candidate_and_downstream_outbox() -> None:
    _organization, team, project, _session, raw_event, observation, outbox = create_observation_recorded_scope()

    result = execute_worker(outbox)

    assert result.duplicate is False
    candidate = MemoryCandidate.objects.get()
    source_outbox = OutboxEvent.objects.get(id=outbox.id)
    downstream = OutboxEvent.objects.get(event_type='MemoryCandidateCreated')

    assert result.candidate.id == candidate.id
    assert result.source_outbox.id == source_outbox.id
    assert result.downstream_outbox.id == downstream.id
    assert candidate.organization_id == project.organization_id
    assert candidate.project_id == project.id
    assert candidate.team_id == team.id
    assert candidate.source_observation_id == observation.id
    assert candidate.title == 'pytest failure fixed'
    assert candidate.body == 'pytest failed on missing memory worker and now exits 0'
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.visibility_scope == VisibilityScope.PROJECT
    assert candidate.confidence == Decimal('0.500')
    assert candidate.content_hash == memory_candidate_content_hash(observation)
    assert candidate.evidence == [
        {
            'observation_id': str(observation.id),
            'raw_event_id': str(raw_event.id),
            'source_outbox_id': str(outbox.id),
            'event_type': 'post_tool_use',
            'title': 'pytest failure fixed',
            'files_read': ['apps/backend/engram/core/models.py'],
            'files_modified': ['apps/backend/engram/memory/services.py'],
        },
    ]
    assert source_outbox.status == OutboxStatus.DONE
    assert source_outbox.attempts == 1
    assert source_outbox.processed_at is not None
    assert source_outbox.last_error == ''
    assert downstream.organization_id == project.organization_id
    assert downstream.project_id == project.id
    assert downstream.team_id == team.id
    assert downstream.aggregate_type == 'memory_candidate'
    assert downstream.aggregate_id == str(candidate.id)
    assert downstream.source_type == 'memory_candidate'
    assert downstream.source_id == str(candidate.id)
    assert downstream.idempotency_key == candidate.content_hash
    assert downstream.payload == {
        'memory_candidate_id': str(candidate.id),
        'source_observation_id': str(observation.id),
        'source_outbox_id': str(outbox.id),
    }
    assert RAW_KEY not in str(candidate.evidence)
    assert RAW_KEY not in str(downstream.payload)


@pytest.mark.django_db
def test_observation_recorded_worker_redacts_candidate_content_and_evidence() -> None:
    _organization, _team, _project, _session, _raw_event, observation, outbox = create_observation_recorded_scope()
    observation.title = f'Bearer {RAW_KEY}'
    observation.body = f'command printed {RAW_KEY}'
    observation.files_read = [f'apps/backend/{RAW_KEY}.txt']
    observation.save(update_fields=['title', 'body', 'files_read', 'updated_at'])

    execute_worker(outbox)

    candidate = MemoryCandidate.objects.get()
    persisted = f'{candidate.title} {candidate.body} {candidate.evidence}'

    assert RAW_KEY not in persisted
    assert '[REDACTED]' in candidate.title
    assert '[REDACTED]' in candidate.body
    assert '[REDACTED]' in str(candidate.evidence)


@pytest.mark.django_db
def test_observation_recorded_worker_is_idempotent_for_duplicate_delivery() -> None:
    _organization, _team, _project, _session, _raw_event, _observation, outbox = create_observation_recorded_scope()
    first = execute_worker(outbox)

    second = execute_worker(outbox)

    assert second.duplicate is True
    assert second.candidate.id == first.candidate.id
    assert second.downstream_outbox.id == first.downstream_outbox.id
    assert MemoryCandidate.objects.count() == 1
    assert OutboxEvent.objects.count() == 2


@pytest.mark.django_db
def test_observation_recorded_worker_reuses_existing_candidate_before_marking_done() -> None:
    organization, team, project, _session, _raw_event, observation, outbox = create_observation_recorded_scope()
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
                'source_outbox_id': str(outbox.id),
                'event_type': 'post_tool_use',
                'title': observation.title,
                'files_read': observation.files_read,
                'files_modified': observation.files_modified,
            },
        ],
        content_hash=memory_candidate_content_hash(observation),
        confidence=Decimal('0.500'),
    )
    downstream = OutboxEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        aggregate_type='memory_candidate',
        aggregate_id=str(candidate.id),
        source_type='memory_candidate',
        source_id=str(candidate.id),
        event_type='MemoryCandidateCreated',
        payload_version=1,
        payload={
            'memory_candidate_id': str(candidate.id),
            'source_observation_id': str(observation.id),
            'source_outbox_id': str(outbox.id),
        },
        idempotency_key=candidate.content_hash,
    )

    result = execute_worker(outbox)

    outbox.refresh_from_db()

    assert result.duplicate is True
    assert result.candidate.id == candidate.id
    assert result.downstream_outbox.id == downstream.id
    assert outbox.status == OutboxStatus.DONE
    assert MemoryCandidate.objects.count() == 1
    assert OutboxEvent.objects.count() == 2


@pytest.mark.django_db
def test_observation_recorded_worker_marks_wrong_event_type_failed() -> None:
    _organization, _team, _project, _session, _raw_event, _observation, outbox = create_observation_recorded_scope(
        outbox_event_type='OtherEvent',
    )

    with pytest.raises(MemoryWorkerError):
        execute_worker(outbox)

    outbox.refresh_from_db()

    assert outbox.status == OutboxStatus.FAILED
    assert outbox.attempts == 1
    assert 'MemoryWorkerError' in outbox.last_error
    assert 'ObservationRecorded' in outbox.last_error
    assert RAW_KEY not in outbox.last_error
    assert outbox.next_retry_at is not None
    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_observation_recorded_worker_marks_missing_observation_id_failed() -> None:
    _organization, _team, _project, _session, _raw_event, _observation, outbox = create_observation_recorded_scope(
        outbox_payload={'raw_event_id': 'raw-event-only'},
    )

    with pytest.raises(MemoryWorkerError):
        execute_worker(outbox)

    outbox.refresh_from_db()

    assert outbox.status == OutboxStatus.FAILED
    assert outbox.attempts == 1
    assert 'observation_id' in outbox.last_error
    assert outbox.next_retry_at is not None
    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_observation_recorded_worker_marks_malformed_observation_id_failed() -> None:
    _organization, _team, _project, _session, _raw_event, _observation, outbox = create_observation_recorded_scope(
        outbox_payload={'observation_id': ['not', 'a', 'uuid']},
    )

    with pytest.raises(MemoryWorkerError):
        execute_worker(outbox)

    outbox.refresh_from_db()

    assert outbox.status == OutboxStatus.FAILED
    assert outbox.attempts == 1
    assert 'observation_id' in outbox.last_error
    assert outbox.next_retry_at is not None
    assert MemoryCandidate.objects.count() == 0


@pytest.mark.django_db
def test_process_observation_recorded_outbox_task_delegates_by_outbox_id() -> None:
    _organization, _team, _project, _session, _raw_event, _observation, outbox = create_observation_recorded_scope()

    candidate_id = process_observation_recorded_outbox.run(str(outbox.id))

    outbox.refresh_from_db()
    candidate = MemoryCandidate.objects.get()

    assert candidate_id == str(candidate.id)
    assert outbox.status == OutboxStatus.DONE
