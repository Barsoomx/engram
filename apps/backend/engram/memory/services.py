from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

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
    RetrievalDocument,
    VisibilityScope,
)

TOKEN_RE = re.compile(r'(?i)(sk-[a-z0-9][a-z0-9_-]{8,}|egk_[a-z0-9][a-z0-9_-]{8,}|bearer\s+[a-z0-9._~+/=-]{12,})')


@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    observation_id: uuid.UUID
    worker_id: str = 'memory-worker'


@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
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
        with transaction.atomic():
            observation = self._lock_observation(data.observation_id)
            candidate, candidate_created = self._get_or_create_candidate(observation)
            promotion = PromoteMemoryCandidate().execute(
                PromoteMemoryCandidateInput(candidate_id=candidate.id),
            )

            return MemoryCandidateWorkerResult(
                candidate=promotion.candidate,
                memory=promotion.memory,
                memory_version=promotion.memory_version,
                retrieval_document=promotion.retrieval_document,
                duplicate=not candidate_created or promotion.duplicate,
            )

    def _lock_observation(self, observation_id: uuid.UUID) -> Observation:
        try:
            return (
                Observation.objects.select_for_update(of=('self',))
                .select_related('organization', 'project', 'team', 'raw_event')
                .get(id=observation_id)
            )
        except Observation.DoesNotExist as error:
            raise MemoryWorkerError('observation not found') from error

    def _get_or_create_candidate(
        self,
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
                'evidence': candidate_evidence(observation),
                'confidence': Decimal('0.500'),
            },
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


def candidate_evidence(observation: Observation) -> list[dict[str, object]]:
    return [
        {
            'observation_id': str(observation.id),
            'raw_event_id': str(observation.raw_event_id) if observation.raw_event_id else '',
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
            retrieval_document = self._index_memory_version(candidate, version)

            return PromoteMemoryCandidateResult(
                candidate=candidate,
                memory=memory,
                memory_version=version,
                retrieval_document=retrieval_document,
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
        retrieval_document = self._index_memory_version(candidate, version)

        return PromoteMemoryCandidateResult(
            candidate=candidate,
            memory=memory,
            memory_version=version,
            retrieval_document=retrieval_document,
            duplicate=True,
        )

    def _index_memory_version(self, candidate: MemoryCandidate, version: MemoryVersion) -> RetrievalDocument:
        index_result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))
        document = index_result.retrieval_document
        file_paths = self._candidate_file_paths(candidate)
        if document.file_paths == file_paths:
            return document

        document.file_paths = file_paths
        document.save(update_fields=['file_paths', 'updated_at'])

        return document

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

        return [
            redact_text(file_path)
            for file_path in [
                *observation.files_read,
                *observation.files_modified,
            ]
        ]
