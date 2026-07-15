from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
    MemoryVersion,
    MemoryVersionSource,
    RetrievalDocument,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.core.redaction import redact_value
from engram.memory.candidate_decision_work import (
    build_candidate_decision_input,
    candidate_decision_snapshot,
    ensure_candidate_decision_work_locked,
    evidence_manifest,
)
from engram.memory.distillation_provenance import session_candidate_content_hash
from engram.memory.projections import create_embedding_work_and_signal, write_exact_memory_projection
from engram.memory.work_execution import WorkClaim, finish_work_claim, lock_work_fence
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
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class AttachPromotedCandidateSourceInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    candidate_source_id: uuid.UUID
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class MemoryTransitionResult:
    transition: MemoryTransition
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
    transition_id = memory.current_transition_id
    version_id = None
    if memory.current_version:
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


def _scope_matches(
    obj: Memory | MemoryCandidate | MemoryCandidateSource | WorkflowWork,
    scope: TransitionScope,
) -> bool:
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
    session_id = candidate.source_observation.session_id if candidate.source_observation_id else None
    if session_id is None:
        session_id = (
            MemoryCandidateSource.objects.filter(candidate_id=candidate.id)
            .order_by('id')
            .values_list('observation__session_id', flat=True)
            .first()
        )
    if session_id is None:
        raise MemoryTransitionError(
            'stale_decision',
            'candidate content session cannot be reconstructed',
            retryable=True,
        )
    canonical_content_hash = session_candidate_content_hash(session_id, candidate.title, candidate.body)
    if (
        candidate.content_hash != canonical_content_hash
        or fence.candidate_content_hash != canonical_content_hash
        or manifest_hash != fence.evidence_manifest_hash
    ):
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
    request: TransitionRequest, transition_id: uuid.UUID, *, action: str, ids: dict[str, object]
) -> dict[str, object]:
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
        MemoryCandidateSource.objects.select_for_update()
        .select_related('window', 'observation', 'stage')
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


def memory_version_provenance_hash(sources: list[MemoryVersionSource]) -> str:
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


def _version_sources(version: MemoryVersion) -> list[MemoryVersionSource]:
    sources = list(MemoryVersionSource.objects.filter(memory_version_id=version.id).select_related('candidate_source'))
    return sorted(
        sources, key=lambda source: (str(source.candidate_source_id or ''), str(source.source_memory_version_id or ''))
    )


def _transition_result(transition: MemoryTransition, *, duplicate: bool = False) -> MemoryTransitionResult:
    return MemoryTransitionResult(
        transition=transition,
        memory=transition.result_memory,
        memory_version=transition.result_version,
        retrieval_document=transition.result_exact_document,
        embedding_work=transition.embedding_work,
        duplicate=duplicate,
    )


def _existing_transition(
    request: TransitionRequest, *, fingerprint: str, subject_id: uuid.UUID | None = None
) -> MemoryTransition | None:
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
    return create_embedding_work_and_signal(document=document)


def _lock_unclaimed_candidate_work(request: TransitionRequest, candidate_id: uuid.UUID) -> None:
    list(
        WorkflowWork.objects.select_for_update()
        .filter(
            organization_id=request.scope.organization_id,
            project_id=request.scope.project_id,
            work_type=WorkflowWorkType.CANDIDATE_DECISION,
            subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
            subject_id=candidate_id,
            disposition=WorkflowWorkDisposition.REQUIRED,
        )
        .order_by('id')
    )


def _require_claimed_candidate_work(work: WorkflowWork, candidate: MemoryCandidate) -> None:
    expected_snapshot = candidate_decision_snapshot(build_candidate_decision_input(candidate))
    if (
        work.organization_id != candidate.organization_id
        or work.project_id != candidate.project_id
        or work.team_id != candidate.team_id
        or work.work_type != WorkflowWorkType.CANDIDATE_DECISION
        or work.subject_type != WorkflowSubjectType.MEMORY_CANDIDATE
        or work.subject_id != candidate.id
        or work.input_snapshot != expected_snapshot
    ):
        raise MemoryTransitionError('stale_decision', 'owning candidate work no longer matches', retryable=True)


