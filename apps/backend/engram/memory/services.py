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

from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Observation,
    OutboxEvent,
    OutboxStatus,
    RetrievalDocument,
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


@dataclass(frozen=True)
class PromoteMemoryCandidateInput:
    candidate_id: uuid.UUID


@dataclass(frozen=True)
class PromoteMemoryCandidateResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    duplicate: bool


@dataclass(frozen=True)
class MemoryFeedbackInput:
    raw_key: str
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    action: str
    reason: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class MemoryFeedbackResult:
    memory: Memory
    action: str
    retrieval_documents_updated: int
    already_applied: bool

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'project_id': str(self.memory.project_id),
            'team_id': str(self.memory.team_id) if self.memory.team_id else '',
            'action': self.action,
            'stale': self.memory.stale,
            'refuted': self.memory.refuted,
            'retrieval_documents_updated': self.retrieval_documents_updated,
            'already_applied': self.already_applied,
        }


class MemoryFeedbackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordMemoryFeedback:
    def execute(self, data: MemoryFeedbackInput) -> MemoryFeedbackResult:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='memories:review',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='memory',
            target_id=str(data.memory_id),
        )
        with transaction.atomic():
            memory = self._lock_memory(data, scope)
            self._ensure_team_scope(memory, scope)
            already_applied = self._already_applied(memory, data.action)
            self._apply(memory, data.action)
            updated = self._sync_retrieval_documents(memory, data.action)
            self._audit(memory, scope, data, updated, already_applied)

        return MemoryFeedbackResult(
            memory=memory,
            action=data.action,
            retrieval_documents_updated=updated,
            already_applied=already_applied,
        )

    def _lock_memory(self, data: MemoryFeedbackInput, scope: EffectiveScope) -> Memory:
        memory = (
            Memory.objects.select_for_update()
            .filter(
                organization_id=scope.organization_id,
                project_id=data.project_id,
                id=data.memory_id,
            )
            .first()
        )
        if memory is None:
            raise MemoryFeedbackError('memory_not_found', 'Memory was not found')

        return memory

    def _ensure_team_scope(self, memory: Memory, scope: EffectiveScope) -> None:
        if (
            memory.visibility_scope == VisibilityScope.TEAM
            and memory.team_id is not None
            and memory.team_id not in scope.team_ids
        ):
            raise AccessDeniedError('team_scope_denied', 'Memory is outside effective team scope')

    def _already_applied(self, memory: Memory, action: str) -> bool:
        return bool(getattr(memory, action))

    def _apply(self, memory: Memory, action: str) -> None:
        if getattr(memory, action):
            return

        setattr(memory, action, True)
        memory.save(update_fields=[action, 'updated_at'])

    def _sync_retrieval_documents(self, memory: Memory, action: str) -> int:
        return RetrievalDocument.objects.filter(
            organization=memory.organization,
            project=memory.project,
            memory=memory,
        ).update(**{action: True})

    def _audit(
        self,
        memory: Memory,
        scope: EffectiveScope,
        data: MemoryFeedbackInput,
        updated: int,
        already_applied: bool,
    ) -> None:
        AuditEvent.objects.create(
            organization=memory.organization,
            project=memory.project,
            team=memory.team,
            event_type='MemoryFeedbackRecorded',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory',
            target_id=str(memory.id),
            capability='memories:review',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'action': data.action,
                'reason': redact_text(data.reason),
                'retrieval_documents_updated': updated,
                'already_applied': already_applied,
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
            },
        )


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


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> PromoteMemoryCandidateResult:
        with transaction.atomic():
            candidate = self._lock_candidate(data.candidate_id)
            if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
                return self._existing_result(candidate)
            if candidate.status != CandidateStatus.PROPOSED:
                raise MemoryWorkerError('Only proposed memory candidates can be promoted')

            memory = Memory.objects.create(
                organization=candidate.organization,
                project=candidate.project,
                team=candidate.team,
                title=candidate.title,
                body=candidate.body,
                status=MemoryStatus.APPROVED,
                visibility_scope=candidate.visibility_scope,
                confidence=candidate.confidence,
                metadata=self._memory_metadata(candidate),
            )
            version = MemoryVersion.objects.create(
                organization=candidate.organization,
                project=candidate.project,
                memory=memory,
                source_observation=candidate.source_observation,
                version=1,
                body=candidate.body,
                content_hash=candidate.content_hash,
            )
            candidate.status = CandidateStatus.PROMOTED
            candidate.promoted_memory = memory
            candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])
            index_result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

            return PromoteMemoryCandidateResult(
                candidate=candidate,
                memory=memory,
                memory_version=version,
                retrieval_document=index_result.retrieval_document,
                duplicate=False,
            )

    def _lock_candidate(self, candidate_id: uuid.UUID) -> MemoryCandidate:
        try:
            return MemoryCandidate.objects.select_for_update().get(id=candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('memory candidate not found') from error

    def _existing_result(self, candidate: MemoryCandidate) -> PromoteMemoryCandidateResult:
        memory = candidate.promoted_memory
        version = MemoryVersion.objects.get(memory=memory, version=memory.current_version)
        index_result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

        return PromoteMemoryCandidateResult(
            candidate=candidate,
            memory=memory,
            memory_version=version,
            retrieval_document=index_result.retrieval_document,
            duplicate=True,
        )

    def _memory_metadata(self, candidate: MemoryCandidate) -> dict[str, object]:
        return {
            'source': 'memory_candidate',
            'memory_candidate_id': str(candidate.id),
            'evidence': candidate.evidence,
            'file_paths': self._candidate_file_paths(candidate),
        }

    def _candidate_file_paths(self, candidate: MemoryCandidate) -> list[str]:
        observation = candidate.source_observation
        if observation is None:
            return []

        return [*observation.files_read, *observation.files_modified]
