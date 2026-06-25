from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from engram.core.models import (
    CandidateStatus,
    MemoryCandidate,
    Observation,
    OutboxEvent,
    OutboxStatus,
    VisibilityScope,
)

OBSERVATION_RECORDED = 'ObservationRecorded'
MEMORY_CANDIDATE_CREATED = 'MemoryCandidateCreated'
TOKEN_RE = re.compile(r'(?i)(sk-[a-z0-9][a-z0-9_-]{8,}|egk_[a-z0-9][a-z0-9_-]{8,}|bearer\s+[a-z0-9._~+/=-]{12,})')


@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    outbox_event_id: uuid.UUID
    worker_id: str = 'memory-worker'


@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    source_outbox: OutboxEvent
    candidate: MemoryCandidate
    downstream_outbox: OutboxEvent
    duplicate: bool


class MemoryWorkerError(Exception):
    pass


class ProcessObservationRecorded:
    def execute(self, data: MemoryCandidateWorkerInput) -> MemoryCandidateWorkerResult:
        try:
            return self._execute(data)
        except MemoryWorkerError as error:
            self._mark_failed(data, error)
            raise

    def _execute(self, data: MemoryCandidateWorkerInput) -> MemoryCandidateWorkerResult:
        with transaction.atomic():
            source_outbox = self._lock_source_outbox(data.outbox_event_id)
            if source_outbox.event_type != OBSERVATION_RECORDED:
                raise MemoryWorkerError(f'expected {OBSERVATION_RECORDED} outbox event')
            if source_outbox.status == OutboxStatus.DONE:
                return self._existing_result(source_outbox)

            self._mark_processing(source_outbox, data.worker_id)
            observation = self._load_observation(source_outbox)
            candidate, candidate_created = self._get_or_create_candidate(source_outbox, observation)
            downstream_outbox, downstream_created = self._get_or_create_downstream_outbox(
                source_outbox,
                candidate,
                observation,
            )
            now = timezone.now()
            source_outbox.status = OutboxStatus.DONE
            source_outbox.processed_at = now
            source_outbox.last_error = ''
            source_outbox.next_retry_at = None
            source_outbox.save(update_fields=['status', 'processed_at', 'last_error', 'next_retry_at', 'updated_at'])

            return MemoryCandidateWorkerResult(
                source_outbox=source_outbox,
                candidate=candidate,
                downstream_outbox=downstream_outbox,
                duplicate=not candidate_created or not downstream_created,
            )

    def _lock_source_outbox(self, outbox_event_id: uuid.UUID) -> OutboxEvent:
        try:
            return OutboxEvent.objects.select_for_update().get(id=outbox_event_id)
        except OutboxEvent.DoesNotExist as error:
            raise MemoryWorkerError('source outbox event not found') from error

    def _mark_processing(self, source_outbox: OutboxEvent, worker_id: str) -> None:
        now = timezone.now()
        source_outbox.status = OutboxStatus.PROCESSING
        source_outbox.attempts += 1
        source_outbox.locked_by = worker_id
        source_outbox.locked_at = now
        source_outbox.save(update_fields=['status', 'attempts', 'locked_by', 'locked_at', 'updated_at'])

    def _load_observation(self, source_outbox: OutboxEvent) -> Observation:
        payload = source_outbox.payload
        if not isinstance(payload, dict):
            raise MemoryWorkerError('ObservationRecorded payload must be a JSON object')

        observation_id = payload.get('observation_id')
        if not observation_id:
            raise MemoryWorkerError('ObservationRecorded payload missing observation_id')

        try:
            return Observation.objects.select_related('organization', 'project', 'team', 'raw_event').get(
                organization=source_outbox.organization,
                project=source_outbox.project,
                id=observation_id,
            )
        except (Observation.DoesNotExist, TypeError, ValidationError, ValueError) as error:
            raise MemoryWorkerError('ObservationRecorded observation_id does not match a scoped observation') from error

    def _get_or_create_candidate(
        self,
        source_outbox: OutboxEvent,
        observation: Observation,
    ) -> tuple[MemoryCandidate, bool]:
        candidate_hash = memory_candidate_content_hash(observation)

        return MemoryCandidate.objects.get_or_create(
            organization=observation.organization,
            project=observation.project,
            content_hash=candidate_hash,
            defaults={
                'team': observation.team,
                'source_observation': observation,
                'title': candidate_title(observation),
                'body': candidate_body(observation),
                'status': CandidateStatus.PROPOSED,
                'visibility_scope': VisibilityScope.PROJECT,
                'evidence': candidate_evidence(source_outbox, observation),
                'confidence': Decimal('0.500'),
            },
        )

    def _get_or_create_downstream_outbox(
        self,
        source_outbox: OutboxEvent,
        candidate: MemoryCandidate,
        observation: Observation,
    ) -> tuple[OutboxEvent, bool]:
        return OutboxEvent.objects.get_or_create(
            organization=candidate.organization,
            event_type=MEMORY_CANDIDATE_CREATED,
            source_type='memory_candidate',
            source_id=str(candidate.id),
            idempotency_key=candidate.content_hash,
            defaults={
                'project': candidate.project,
                'team': candidate.team,
                'aggregate_type': 'memory_candidate',
                'aggregate_id': str(candidate.id),
                'payload_version': 1,
                'payload': {
                    'memory_candidate_id': str(candidate.id),
                    'source_observation_id': str(observation.id),
                    'source_outbox_id': str(source_outbox.id),
                },
                'actor_type': source_outbox.actor_type,
                'actor_id': source_outbox.actor_id,
                'correlation_id': source_outbox.correlation_id,
                'trace_id': source_outbox.trace_id,
            },
        )

    def _existing_result(self, source_outbox: OutboxEvent) -> MemoryCandidateWorkerResult:
        observation = self._load_observation(source_outbox)
        candidate = MemoryCandidate.objects.get(
            organization=observation.organization,
            project=observation.project,
            content_hash=memory_candidate_content_hash(observation),
        )
        downstream_outbox = OutboxEvent.objects.get(
            organization=candidate.organization,
            event_type=MEMORY_CANDIDATE_CREATED,
            source_type='memory_candidate',
            source_id=str(candidate.id),
            idempotency_key=candidate.content_hash,
        )

        return MemoryCandidateWorkerResult(
            source_outbox=source_outbox,
            candidate=candidate,
            downstream_outbox=downstream_outbox,
            duplicate=True,
        )

    def _mark_failed(self, data: MemoryCandidateWorkerInput, error: MemoryWorkerError) -> None:
        now = timezone.now()
        message = redact_error(f'{error.__class__.__name__}: {error}')[:1000]
        OutboxEvent.objects.filter(id=data.outbox_event_id).exclude(status=OutboxStatus.DONE).update(
            status=OutboxStatus.FAILED,
            attempts=F('attempts') + 1,
            locked_by=data.worker_id,
            locked_at=now,
            last_error=message,
            next_retry_at=now + timedelta(minutes=1),
            updated_at=now,
        )


def memory_candidate_content_hash(observation: Observation) -> str:
    source = f'{observation.id}:{observation.content_hash}'

    return hashlib.sha256(source.encode()).hexdigest()


def candidate_title(observation: Observation) -> str:
    return redact_text(observation.title)[:255]


def candidate_body(observation: Observation) -> str:
    body = observation.body.strip()
    if body:
        return redact_text(body)

    return redact_text(observation.title)


def candidate_evidence(source_outbox: OutboxEvent, observation: Observation) -> list[dict[str, object]]:
    return [
        {
            'observation_id': str(observation.id),
            'raw_event_id': str(observation.raw_event_id) if observation.raw_event_id else '',
            'source_outbox_id': str(source_outbox.id),
            'event_type': observation.source_metadata.get('event_type', observation.observation_type),
            'title': redact_text(observation.title),
            'files_read': redact_value(observation.files_read),
            'files_modified': redact_value(observation.files_modified),
        },
    ]


def redact_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)

    return value


def redact_text(value: str) -> str:
    return TOKEN_RE.sub('[REDACTED]', value)


def redact_error(message: str) -> str:
    return redact_text(message)
