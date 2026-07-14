from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryStatus,
    MemoryVersion,
    RetrievalDocument,
    WorkflowWork,
)
from engram.core.redaction import redact_value
from engram.memory.candidate_decision_work import (
    ensure_candidate_decision_work_locked,
    evidence_manifest,
)
from engram.memory.workflow_work import canonical_json_bytes, resolve_work_succeeded


@dataclass(frozen=True, slots=True)
class TransitionScope:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    scope: TransitionScope
    idempotency_key: str
    actor_type: str
    actor_id: str
    capability: str
    request_id: str
    correlation_id: str
    reason: str
    origin: str


@dataclass(frozen=True, slots=True)
class CandidateFence:
    candidate_id: uuid.UUID
    candidate_content_hash: str
    evidence_manifest_hash: str


@dataclass(frozen=True, slots=True)
class MemoryFence:
    memory_id: uuid.UUID
    current_transition_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    state_hash: str


@dataclass(frozen=True, slots=True)
class PromoteMemoryCandidateInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    work_claim: Any | None = None


@dataclass(frozen=True, slots=True)
class AttachPromotedCandidateSourceInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    candidate_source_id: uuid.UUID
    work_claim: Any | None = None


@dataclass(frozen=True, slots=True)
class MemoryTransitionResult:
    transition: Any
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    embedding_work: WorkflowWork | None
    duplicate: bool = False


class MemoryTransitionError(ValueError):
    def __init__(self, code: str, message: str | None = None, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f'{code}: {message or code}')


def _fault_boundary(_point: str) -> None:
    return None


def build_memory_fence(memory: Memory) -> MemoryFence:
    transition_id = getattr(memory, 'current_transition_id', None)
    version_id = None
    if getattr(memory, 'current_version', None):
        version_id = (
            MemoryVersion.objects.filter(memory_id=memory.id, version=memory.current_version)
            .values_list('id', flat=True)
            .first()
        )
    state = {
        'memory_id': str(memory.id),
        'current_transition_id': str(transition_id) if transition_id else None,
        'current_version_id': str(version_id) if version_id else None,
        'current_version': memory.current_version,
        'title': memory.title,
        'body': memory.body,
        'status': memory.status,
        'stale': memory.stale,
        'refuted': memory.refuted,
        'visibility_scope': memory.visibility_scope,
        'team_id': str(memory.team_id) if memory.team_id else None,
    }
    return MemoryFence(
        memory_id=memory.id,
        current_transition_id=transition_id,
        current_version_id=version_id,
        state_hash=hashlib.sha256(canonical_json_bytes(state)).hexdigest(),
    )


def _scope_matches(obj: Any, scope: TransitionScope) -> bool:
    return (
        obj.organization_id == scope.organization_id
        and obj.project_id == scope.project_id
        and obj.team_id == scope.team_id
    )


def _candidate_fence(candidate: MemoryCandidate, fence: CandidateFence) -> None:
    if candidate.id != fence.candidate_id:
        raise MemoryTransitionError(
            'stale_decision', 'candidate fence does not identify the locked candidate', retryable=True
        )
    _entries, manifest_hash = evidence_manifest(candidate)
    if candidate.content_hash != fence.candidate_content_hash or manifest_hash != fence.evidence_manifest_hash:
        raise MemoryTransitionError('stale_decision', 'candidate fence no longer matches', retryable=True)