def _finish_candidate_work(
    candidate: MemoryCandidate,
    *,
    claim: WorkClaim | None,
    claimed_work: WorkflowWork | None,
    result_memory_id: uuid.UUID,
) -> None:
    decision_work, _created = ensure_candidate_decision_work_locked(candidate)
    if claim is not None:
        if claimed_work is None or decision_work.id != claimed_work.id:
            raise MemoryTransitionError('stale_decision', 'owning candidate work generation changed', retryable=True)
        finish_work_claim(
            claim=claim,
            now=timezone.now(),
            completion='product_succeeded',
            result_memory_id=result_memory_id,
        )
        return
    resolve_work_succeeded(
        decision_work.id,
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
    )


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(request, action='promote', subject_id=data.candidate_fence.candidate_id)
        with transaction.atomic():
            claimed_work = None
            if data.work_claim is not None:
                claimed_work, _run = lock_work_fence(claim=data.work_claim, now=timezone.now())
            else:
                _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
            candidate = MemoryCandidate.objects.select_for_update().get(id=data.candidate_fence.candidate_id)
            if not _scope_matches(candidate, request.scope):
                raise MemoryTransitionError('scope', 'candidate is outside the declared scope')
            if claimed_work is not None:
                _require_claimed_candidate_work(claimed_work, candidate)
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
            transition_id = uuid.uuid4()
            version_sources = _version_sources(version)
            provenance_hash = memory_version_provenance_hash(version_sources)
            document = write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=version_sources,
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
                        'exact_document_id': document.id,
                        'exact_projection_hash': document.exact_projection_hash,
                        'work_id': embedding_work.id,
                        'request_fingerprint': fingerprint,
                        'provenance_hash': provenance_hash,
                    },
                ),
            )
            _fault_boundary('audit')
            transition = MemoryTransition.objects.create(
                id=transition_id,
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                transition_type=MemoryTransitionType.PROMOTE,
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
            _finish_candidate_work(
                candidate,
                claim=data.work_claim,
                claimed_work=claimed_work,
                result_memory_id=memory.id,
            )
            return _transition_result(transition)


class AttachPromotedCandidateSource:
    def execute(self, data: AttachPromotedCandidateSourceInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(request, action='attach_source', subject_id=data.candidate_source_id)
        with transaction.atomic():
            claimed_work = None
            if data.work_claim is not None:
                claimed_work, _run = lock_work_fence(claim=data.work_claim, now=timezone.now())
            else:
                _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
            candidate = MemoryCandidate.objects.select_for_update().get(id=data.candidate_fence.candidate_id)
            if not _scope_matches(candidate, request.scope):
                raise MemoryTransitionError('scope', 'candidate is outside the declared scope')
            if claimed_work is not None:
                _require_claimed_candidate_work(claimed_work, candidate)
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _candidate_fence(candidate, data.candidate_fence)
            if candidate.status != CandidateStatus.PROMOTED or candidate.promoted_memory_id is None:
                raise MemoryTransitionError('candidate_state', 'candidate is not promoted')
            source = MemoryCandidateSource.objects.select_for_update().get(id=data.candidate_source_id)
            if not _scope_matches(source, request.scope) or source.candidate_id != candidate.id:
                raise MemoryTransitionError('scope', 'candidate source is outside the declared scope')
            memory = Memory.objects.select_for_update().get(id=candidate.promoted_memory_id)
            if not _scope_matches(memory, request.scope) or memory.id != data.memory_fence.memory_id:
                raise MemoryTransitionError('scope', 'memory is outside the declared scope')
            version = MemoryVersion.objects.select_for_update().get(
                memory_id=memory.id,
                version=memory.current_version,
            )
            if build_memory_fence(memory) != data.memory_fence:
                raise MemoryTransitionError('stale_decision', 'memory fence no longer matches', retryable=True)
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
            version_sources = _version_sources(version)
            provenance_hash = memory_version_provenance_hash(version_sources)
            document = write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=version_sources,
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
                        'exact_document_id': document.id,
                        'exact_projection_hash': document.exact_projection_hash,
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
                transition_type=MemoryTransitionType.ATTACH_SOURCE,
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
            _finish_candidate_work(
                candidate,
                claim=data.work_claim,
                claimed_work=claimed_work,
                result_memory_id=memory.id,
            )
            return _transition_result(transition)