def _request_fingerprint(request: TransitionRequest, *, action: str, subject_id: uuid.UUID) -> str:
    value = {
        'schema': 'memory_transition_request/v1',
        'action': action,
        'subject_id': str(subject_id),
        'scope': {
            'organization_id': str(request.scope.organization_id),
            'project_id': str(request.scope.project_id),
            'team_id': str(request.scope.team_id) if request.scope.team_id else None,
        },
        'actor_type': request.actor_type,
        'actor_id': request.actor_id,
        'capability': request.capability,
        'reason': request.reason,
        'origin': request.origin,
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _audit_metadata(
    request: TransitionRequest, transition_id: uuid.UUID, *, action: str, ids: dict[str, Any]
) -> dict[str, Any]:
    reason = request.reason if isinstance(request.reason, str) else str(request.reason)
    redacted_reason = str(redact_value(reason).value)[:1024]
    return {
        'schema': 'memory_transition/v1',
        'transition_type': action,
        'transition_id': str(transition_id),
        'origin': request.origin,
        'reason': redacted_reason,
        'scope_filters': {
            'organization_id': str(request.scope.organization_id),
            'project_id': str(request.scope.project_id),
            'team_id': str(request.scope.team_id) if request.scope.team_id else None,
        },
        **{key: str(value) for key, value in ids.items() if value is not None},
    }


def _source_rows(candidate: MemoryCandidate) -> list[MemoryCandidateSource]:
    sources = list(
        MemoryCandidateSource.objects.select_related('window', 'observation', 'stage')
        .filter(candidate_id=candidate.id)
        .order_by('id'),
    )
    return sorted(
        sources,
        key=lambda source: (
            source.window.input_hash,
            source.observation.session_sequence,
            str(source.observation_id),
            source.stage.stage_key,
            source.anchors_hash,
        ),
    )


def _provenance_hash(sources: list[Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            [
                {
                    'id': str(source.id),
                    'source_content_hash': source.source_content_hash,
                    'candidate_source_id': str(source.candidate_source_id) if source.candidate_source_id else None,
                    'source_memory_version_id': str(source.source_memory_version_id)
                    if source.source_memory_version_id
                    else None,
                }
                for source in sources
            ],
        ),
    ).hexdigest()


def _version_sources(version: MemoryVersion) -> list[Any]:
    from engram.core.models import MemoryVersionSource

    sources = list(MemoryVersionSource.objects.filter(memory_version_id=version.id).select_related('candidate_source'))
    return sorted(
        sources, key=lambda source: (str(source.candidate_source_id or ''), str(source.source_memory_version_id or ''))
    )


def _transition_result(transition: Any, *, duplicate: bool = False) -> MemoryTransitionResult:
    memory = transition.result_memory or transition.memory
    version = transition.result_version or transition.to_version
    document = transition.result_exact_document or transition.exact_document
    return MemoryTransitionResult(
        transition=transition,
        memory=memory,
        memory_version=version,
        retrieval_document=document,
        embedding_work=transition.embedding_work,
        duplicate=duplicate,
    )


def _existing_transition(
    request: TransitionRequest, *, fingerprint: str, subject_id: uuid.UUID | None = None
) -> Any | None:
    from engram.core.models import MemoryTransition

    transition = (
        MemoryTransition.objects.select_for_update()
        .filter(
            organization_id=request.scope.organization_id,
            project_id=request.scope.project_id,
            idempotency_key=request.idempotency_key,
        )
        .first()
    )
    if transition is None:
        return None
    if transition.request_fingerprint != fingerprint:
        raise MemoryTransitionError('idempotency_collision', 'idempotency key has a different semantic request')
    if subject_id is not None and transition.candidate_id != subject_id:
        raise MemoryTransitionError('idempotency_collision', 'idempotency key belongs to another candidate')
    return transition


def _create_embedding(document: RetrievalDocument) -> tuple[WorkflowWork, bool]:
    from engram.memory import projections

    return projections.create_embedding_work_and_signal(document=document)


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(request, action='promote', subject_id=data.candidate_fence.candidate_id)
        with transaction.atomic():
            candidate = MemoryCandidate.objects.select_for_update().get(id=data.candidate_fence.candidate_id)
            if not _scope_matches(candidate, request.scope):
                raise MemoryTransitionError('scope', 'candidate is outside the declared scope')
            existing = _existing_transition(request, fingerprint=fingerprint, subject_id=candidate.id)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _candidate_fence(candidate, data.candidate_fence)
            if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
                raise MemoryTransitionError(
                    'idempotency_collision', 'candidate was already promoted by another request'
                )
            if candidate.status != CandidateStatus.PROPOSED:
                raise MemoryTransitionError('candidate_state', 'only proposed candidates can be promoted')
            sources = _source_rows(candidate)
            if not sources:
                raise MemoryTransitionError('provenance', 'promotion requires non-empty provenance')
            memory = Memory.objects.create(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
                title=candidate.title,
                body=candidate.body,
                status=MemoryStatus.APPROVED,
                visibility_scope=candidate.visibility_scope,
                current_version=0,
                transition_contract_version=0,
                confidence=candidate.confidence,
                metadata={
                    'source': 'memory_candidate',
                    'memory_candidate_id': str(candidate.id),
                    'evidence': candidate.evidence,
                    'full_text': f'{candidate.title}\n\n{candidate.body}'.strip(),
                    'file_paths': [
                        *(candidate.source_observation.files_read if candidate.source_observation else []),
                        *(candidate.source_observation.files_modified if candidate.source_observation else []),
                    ],
                    'source_observation_ids': [str(candidate.source_observation_id)]
                    if candidate.source_observation_id
                    else [],
                    **({'kind': candidate.kind} if candidate.kind else {}),
                },
            )
            _fault_boundary('memory')
            version = MemoryVersion.objects.create(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                memory=memory,
                source_observation=candidate.source_observation,
                version=1,
                body=candidate.body,
                content_hash=candidate.content_hash,
            )
            _fault_boundary('version')
            from engram.core.models import MemoryVersionSource

            for source in sources:
                MemoryVersionSource.objects.create(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    memory_version=version,
                    candidate_source=source,
                    source_content_hash=source.anchors_hash,
                )
            _fault_boundary('source')
            from engram.memory import projections

            transition_id = uuid.uuid4()
            provenance_hash = _provenance_hash(_version_sources(version))
            document = projections.write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=_version_sources(version),
            )
            _fault_boundary('exact_document')
            embedding_work, _created = _create_embedding(document)
            _fault_boundary('work_package')
            audit = AuditEvent.objects.create(
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                event_type='MemoryTransitionCommitted',
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                target_type='memory',
                target_id=str(memory.id),
                capability=request.capability,
                result=AuditResult.RECORDED,
                request_id=request.request_id,
                correlation_id=request.correlation_id,
                metadata=_audit_metadata(
                    request,
                    transition_id,
                    action='promote',
                    ids={
                        'candidate_id': candidate.id,
                        'memory_id': memory.id,
                        'version_id': version.id,
                        'work_id': embedding_work.id,
                        'request_fingerprint': fingerprint,
                        'provenance_hash': provenance_hash,
                    },
                ),
            )
            _fault_boundary('audit')
            from engram.core.models import MemoryTransition

            transition = MemoryTransition.objects.create(
                id=transition_id,
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                transition_type='promote',
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                candidate=candidate,
                memory=memory,
                to_version=version,
                result_memory=memory,
                result_version=version,
                exact_document=document,
                result_exact_document=document,
                embedding_work=embedding_work,
                audit_event=audit,
                provenance_hash=provenance_hash,
            )
            _fault_boundary('transition')
            memory.current_version = 1
            memory.transition_contract_version = 1
            memory.current_transition_id = transition.id
            memory.save(
                update_fields=['current_version', 'transition_contract_version', 'current_transition', 'updated_at']
            )
            candidate.status = CandidateStatus.PROMOTED
            candidate.promoted_memory_id = memory.id
            candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])
            _fault_boundary('candidate_pointer')
            decision_work, _ = ensure_candidate_decision_work_locked(candidate)
            resolve_work_succeeded(
                decision_work.id, organization_id=candidate.organization_id, project_id=candidate.project_id
            )
            return _transition_result(transition)


class AttachPromotedCandidateSource:
    def execute(self, data: AttachPromotedCandidateSourceInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(request, action='attach_source', subject_id=data.candidate_source_id)
        with transaction.atomic():
            candidate = MemoryCandidate.objects.select_for_update().get(id=data.candidate_fence.candidate_id)
            if not _scope_matches(candidate, request.scope):
                raise MemoryTransitionError('scope', 'candidate is outside the declared scope')
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _candidate_fence(candidate, data.candidate_fence)
            if candidate.status != CandidateStatus.PROMOTED or candidate.promoted_memory_id is None:
                raise MemoryTransitionError('candidate_state', 'candidate is not promoted')
            memory = Memory.objects.select_for_update().get(id=candidate.promoted_memory_id)
            if not _scope_matches(memory, request.scope) or memory.id != data.memory_fence.memory_id:
                raise MemoryTransitionError('scope', 'memory is outside the declared scope')
            if build_memory_fence(memory) != data.memory_fence:
                raise MemoryTransitionError('stale_decision', 'memory fence no longer matches', retryable=True)
            source = MemoryCandidateSource.objects.select_for_update().get(id=data.candidate_source_id)
            if not _scope_matches(source, request.scope) or source.candidate_id != candidate.id:
                raise MemoryTransitionError('scope', 'candidate source is outside the declared scope')
            version = MemoryVersion.objects.get(memory_id=memory.id, version=memory.current_version)
            from engram.core.models import MemoryTransition, MemoryVersionSource

            if MemoryVersionSource.objects.filter(memory_version_id=version.id, candidate_source_id=source.id).exists():
                raise MemoryTransitionError('idempotency_collision', 'candidate source is already attached')
            MemoryVersionSource.objects.create(
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                memory_version=version,
                candidate_source=source,
                source_content_hash=source.anchors_hash,
            )
            transition_id = uuid.uuid4()
            provenance_hash = _provenance_hash(_version_sources(version))
            document = __import__(
                'engram.memory.projections', fromlist=['write_exact_memory_projection']
            ).write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=_version_sources(version),
            )
            embedding_work, _ = _create_embedding(document)
            audit = AuditEvent.objects.create(
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                event_type='MemoryTransitionCommitted',
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                target_type='memory',
                target_id=str(memory.id),
                capability=request.capability,
                result=AuditResult.RECORDED,
                request_id=request.request_id,
                correlation_id=request.correlation_id,
                metadata=_audit_metadata(
                    request,
                    transition_id,
                    action='attach_source',
                    ids={
                        'candidate_id': candidate.id,
                        'memory_id': memory.id,
                        'version_id': version.id,
                        'work_id': embedding_work.id,
                        'request_fingerprint': fingerprint,
                        'provenance_hash': provenance_hash,
                    },
                ),
            )
            transition = MemoryTransition.objects.create(
                id=transition_id,
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                transition_type='attach_source',
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                candidate=candidate,
                memory=memory,
                from_version=version,
                to_version=version,
                result_memory=memory,
                result_version=version,
                exact_document=document,
                result_exact_document=document,
                embedding_work=embedding_work,
                audit_event=audit,
                provenance_hash=provenance_hash,
            )
            memory.current_transition_id = transition.id
            memory.save(update_fields=['current_transition', 'updated_at'])
            decision_work, _ = ensure_candidate_decision_work_locked(candidate)
            resolve_work_succeeded(
                decision_work.id, organization_id=candidate.organization_id, project_id=candidate.project_id
            )
            return _transition_result(transition)
