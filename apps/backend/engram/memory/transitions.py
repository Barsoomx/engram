from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    MemoryConflict,
    MemoryConflictResolution,
    MemoryLink,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
    MemoryVersion,
    MemoryVersionSource,
    Observation,
    ObservationSource,
    RetrievalDocument,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.core.redaction import redact_value
from engram.memory.candidate_decision_work import (
    CandidateDecisionWorkScopeError,
    build_candidate_decision_input,
    candidate_decision_snapshot,
    ensure_candidate_decision_work_locked,
)
from engram.memory.deterministic_gates import (
    EffectiveCandidateScope,
    SanitizedCandidateView,
    effective_candidate_scope,
    redact_candidate_view,
)
from engram.memory.distillation_provenance import session_candidate_content_hash
from engram.memory.import_provenance import (
    ImportProvenanceError,
    agent_proposal_candidate_content_hash,
    candidate_evidence_manifest,
    import_source_metadata,
)
from engram.memory.projections import create_embedding_work_and_signal, write_exact_memory_projection
from engram.memory.work_execution import WorkClaim, finish_work_claim, lock_work_fence
from engram.memory.workflow_work import canonical_json_bytes, resolve_work_no_signal, resolve_work_succeeded


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
    sanitized_title: str | None = None
    sanitized_body: str | None = None
    effective_visibility_scope: str | None = None
    effective_team_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AttachPromotedCandidateSourceInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    candidate_source_id: uuid.UUID
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class PublishDigestMemoryInput:
    request: TransitionRequest
    source_memory_fences: tuple[MemoryFence, ...]
    title: str
    body: str
    source_memory_version_ids: tuple[uuid.UUID, ...] = ()
    work_claim: WorkClaim | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    visibility_scope: str = VisibilityScope.PROJECT


@dataclass(frozen=True, slots=True)
class ReviseMemoryInput:
    request: TransitionRequest
    memory_fence: MemoryFence
    title: str
    body: str
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class ReviseMemoryFromCandidateInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    title: str
    body: str
    work_claim: WorkClaim | None = None
    sanitized_title: str | None = None
    sanitized_body: str | None = None
    effective_visibility_scope: str | None = None
    effective_team_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MergeMemoryCandidateInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    title: str
    body: str
    work_claim: WorkClaim | None = None
    sanitized_title: str | None = None
    sanitized_body: str | None = None
    effective_visibility_scope: str | None = None
    effective_team_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MergeMemoriesInput:
    request: TransitionRequest
    source_memory_fence: MemoryFence
    result_memory_fence: MemoryFence
    title: str
    body: str
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class SupersedeMemoryWithCandidateInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    loser_memory_fence: MemoryFence
    work_claim: WorkClaim | None = None
    sanitized_title: str | None = None
    sanitized_body: str | None = None
    effective_visibility_scope: str | None = None
    effective_team_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SupersedeMemoriesInput:
    request: TransitionRequest
    source_memory_fence: MemoryFence
    result_memory_fence: MemoryFence
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class MemoryStateInput:
    request: TransitionRequest
    memory_fence: MemoryFence
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class OpenMemoryConflictInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    memory_fence: MemoryFence
    evidence_hash: str
    redacted_reason: str
    work_claim: WorkClaim | None = None


@dataclass(frozen=True, slots=True)
class ResolveMemoryConflictInput:
    request: TransitionRequest
    candidate_fence: CandidateFence
    conflict_ids: tuple[uuid.UUID, ...]
    conflict_memory_fences: tuple[MemoryFence, ...]
    resolution: str
    selected_memory_fence: MemoryFence | None = None
    title: str | None = None
    body: str | None = None
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


_PROMOTION_SOURCE_KINDS = frozenset(
    {
        MemoryCandidateSourceKind.DISTILLATION,
        MemoryCandidateSourceKind.IMPORT,
        MemoryCandidateSourceKind.AGENT_PROPOSAL,
    }
)
_NON_PROMOTION_SOURCE_KINDS = frozenset(
    {MemoryCandidateSourceKind.DISTILLATION, MemoryCandidateSourceKind.AGENT_PROPOSAL}
)


def _candidate_fence(
    candidate: MemoryCandidate,
    fence: CandidateFence,
    sources: list[MemoryCandidateSource],
    *,
    allowed_source_kinds: frozenset[str],
) -> None:
    if candidate.id != fence.candidate_id:
        raise MemoryTransitionError(
            'stale_decision', 'candidate fence does not identify the locked candidate', retryable=True
        )
    kinds = {source.source_kind for source in sources}
    if len(kinds) > 1:
        raise MemoryTransitionError('provenance', 'candidate provenance has mixed source kinds')

    if any(source.source_kind not in allowed_source_kinds for source in sources):
        raise MemoryTransitionError('provenance', 'candidate provenance source kind is not allowed')
    try:
        _entries, manifest_hash = candidate_evidence_manifest(candidate, sources=sources)
    except (ImportProvenanceError, ValueError, TypeError, AttributeError) as error:
        raise MemoryTransitionError('stale_decision', 'candidate provenance is invalid', retryable=True) from error
    source_kind = next(iter(kinds)) if kinds else None
    canonical_content_hash = _canonical_candidate_content_hash(candidate, source_kind, sources)
    if (
        candidate.content_hash != canonical_content_hash
        or fence.candidate_content_hash != canonical_content_hash
        or manifest_hash != fence.evidence_manifest_hash
    ):
        raise MemoryTransitionError('stale_decision', 'candidate fence no longer matches', retryable=True)


def _canonical_candidate_content_hash(
    candidate: MemoryCandidate,
    source_kind: str | None,
    sources: list[MemoryCandidateSource],
) -> str:
    if source_kind == MemoryCandidateSourceKind.IMPORT:
        return candidate.content_hash

    if source_kind == MemoryCandidateSourceKind.AGENT_PROPOSAL:
        if candidate.source_observation_id is not None:
            raise MemoryTransitionError('provenance', 'agent candidate must not have a source observation')

        return agent_proposal_candidate_content_hash(candidate.title, candidate.body, candidate.kind, candidate.team_id)

    session_id = candidate.source_observation.session_id if candidate.source_observation_id else None
    if session_id is None and sources:
        session_id = sources[0].observation.session_id
    if session_id is None:
        raise MemoryTransitionError(
            'stale_decision',
            'candidate content session cannot be reconstructed',
            retryable=True,
        )

    return session_candidate_content_hash(session_id, candidate.title, candidate.body)


def _candidate_fence_value(fence: CandidateFence) -> dict[str, object]:
    return {
        'candidate_id': str(fence.candidate_id),
        'candidate_content_hash': fence.candidate_content_hash,
        'evidence_manifest_hash': fence.evidence_manifest_hash,
    }


def _memory_fence_value(fence: MemoryFence) -> dict[str, object]:
    return {
        'memory_id': str(fence.memory_id),
        'current_transition_id': str(fence.current_transition_id) if fence.current_transition_id else None,
        'current_version_id': str(fence.current_version_id) if fence.current_version_id else None,
        'state_hash': fence.state_hash,
    }


def _work_claim_value(claim: WorkClaim | None) -> dict[str, object] | None:
    if claim is None:
        return None

    return {'work_id': str(claim.work_id)}


def _request_fingerprint(
    request: TransitionRequest,
    *,
    action: str,
    subject_id: uuid.UUID,
    command: dict[str, object],
) -> str:
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
        'command': command,
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
        MemoryCandidateSource.objects.select_for_update(of=('self',))
        .select_related('window', 'observation', 'stage', 'import_source')
        .filter(candidate_id=candidate.id)
        .order_by('id'),
    )

    observation_ids = {source.observation_id for source in sources if source.observation_id is not None}
    if candidate.source_observation_id is not None:
        observation_ids.add(candidate.source_observation_id)
    observations = {
        observation.id: observation
        for observation in Observation.objects.select_for_update().filter(id__in=observation_ids).order_by('id')
    }
    import_source_ids = {source.import_source_id for source in sources if source.import_source_id is not None}
    import_sources = {
        source.id: source
        for source in ObservationSource.objects.select_for_update().filter(id__in=import_source_ids).order_by('id')
    }
    if candidate.source_observation_id is not None and candidate.source_observation_id in observations:
        candidate.source_observation = observations[candidate.source_observation_id]
    for source in sources:
        if source.observation_id is not None:
            source.observation = observations[source.observation_id]
        if source.import_source_id is not None:
            source.import_source = import_sources[source.import_source_id]

    def sort_key(source: MemoryCandidateSource) -> tuple[object, ...]:
        if source.source_kind == MemoryCandidateSourceKind.AGENT_PROPOSAL:
            return ('agent_proposal', source.anchors_hash)
        if source.source_kind == MemoryCandidateSourceKind.IMPORT:
            return (
                'import',
                source.observation.session_sequence,
                str(source.observation_id),
                source.anchors_hash,
            )
        return (
            'distillation',
            source.window.input_hash,
            source.observation.session_sequence,
            str(source.observation_id),
            source.stage.stage_key,
            source.anchors_hash,
        )

    return sorted(sources, key=sort_key)


def canonical_memory_version_sources(
    sources: list[MemoryVersionSource] | tuple[MemoryVersionSource, ...],
) -> list[MemoryVersionSource]:
    return sorted(
        sources,
        key=lambda source: (
            str(source.candidate_source_id or ''),
            str(source.source_memory_version_id or ''),
            str(source.id),
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
                for source in canonical_memory_version_sources(sources)
            ],
        ),
    ).hexdigest()


def _version_sources(version: MemoryVersion) -> list[MemoryVersionSource]:
    sources = list(MemoryVersionSource.objects.filter(memory_version_id=version.id).select_related('candidate_source'))
    return canonical_memory_version_sources(sources)


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
    result_memory_id: uuid.UUID | None,
    completion: str | None = None,
) -> None:
    decision_work, _created = ensure_candidate_decision_work_locked(candidate)
    resolved_completion = completion or ('product_succeeded' if result_memory_id is not None else 'product_no_signal')
    if claim is not None:
        if claimed_work is None or decision_work.id != claimed_work.id:
            raise MemoryTransitionError('stale_decision', 'owning candidate work generation changed', retryable=True)
        finish_work_claim(
            claim=claim,
            now=timezone.now(),
            completion=resolved_completion,
            result_memory_id=result_memory_id,
        )
        return
    if decision_work.disposition != WorkflowWorkDisposition.REQUIRED:
        return
    resolver = resolve_work_succeeded if resolved_completion == 'product_succeeded' else resolve_work_no_signal
    resolver(decision_work.id, organization_id=candidate.organization_id, project_id=candidate.project_id)


def _lock_optional_work(claim: WorkClaim | None, request: TransitionRequest) -> WorkflowWork | None:
    if claim is None:
        return None
    work, _run = lock_work_fence(claim=claim, now=timezone.now())
    if not _scope_matches(work, request.scope):
        raise MemoryTransitionError('scope', 'owning work is outside the declared scope')

    return work


def _finish_optional_work(claim: WorkClaim | None, *, result_memory_id: uuid.UUID | None) -> None:
    if claim is None:
        return
    finish_work_claim(
        claim=claim,
        now=timezone.now(),
        completion='product_succeeded' if result_memory_id is not None else 'product_no_signal',
        result_memory_id=result_memory_id,
    )


def _reject_existing_memory_work_claim(claim: WorkClaim | None) -> None:
    if claim is not None:
        raise MemoryTransitionError(
            'stale_decision',
            'existing-memory transitions do not accept workflow work claims',
            retryable=True,
        )


def _digest_snapshot_source_set(work: WorkflowWork) -> set[tuple[str, str]]:
    key = 'sources' if work.work_type == WorkflowWorkType.DAILY_DIGEST else 'changes'
    refs = work.input_snapshot.get(key)
    if not isinstance(refs, list):
        raise MemoryTransitionError('stale_decision', 'digest work snapshot has no frozen source set', retryable=True)
    source_set: set[tuple[str, str]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            raise MemoryTransitionError('stale_decision', 'digest work snapshot source is malformed', retryable=True)
        memory_id = ref.get('memory_id')
        version_id = ref.get('memory_version_id')
        if not isinstance(memory_id, str) or not isinstance(version_id, str):
            raise MemoryTransitionError('stale_decision', 'digest work snapshot source is malformed', retryable=True)
        source_set.add((memory_id, version_id))

    return source_set


def _validate_digest_work_claim(
    work: WorkflowWork,
    request: TransitionRequest,
    source_pairs: set[tuple[str, str]],
) -> None:
    if work.work_type not in (WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST):
        raise MemoryTransitionError('stale_decision', 'owning work is not digest work', retryable=True)
    if work.organization_id != request.scope.organization_id or work.project_id != request.scope.project_id:
        raise MemoryTransitionError(
            'stale_decision', 'digest work subject is outside the declared scope', retryable=True
        )
    if work.subject_type == WorkflowSubjectType.PROJECT:
        if work.subject_id != request.scope.project_id or work.team_id is not None or request.scope.team_id is not None:
            raise MemoryTransitionError(
                'stale_decision', 'digest work project subject does not match request', retryable=True
            )
    elif work.subject_type == WorkflowSubjectType.TEAM:
        if (
            work.work_type != WorkflowWorkType.WEEKLY_DIGEST
            or work.team_id != work.subject_id
            or request.scope.team_id != work.subject_id
        ):
            raise MemoryTransitionError(
                'stale_decision', 'digest work team subject does not match request', retryable=True
            )
    else:
        raise MemoryTransitionError('stale_decision', 'digest work subject type is invalid', retryable=True)

    if _digest_snapshot_source_set(work) != source_pairs:
        raise MemoryTransitionError('stale_decision', 'digest source snapshot no longer matches', retryable=True)


def _lock_candidate(request: TransitionRequest, fence: CandidateFence) -> MemoryCandidate:
    try:
        candidate = MemoryCandidate.objects.select_for_update().get(id=fence.candidate_id)
    except MemoryCandidate.DoesNotExist as error:
        raise MemoryTransitionError('scope', 'candidate is outside the declared scope') from error
    if not _scope_matches(candidate, request.scope):
        raise MemoryTransitionError('scope', 'candidate is outside the declared scope')

    return candidate


def _declared_fence_ids(fences: tuple[MemoryFence, ...]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    if not fences:
        raise MemoryTransitionError('memory_state', 'at least one memory fence is required')
    memory_ids = [fence.memory_id for fence in fences]
    if len(set(memory_ids)) != len(memory_ids):
        raise MemoryTransitionError('memory_state', 'memory fences must identify distinct memories')
    version_ids: list[uuid.UUID] = []
    for fence in fences:
        if fence.current_version_id is None:
            raise MemoryTransitionError(
                'stale_decision',
                'memory fence has no declared current version',
                retryable=True,
            )
        version_ids.append(fence.current_version_id)

    return memory_ids, version_ids


def _locked_memory_map(
    request: TransitionRequest,
    locked_memories: list[Memory],
    *,
    expected_count: int,
) -> dict[uuid.UUID, Memory]:
    if len(locked_memories) != expected_count:
        raise MemoryTransitionError('scope', 'a declared memory is outside the available scope')
    memories = {memory.id: memory for memory in locked_memories}
    for memory in locked_memories:
        if not _scope_matches(memory, request.scope):
            raise MemoryTransitionError('scope', 'memory is outside the declared scope')
        if memory.transition_contract_version != 1 or memory.current_transition_id is None:
            raise MemoryTransitionError('memory_state', 'memory is not owned by the transition contract')

    return memories


def _locked_version_map(
    request: TransitionRequest,
    fences: tuple[MemoryFence, ...],
    locked_versions: list[MemoryVersion],
    *,
    expected_count: int,
) -> dict[uuid.UUID, MemoryVersion]:
    if len(locked_versions) != expected_count:
        raise MemoryTransitionError('stale_decision', 'a declared memory version no longer exists', retryable=True)
    versions = {version.memory_id: version for version in locked_versions}
    for fence in fences:
        version = versions.get(fence.memory_id)
        if version is None or version.id != fence.current_version_id:
            raise MemoryTransitionError(
                'stale_decision',
                'memory version fence does not match its memory',
                retryable=True,
            )
        if not _scope_matches(version.memory, request.scope):
            raise MemoryTransitionError('scope', 'memory version is outside the declared scope')

    return versions


def _lock_declared_memories(
    request: TransitionRequest,
    fences: tuple[MemoryFence, ...],
) -> tuple[dict[uuid.UUID, Memory], dict[uuid.UUID, MemoryVersion]]:
    memory_ids, version_ids = _declared_fence_ids(fences)

    locked_memories = list(Memory.objects.select_for_update().filter(id__in=memory_ids).order_by('id'))
    memories = _locked_memory_map(request, locked_memories, expected_count=len(memory_ids))
    locked_versions = list(MemoryVersion.objects.select_for_update().filter(id__in=version_ids).order_by('id'))
    versions = _locked_version_map(request, fences, locked_versions, expected_count=len(version_ids))

    return memories, versions


def _digest_source_scope_matches(memory: Memory, request: TransitionRequest, visibility_scope: str) -> bool:
    if memory.organization_id != request.scope.organization_id or memory.project_id != request.scope.project_id:
        return False
    if visibility_scope == VisibilityScope.PROJECT:
        return memory.visibility_scope == VisibilityScope.PROJECT
    if visibility_scope == VisibilityScope.TEAM:
        return memory.visibility_scope == VisibilityScope.PROJECT or (
            memory.visibility_scope == VisibilityScope.TEAM
            and request.scope.team_id is not None
            and memory.team_id == request.scope.team_id
        )
    return False


def _validate_digest_source_scope(
    memories: Mapping[uuid.UUID, Memory], request: TransitionRequest, visibility_scope: str
) -> None:
    if not memories:
        raise MemoryTransitionError('memory_state', 'at least one digest source memory is required')
    for memory in memories.values():
        if memory.transition_contract_version != 1 or memory.current_transition_id is None:
            raise MemoryTransitionError('memory_state', 'digest source is not owned by the transition contract')
        if not _digest_source_scope_matches(memory, request, visibility_scope):
            raise MemoryTransitionError('scope', 'digest source is outside the declared visibility scope')


def _validate_digest_output_scope(request: TransitionRequest, visibility_scope: str) -> None:
    if visibility_scope == VisibilityScope.PROJECT:
        return
    if visibility_scope == VisibilityScope.TEAM and request.scope.team_id is not None:
        return
    raise MemoryTransitionError('scope', 'digest output visibility does not match the declared scope')


def _lock_digest_sources_from_fences(
    request: TransitionRequest,
    fences: tuple[MemoryFence, ...],
    visibility_scope: str,
) -> tuple[dict[uuid.UUID, Memory], dict[uuid.UUID, MemoryVersion]]:
    memory_ids, version_ids = _declared_fence_ids(fences)
    locked_memories = list(
        Memory.objects.select_for_update()
        .filter(id__in=memory_ids, organization_id=request.scope.organization_id, project_id=request.scope.project_id)
        .order_by('id')
    )
    if len(locked_memories) != len(memory_ids):
        raise MemoryTransitionError('scope', 'a declared memory is outside the available scope')
    memories = {memory.id: memory for memory in locked_memories}
    _validate_digest_source_scope(memories, request, visibility_scope)

    locked_versions = list(
        MemoryVersion.objects.select_for_update()
        .filter(id__in=version_ids, organization_id=request.scope.organization_id, project_id=request.scope.project_id)
        .order_by('id')
    )
    if len(locked_versions) != len(version_ids):
        raise MemoryTransitionError('stale_decision', 'a declared memory version no longer exists', retryable=True)
    versions = {version.memory_id: version for version in locked_versions}
    if set(versions) != set(memory_ids):
        raise MemoryTransitionError('stale_decision', 'memory version fence does not match its memory', retryable=True)
    for fence in fences:
        version = versions.get(fence.memory_id)
        if version is None or version.id != fence.current_version_id:
            raise MemoryTransitionError(
                'stale_decision', 'memory version fence does not match its memory', retryable=True
            )

    return memories, versions


def _normalise_digest_version_ids(version_ids: tuple[uuid.UUID, ...] | None) -> tuple[uuid.UUID, ...]:
    if not version_ids:
        raise MemoryTransitionError('memory_state', 'at least one digest source version is required')
    try:
        normalised = tuple(uuid.UUID(str(version_id)) for version_id in version_ids)
    except (TypeError, ValueError, AttributeError) as error:
        raise MemoryTransitionError('memory_state', 'digest source version ids are malformed') from error
    if len(set(normalised)) != len(normalised):
        raise MemoryTransitionError('memory_state', 'digest source versions must be distinct')
    return normalised


def _lock_digest_sources_from_versions(
    request: TransitionRequest,
    version_ids: tuple[uuid.UUID, ...],
    visibility_scope: str,
) -> tuple[dict[uuid.UUID, Memory], dict[uuid.UUID, MemoryVersion]]:
    # Read the immutable rows only to discover their parent memories, then lock
    # memories before taking the version locks to preserve the transition lock order.
    declared_memory_ids = list(MemoryVersion.objects.filter(id__in=version_ids).values_list('memory_id', flat=True))
    if len(declared_memory_ids) != len(version_ids) or len(set(declared_memory_ids)) != len(declared_memory_ids):
        raise MemoryTransitionError('stale_decision', 'a declared digest source version is invalid', retryable=True)
    locked_memories = list(
        Memory.objects.select_for_update()
        .filter(
            id__in=declared_memory_ids,
            organization_id=request.scope.organization_id,
            project_id=request.scope.project_id,
        )
        .order_by('id')
    )
    if len(locked_memories) != len(declared_memory_ids):
        raise MemoryTransitionError('scope', 'a declared memory is outside the available scope')
    memories = {memory.id: memory for memory in locked_memories}
    _validate_digest_source_scope(memories, request, visibility_scope)

    locked_versions = list(
        MemoryVersion.objects.select_for_update()
        .filter(
            id__in=version_ids,
            organization_id=request.scope.organization_id,
            project_id=request.scope.project_id,
        )
        .order_by('id')
    )
    if len(locked_versions) != len(version_ids):
        raise MemoryTransitionError('stale_decision', 'a declared digest source version is invalid', retryable=True)
    versions = {version.memory_id: version for version in locked_versions}
    if set(versions) != set(memories):
        raise MemoryTransitionError('stale_decision', 'digest source version does not match its memory', retryable=True)

    return memories, versions


def _lock_exact_documents(versions: dict[uuid.UUID, MemoryVersion]) -> dict[uuid.UUID, RetrievalDocument]:
    version_ids = [version.id for version in versions.values()]
    documents = list(
        RetrievalDocument.objects.select_for_update().filter(memory_version_id__in=version_ids).order_by('id')
    )
    if len(documents) != len(version_ids):
        raise MemoryTransitionError('projection', 'a declared source version has no exact retrieval document')

    return {document.memory_id: document for document in documents}


def _verify_memory_fences(memories: dict[uuid.UUID, Memory], fences: tuple[MemoryFence, ...]) -> None:
    for fence in fences:
        if build_memory_fence(memories[fence.memory_id]) != fence:
            raise MemoryTransitionError('stale_decision', 'memory fence no longer matches', retryable=True)


def _require_active_memory(memory: Memory) -> None:
    if memory.status != MemoryStatus.APPROVED or memory.stale or memory.refuted:
        raise MemoryTransitionError('memory_state', 'memory is not active')


def _require_proposed_candidate(candidate: MemoryCandidate) -> None:
    if candidate.status != CandidateStatus.PROPOSED or candidate.promoted_memory_id is not None:
        raise MemoryTransitionError('candidate_state', 'only proposed candidates can be settled')


def _require_no_open_conflict(candidate: MemoryCandidate) -> None:
    if MemoryConflict.objects.filter(candidate_id=candidate.id, resolved_transition__isnull=True).exists():
        raise MemoryTransitionError('unresolved_conflict', 'candidate has unresolved memory conflicts')


def _require_sha256(value: str, *, field: str) -> None:
    if len(value) != 64 or value.lower() != value or any(character not in '0123456789abcdef' for character in value):
        raise MemoryTransitionError('command', f'{field} must be a lowercase SHA-256 digest')


def _content_hash(*, memory_id: uuid.UUID, version: int, title: str, body: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                'schema': 'memory_content/v1',
                'memory_id': str(memory_id),
                'version': version,
                'title': title,
                'body': body,
            }
        )
    ).hexdigest()


def _version_source_hash(version: MemoryVersion) -> str:
    value = version.content_hash
    if len(value) == 64 and value.lower() == value and all(character in '0123456789abcdef' for character in value):
        return value

    return hashlib.sha256(
        canonical_json_bytes(
            {
                'memory_version_id': str(version.id),
                'content_hash': value,
                'body': version.body,
            }
        )
    ).hexdigest()


def _create_version_sources(
    version: MemoryVersion,
    *,
    candidate_sources: list[MemoryCandidateSource] | tuple[MemoryCandidateSource, ...] = (),
    memory_versions: list[MemoryVersion] | tuple[MemoryVersion, ...] = (),
) -> list[MemoryVersionSource]:
    if not candidate_sources and not memory_versions:
        raise MemoryTransitionError('provenance', 'memory version requires non-empty provenance')
    for source in candidate_sources:
        MemoryVersionSource.objects.create(
            organization_id=version.organization_id,
            project_id=version.project_id,
            team_id=version.memory.team_id,
            memory_version=version,
            candidate_source=source,
            source_content_hash=source.anchors_hash,
        )
    for source_version in memory_versions:
        MemoryVersionSource.objects.create(
            organization_id=version.organization_id,
            project_id=version.project_id,
            team_id=version.memory.team_id,
            memory_version=version,
            source_memory_version=source_version,
            source_content_hash=_version_source_hash(source_version),
        )
    _fault_boundary('source')

    return _version_sources(version)


def _current_version_sources(version: MemoryVersion) -> list[MemoryVersionSource]:
    sources = _version_sources(version)
    if not sources:
        raise MemoryTransitionError('provenance', 'current memory version has no transition provenance')

    return sources


def _candidate_memory_metadata(candidate: MemoryCandidate, *, title: str, body: str) -> dict[str, object]:
    observation = candidate.source_observation
    return {
        'source': 'memory_candidate',
        'memory_candidate_id': str(candidate.id),
        'evidence': candidate.evidence,
        'full_text': f'{title}\n\n{body}'.strip(),
        'file_paths': [
            *(observation.files_read if observation else []),
            *(observation.files_modified if observation else []),
        ],
        'source_observation_ids': [str(candidate.source_observation_id)] if candidate.source_observation_id else [],
        **({'kind': candidate.kind} if candidate.kind else {}),
    }


def _revalidated_sanitized_view(
    candidate: MemoryCandidate,
    *,
    sanitized_title: str | None,
    sanitized_body: str | None,
) -> SanitizedCandidateView | None:
    if sanitized_title is None:
        return None

    view = redact_candidate_view(candidate)
    if view.title != sanitized_title or view.body != sanitized_body:
        raise MemoryTransitionError(
            'stale_decision',
            'sanitized candidate content no longer derives from the locked candidate',
            retryable=True,
        )

    return view


def _revalidated_effective_scope(
    candidate: MemoryCandidate,
    sources: list[MemoryCandidateSource],
    *,
    effective_visibility_scope: str | None,
    effective_team_id: uuid.UUID | None,
) -> EffectiveCandidateScope | None:
    if effective_visibility_scope is None:
        return None

    try:
        derived = effective_candidate_scope(candidate, sources)
    except CandidateDecisionWorkScopeError as error:
        raise MemoryTransitionError(
            'stale_decision', 'effective candidate scope could not be revalidated', retryable=True
        ) from error
    if derived.visibility_scope != effective_visibility_scope or derived.team_id != effective_team_id:
        raise MemoryTransitionError(
            'stale_decision',
            'effective candidate scope no longer derives from the locked candidate',
            retryable=True,
        )

    return derived


def _sanitized_metadata(metadata: dict[str, object]) -> dict[str, object]:
    redacted = redact_value(metadata).value

    return redacted if isinstance(redacted, dict) else dict(metadata)


def _create_candidate_memory(
    candidate: MemoryCandidate,
    sources: list[MemoryCandidateSource],
    *,
    title: str | None = None,
    body: str | None = None,
    scope_override: EffectiveCandidateScope | None = None,
    sanitize_metadata: bool = False,
) -> tuple[Memory, MemoryVersion, list[MemoryVersionSource]]:
    result_title = title if title is not None else candidate.title
    result_body = body if body is not None else candidate.body
    visibility_scope = scope_override.visibility_scope if scope_override is not None else candidate.visibility_scope
    team_id = candidate.team_id
    metadata = _candidate_memory_metadata(candidate, title=result_title, body=result_body)
    if sanitize_metadata:
        metadata = _sanitized_metadata(metadata)
    memory = Memory.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=team_id,
        title=result_title,
        body=result_body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        current_version=0,
        transition_contract_version=0,
        confidence=candidate.confidence,
        metadata=metadata,
    )
    _fault_boundary('memory')
    content_hash = (
        candidate.content_hash
        if result_title == candidate.title and result_body == candidate.body
        else _content_hash(memory_id=memory.id, version=1, title=result_title, body=result_body)
    )
    version = MemoryVersion.objects.create(
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        memory=memory,
        source_observation=candidate.source_observation,
        version=1,
        body=result_body,
        content_hash=content_hash,
        source_metadata={'full_text': f'{result_title}\n\n{result_body}'.strip()},
    )
    _fault_boundary('version')
    version_sources = _create_version_sources(version, candidate_sources=sources)

    return memory, version, version_sources


def _revision_metadata(memory: Memory, version: MemoryVersion, *, title: str, body: str) -> dict[str, object]:
    metadata = dict(memory.metadata) if isinstance(memory.metadata, dict) else {}
    version_metadata = dict(version.source_metadata) if isinstance(version.source_metadata, dict) else {}
    metadata.update(version_metadata)
    metadata['full_text'] = f'{title}\n\n{body}'.strip()

    return metadata


def _create_revision_version(
    memory: Memory,
    prior_version: MemoryVersion,
    *,
    title: str,
    body: str,
    candidate_sources: list[MemoryCandidateSource] | tuple[MemoryCandidateSource, ...] = (),
    extra_source_versions: list[MemoryVersion] | tuple[MemoryVersion, ...] = (),
) -> tuple[MemoryVersion, list[MemoryVersionSource]]:
    next_version = memory.current_version + 1
    metadata = _revision_metadata(memory, prior_version, title=title, body=body)
    version = MemoryVersion.objects.create(
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        memory=memory,
        version=next_version,
        body=body,
        content_hash=_content_hash(memory_id=memory.id, version=next_version, title=title, body=body),
        source_metadata=metadata,
    )
    _fault_boundary('version')
    memory.title = title
    memory.body = body
    memory.metadata = metadata
    source_versions = [prior_version, *extra_source_versions]
    version_sources = _create_version_sources(
        version,
        candidate_sources=candidate_sources,
        memory_versions=source_versions,
    )

    return version, version_sources


def _canonical_digest_metadata(
    metadata: Mapping[str, object],
    *,
    title: str,
    body: str,
) -> dict[str, object]:
    canonical_metadata = json.loads(canonical_json_bytes(dict(metadata)))
    canonical_metadata.update(
        {
            'kind': 'digest',
            'source': 'digest_work',
            'full_text': f'{title}\n\n{body}'.strip(),
        }
    )

    return canonical_metadata


def _create_digest_memory(
    request: TransitionRequest,
    *,
    title: str,
    body: str,
    source_versions: list[MemoryVersion],
    metadata: Mapping[str, object],
    visibility_scope: str,
) -> tuple[Memory, MemoryVersion, list[MemoryVersionSource]]:
    canonical_metadata = _canonical_digest_metadata(metadata, title=title, body=body)
    memory = Memory.objects.create(
        organization_id=request.scope.organization_id,
        project_id=request.scope.project_id,
        team_id=request.scope.team_id,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        current_version=0,
        transition_contract_version=0,
        metadata=canonical_metadata,
    )
    _fault_boundary('memory')
    version = MemoryVersion.objects.create(
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        memory=memory,
        version=1,
        body=body,
        content_hash=_content_hash(memory_id=memory.id, version=1, title=title, body=body),
        source_metadata=dict(canonical_metadata),
    )
    _fault_boundary('version')
    version_sources = _create_version_sources(version, memory_versions=source_versions)

    return memory, version, version_sources


def _create_semantic_link(*, source: Memory, result: Memory, link_type: str) -> MemoryLink:
    link = MemoryLink.objects.create(
        organization_id=source.organization_id,
        project_id=source.project_id,
        memory=source,
        link_type=link_type,
        target=str(result.id),
        label='',
    )
    _fault_boundary('link')

    return link


def _create_conflict_link(*, candidate: MemoryCandidate, memory: Memory) -> MemoryLink:
    link = MemoryLink.objects.create(
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        memory=memory,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
        label='',
    )
    _fault_boundary('link')

    return link


def _commit_transition(
    *,
    request: TransitionRequest,
    transition_id: uuid.UUID,
    transition_type: str,
    fingerprint: str,
    memory: Memory,
    from_version: MemoryVersion | None,
    to_version: MemoryVersion,
    result_memory: Memory,
    result_version: MemoryVersion,
    exact_document: RetrievalDocument,
    result_exact_document: RetrievalDocument,
    provenance_hash: str,
    candidate: MemoryCandidate | None = None,
    embedding_work: WorkflowWork | None = None,
    semantic_link: MemoryLink | None = None,
    audit_ids: dict[str, object] | None = None,
) -> MemoryTransition:
    ids = {
        'candidate_id': candidate.id if candidate else None,
        'memory_id': memory.id,
        'from_version_id': from_version.id if from_version else None,
        'to_version_id': to_version.id,
        'result_memory_id': result_memory.id,
        'result_version_id': result_version.id,
        'exact_document_id': exact_document.id,
        'result_exact_document_id': result_exact_document.id,
        'exact_projection_hash': exact_document.exact_projection_hash,
        'result_exact_projection_hash': result_exact_document.exact_projection_hash,
        'work_id': embedding_work.id if embedding_work else None,
        'semantic_link_id': semantic_link.id if semantic_link else None,
        'request_fingerprint': fingerprint,
        'provenance_hash': provenance_hash,
        **(audit_ids or {}),
    }
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
        metadata=_audit_metadata(request, transition_id, action=transition_type, ids=ids),
    )
    _fault_boundary('audit')
    transition_row = MemoryTransition.objects.create(
        id=transition_id,
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        team_id=memory.team_id,
        transition_type=transition_type,
        idempotency_key=request.idempotency_key,
        request_fingerprint=fingerprint,
        candidate=candidate,
        memory=memory,
        from_version=from_version,
        to_version=to_version,
        result_memory=result_memory,
        result_version=result_version,
        exact_document=exact_document,
        result_exact_document=result_exact_document,
        embedding_work=embedding_work,
        semantic_link=semantic_link,
        audit_event=audit,
        provenance_hash=provenance_hash,
    )
    _fault_boundary('transition')

    return transition_row


def _advance_memory_pointer(memory: Memory, transition_row: MemoryTransition, version: MemoryVersion) -> None:
    memory.body = version.body
    memory.current_version = version.version
    memory.transition_contract_version = 1
    memory.current_transition_id = transition_row.id
    memory.save(
        update_fields=[
            'title',
            'body',
            'status',
            'stale',
            'refuted',
            'metadata',
            'current_version',
            'transition_contract_version',
            'current_transition',
            'updated_at',
        ]
    )


def _promotion_uses_import_source(
    candidate: MemoryCandidate,
    sources: list[MemoryCandidateSource],
    *,
    work_claim: WorkClaim | None,
    claimed_work: WorkflowWork | None,
) -> bool:
    import_only = all(source.source_kind == MemoryCandidateSourceKind.IMPORT for source in sources)
    if import_only:
        if len(sources) != 1:
            raise MemoryTransitionError('provenance', 'import provenance is invalid')
        if work_claim is not None:
            raise MemoryTransitionError(
                'stale_decision',
                'import promotion does not accept candidate decision work',
                retryable=True,
            )
    elif any(source.source_kind not in _NON_PROMOTION_SOURCE_KINDS for source in sources):
        raise MemoryTransitionError('provenance', 'candidate provenance has mixed source kinds')
    if claimed_work is not None and not import_only:
        _require_claimed_candidate_work(claimed_work, candidate)
    return import_only


def _promotion_memory_metadata(
    candidate: MemoryCandidate,
    sources: list[MemoryCandidateSource],
    *,
    import_only: bool,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        'source': 'memory_candidate',
        'memory_candidate_id': str(candidate.id),
        'evidence': candidate.evidence,
        'full_text': f'{candidate.title}\n\n{candidate.body}'.strip(),
        'file_paths': [
            *(candidate.source_observation.files_read if candidate.source_observation else []),
            *(candidate.source_observation.files_modified if candidate.source_observation else []),
        ],
        'source_observation_ids': [str(candidate.source_observation_id)] if candidate.source_observation_id else [],
        **({'kind': candidate.kind} if candidate.kind else {}),
    }
    if import_only:
        try:
            metadata.update(import_source_metadata(candidate, sources=sources))
        except (ImportProvenanceError, ValueError, TypeError, AttributeError) as error:
            raise MemoryTransitionError('provenance', 'import provenance is invalid') from error
    return metadata


def _finish_promotion_work(
    candidate: MemoryCandidate,
    *,
    import_only: bool,
    claim: WorkClaim | None,
    claimed_work: WorkflowWork | None,
    result_memory_id: uuid.UUID,
) -> None:
    if import_only:
        return
    _finish_candidate_work(
        candidate,
        claim=claim,
        claimed_work=claimed_work,
        result_memory_id=result_memory_id,
    )


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(
            request,
            action='promote',
            subject_id=data.candidate_fence.candidate_id,
            command={
                'candidate_fence': _candidate_fence_value(data.candidate_fence),
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            claimed_work = None
            if data.work_claim is not None:
                claimed_work, _run = lock_work_fence(claim=data.work_claim, now=timezone.now())
            else:
                _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
            candidate = MemoryCandidate.objects.select_for_update().get(id=data.candidate_fence.candidate_id)
            if not _scope_matches(candidate, request.scope):
                raise MemoryTransitionError('scope', 'candidate is outside the declared scope')
            existing = _existing_transition(request, fingerprint=fingerprint, subject_id=candidate.id)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
                raise MemoryTransitionError(
                    'idempotency_collision', 'candidate was already promoted by another request'
                )
            if candidate.status != CandidateStatus.PROPOSED:
                raise MemoryTransitionError('candidate_state', 'only proposed candidates can be promoted')
            sources = _source_rows(candidate)
            if not sources:
                raise MemoryTransitionError('provenance', 'promotion requires non-empty provenance')
            import_only = _promotion_uses_import_source(
                candidate,
                sources,
                work_claim=data.work_claim,
                claimed_work=claimed_work,
            )
            _candidate_fence(
                candidate,
                data.candidate_fence,
                sources,
                allowed_source_kinds=_PROMOTION_SOURCE_KINDS,
            )
            _require_no_open_conflict(candidate)
            sanitized_view = _revalidated_sanitized_view(
                candidate,
                sanitized_title=data.sanitized_title,
                sanitized_body=data.sanitized_body,
            )
            effective_scope = _revalidated_effective_scope(
                candidate,
                sources,
                effective_visibility_scope=data.effective_visibility_scope,
                effective_team_id=data.effective_team_id,
            )
            result_title = sanitized_view.title if sanitized_view is not None else candidate.title
            result_body = sanitized_view.body if sanitized_view is not None else candidate.body
            visibility_scope = (
                effective_scope.visibility_scope if effective_scope is not None else candidate.visibility_scope
            )
            team_id = candidate.team_id
            metadata = _promotion_memory_metadata(candidate, sources, import_only=import_only)
            if sanitized_view is not None:
                metadata['full_text'] = f'{result_title}\n\n{result_body}'.strip()
                metadata = _sanitized_metadata(metadata)
            memory = Memory.objects.create(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=team_id,
                title=result_title,
                body=result_body,
                status=MemoryStatus.APPROVED,
                visibility_scope=visibility_scope,
                current_version=0,
                transition_contract_version=0,
                confidence=candidate.confidence,
                metadata=metadata,
            )
            _fault_boundary('memory')
            content_hash = (
                candidate.content_hash
                if result_title == candidate.title and result_body == candidate.body
                else _content_hash(memory_id=memory.id, version=1, title=result_title, body=result_body)
            )
            version = MemoryVersion.objects.create(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                memory=memory,
                source_observation=candidate.source_observation,
                version=1,
                body=result_body,
                content_hash=content_hash,
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
            _finish_promotion_work(
                candidate,
                import_only=import_only,
                claim=data.work_claim,
                claimed_work=claimed_work,
                result_memory_id=memory.id,
            )
            return _transition_result(transition)


class AttachPromotedCandidateSource:
    def execute(self, data: AttachPromotedCandidateSourceInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(
            request,
            action='attach_source',
            subject_id=data.candidate_source_id,
            command={
                'candidate_fence': _candidate_fence_value(data.candidate_fence),
                'memory_fence': {
                    'memory_id': str(data.memory_fence.memory_id),
                    'current_version_id': (
                        str(data.memory_fence.current_version_id) if data.memory_fence.current_version_id else None
                    ),
                },
                'candidate_source_id': str(data.candidate_source_id),
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
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
            candidate_sources = _source_rows(candidate)
            _candidate_fence(
                candidate,
                data.candidate_fence,
                candidate_sources,
                allowed_source_kinds=_NON_PROMOTION_SOURCE_KINDS,
            )
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


def _write_exact(
    *,
    memory: Memory,
    version: MemoryVersion,
    transition_id: uuid.UUID,
    sources: list[MemoryVersionSource],
) -> RetrievalDocument:
    return write_exact_memory_projection(
        memory=memory,
        version=version,
        transition_id=transition_id,
        sources=sources,
    )


def _embedding_for_active_result(memory: Memory, document: RetrievalDocument) -> WorkflowWork | None:
    if memory.status != MemoryStatus.APPROVED or memory.stale or memory.refuted:
        return None
    work, _created = _create_embedding(document)
    _fault_boundary('work_package')

    return work


class PublishDigestMemory:
    def execute(self, data: PublishDigestMemoryInput) -> MemoryTransitionResult:
        request = data.request
        fences = tuple(data.source_memory_fences)
        version_ids = (
            _normalise_digest_version_ids(data.source_memory_version_ids) if data.source_memory_version_ids else ()
        )
        if bool(fences) == bool(version_ids):
            raise MemoryTransitionError(
                'command', 'exactly one of source_memory_fences or source_memory_version_ids is required'
            )
        _validate_digest_output_scope(request, data.visibility_scope)
        metadata = _canonical_digest_metadata(data.metadata, title=data.title, body=data.body)
        source_command = (
            {
                'source_memory_fences': [
                    _memory_fence_value(fence) for fence in sorted(fences, key=lambda item: str(item.memory_id))
                ]
            }
            if fences
            else {'source_memory_version_ids': [str(version_id) for version_id in sorted(version_ids, key=str)]}
        )
        fingerprint = _request_fingerprint(
            request,
            action='publish_digest',
            subject_id=data.work_claim.work_id if data.work_claim else request.scope.project_id,
            command={
                **source_command,
                'title': data.title,
                'body': data.body,
                'metadata': metadata,
                'visibility_scope': data.visibility_scope,
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            claimed_work = _lock_optional_work(data.work_claim, request)
            if fences:
                memories, versions = _lock_digest_sources_from_fences(request, fences, data.visibility_scope)
                source_pairs = {(str(fence.memory_id), str(fence.current_version_id)) for fence in fences}
            else:
                memories, versions = _lock_digest_sources_from_versions(request, version_ids, data.visibility_scope)
                source_pairs = {(str(memory_id), str(version.id)) for memory_id, version in versions.items()}
            _lock_exact_documents(versions)
            if claimed_work is not None:
                _validate_digest_work_claim(claimed_work, request, source_pairs)
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            if fences:
                _verify_memory_fences(memories, fences)
            source_versions = sorted(versions.values(), key=lambda version: str(version.id))
            memory, version, version_sources = _create_digest_memory(
                request,
                title=data.title,
                body=data.body,
                source_versions=source_versions,
                metadata=metadata,
                visibility_scope=data.visibility_scope,
            )
            transition_id = uuid.uuid4()
            document = _write_exact(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=version_sources,
            )
            _fault_boundary('exact_document')
            embedding_work = _embedding_for_active_result(memory, document)
            provenance_hash = memory_version_provenance_hash(version_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.PUBLISH_DIGEST,
                fingerprint=fingerprint,
                memory=memory,
                from_version=None,
                to_version=version,
                result_memory=memory,
                result_version=version,
                exact_document=document,
                result_exact_document=document,
                embedding_work=embedding_work,
                provenance_hash=provenance_hash,
                audit_ids={'source_version_ids': ','.join(str(item.id) for item in source_versions)},
            )
            _advance_memory_pointer(memory, transition_row, version)
            _fault_boundary('candidate_pointer')
            _finish_optional_work(data.work_claim, result_memory_id=memory.id)

            return _transition_result(transition_row)


class ReviseMemory:
    def execute(self, data: ReviseMemoryInput) -> MemoryTransitionResult:
        _reject_existing_memory_work_claim(data.work_claim)
        request = data.request
        fingerprint = _request_fingerprint(
            request,
            action='revise',
            subject_id=data.memory_fence.memory_id,
            command={
                'memory_fence': _memory_fence_value(data.memory_fence),
                'title': data.title,
                'body': data.body,
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        fences = (data.memory_fence,)
        with transaction.atomic():
            memories, versions = _lock_declared_memories(request, fences)
            _lock_exact_documents(versions)
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _verify_memory_fences(memories, fences)
            memory = memories[data.memory_fence.memory_id]
            prior_version = versions[memory.id]
            _require_active_memory(memory)
            version, version_sources = _create_revision_version(
                memory,
                prior_version,
                title=data.title,
                body=data.body,
            )
            transition_id = uuid.uuid4()
            document = _write_exact(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=version_sources,
            )
            _fault_boundary('exact_document')
            embedding_work = _embedding_for_active_result(memory, document)
            provenance_hash = memory_version_provenance_hash(version_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.REVISE,
                fingerprint=fingerprint,
                memory=memory,
                from_version=prior_version,
                to_version=version,
                result_memory=memory,
                result_version=version,
                exact_document=document,
                result_exact_document=document,
                embedding_work=embedding_work,
                provenance_hash=provenance_hash,
            )
            _advance_memory_pointer(memory, transition_row, version)
            _fault_boundary('candidate_pointer')

            return _transition_result(transition_row)


def _candidate_revision_fingerprint(
    data: ReviseMemoryFromCandidateInput | MergeMemoryCandidateInput,
    *,
    action: str,
) -> str:
    return _request_fingerprint(
        data.request,
        action=action,
        subject_id=data.candidate_fence.candidate_id,
        command={
            'candidate_fence': _candidate_fence_value(data.candidate_fence),
            'memory_fence': _memory_fence_value(data.memory_fence),
            'title': data.title,
            'body': data.body,
            'work_claim': _work_claim_value(data.work_claim),
        },
    )


def _execute_candidate_revision(
    data: ReviseMemoryFromCandidateInput | MergeMemoryCandidateInput,
    *,
    transition_type: str,
) -> MemoryTransitionResult:
    request = data.request
    fingerprint = _candidate_revision_fingerprint(data, action=transition_type)
    with transaction.atomic():
        claimed_work = _lock_optional_work(data.work_claim, request)
        if data.work_claim is None:
            _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
        candidate = _lock_candidate(request, data.candidate_fence)
        if claimed_work is not None:
            _require_claimed_candidate_work(claimed_work, candidate)
        candidate_sources = _source_rows(candidate)
        fences = (data.memory_fence,)
        memories, versions = _lock_declared_memories(request, fences)
        _lock_exact_documents(versions)
        existing = _existing_transition(request, fingerprint=fingerprint, subject_id=candidate.id)
        if existing is not None:
            return _transition_result(existing, duplicate=True)
        _candidate_fence(
            candidate,
            data.candidate_fence,
            candidate_sources,
            allowed_source_kinds=_NON_PROMOTION_SOURCE_KINDS,
        )
        _require_no_open_conflict(candidate)
        _require_proposed_candidate(candidate)
        if not candidate_sources:
            raise MemoryTransitionError('provenance', 'candidate transition requires non-empty provenance')
        _verify_memory_fences(memories, fences)
        memory = memories[data.memory_fence.memory_id]
        prior_version = versions[memory.id]
        _require_active_memory(memory)
        sanitized_view = _revalidated_sanitized_view(
            candidate,
            sanitized_title=data.sanitized_title,
            sanitized_body=data.sanitized_body,
        )
        result_title = sanitized_view.title if sanitized_view is not None else data.title
        result_body = sanitized_view.body if sanitized_view is not None else data.body
        version, version_sources = _create_revision_version(
            memory,
            prior_version,
            title=result_title,
            body=result_body,
            candidate_sources=candidate_sources,
        )
        transition_id = uuid.uuid4()
        document = _write_exact(
            memory=memory,
            version=version,
            transition_id=transition_id,
            sources=version_sources,
        )
        _fault_boundary('exact_document')
        embedding_work = _embedding_for_active_result(memory, document)
        provenance_hash = memory_version_provenance_hash(version_sources)
        transition_row = _commit_transition(
            request=request,
            transition_id=transition_id,
            transition_type=transition_type,
            fingerprint=fingerprint,
            candidate=candidate,
            memory=memory,
            from_version=prior_version,
            to_version=version,
            result_memory=memory,
            result_version=version,
            exact_document=document,
            result_exact_document=document,
            embedding_work=embedding_work,
            provenance_hash=provenance_hash,
        )
        _advance_memory_pointer(memory, transition_row, version)
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

        return _transition_result(transition_row)


class ReviseMemoryFromCandidate:
    def execute(self, data: ReviseMemoryFromCandidateInput) -> MemoryTransitionResult:
        return _execute_candidate_revision(data, transition_type=MemoryTransitionType.REVISE)


class MergeMemoryCandidate:
    def execute(self, data: MergeMemoryCandidateInput) -> MemoryTransitionResult:
        return _execute_candidate_revision(data, transition_type=MemoryTransitionType.MERGE)


class MergeMemories:
    def execute(self, data: MergeMemoriesInput) -> MemoryTransitionResult:
        _reject_existing_memory_work_claim(data.work_claim)
        request = data.request
        if data.source_memory_fence.memory_id == data.result_memory_fence.memory_id:
            raise MemoryTransitionError('memory_state', 'merge requires two distinct memories')
        fences = (data.source_memory_fence, data.result_memory_fence)
        fingerprint = _request_fingerprint(
            request,
            action='merge',
            subject_id=data.source_memory_fence.memory_id,
            command={
                'source_memory_fence': _memory_fence_value(data.source_memory_fence),
                'result_memory_fence': _memory_fence_value(data.result_memory_fence),
                'title': data.title,
                'body': data.body,
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            memories, versions = _lock_declared_memories(request, fences)
            _lock_exact_documents(versions)
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _verify_memory_fences(memories, fences)
            source = memories[data.source_memory_fence.memory_id]
            result = memories[data.result_memory_fence.memory_id]
            source_version = versions[source.id]
            prior_result_version = versions[result.id]
            _require_active_memory(source)
            _require_active_memory(result)
            version, version_sources = _create_revision_version(
                result,
                prior_result_version,
                title=data.title,
                body=data.body,
                extra_source_versions=(source_version,),
            )
            source_sources = _current_version_sources(source_version)
            transition_id = uuid.uuid4()
            source_document = _write_exact(
                memory=source,
                version=source_version,
                transition_id=transition_id,
                sources=source_sources,
            )
            result_document = _write_exact(
                memory=result,
                version=version,
                transition_id=transition_id,
                sources=version_sources,
            )
            _fault_boundary('exact_document')
            source_embedding_work, _source_work_created = _create_embedding(source_document)
            embedding_work = _embedding_for_active_result(result, result_document)
            link = _create_semantic_link(source=source, result=result, link_type=LinkType.NARROWED_BY)
            provenance_hash = memory_version_provenance_hash(version_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.MERGE,
                fingerprint=fingerprint,
                memory=source,
                from_version=source_version,
                to_version=source_version,
                result_memory=result,
                result_version=version,
                exact_document=source_document,
                result_exact_document=result_document,
                embedding_work=embedding_work,
                semantic_link=link,
                provenance_hash=provenance_hash,
                audit_ids={'affected_embedding_work_id': source_embedding_work.id},
            )
            _advance_memory_pointer(source, transition_row, source_version)
            _advance_memory_pointer(result, transition_row, version)
            _fault_boundary('candidate_pointer')

            return _transition_result(transition_row)


class SupersedeMemoryWithCandidate:
    def execute(self, data: SupersedeMemoryWithCandidateInput) -> MemoryTransitionResult:
        request = data.request
        fingerprint = _request_fingerprint(
            request,
            action='supersede',
            subject_id=data.candidate_fence.candidate_id,
            command={
                'candidate_fence': _candidate_fence_value(data.candidate_fence),
                'loser_memory_fence': _memory_fence_value(data.loser_memory_fence),
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            claimed_work = _lock_optional_work(data.work_claim, request)
            if data.work_claim is None:
                _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
            candidate = _lock_candidate(request, data.candidate_fence)
            if claimed_work is not None:
                _require_claimed_candidate_work(claimed_work, candidate)
            candidate_sources = _source_rows(candidate)
            fences = (data.loser_memory_fence,)
            memories, versions = _lock_declared_memories(request, fences)
            _lock_exact_documents(versions)
            existing = _existing_transition(request, fingerprint=fingerprint, subject_id=candidate.id)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _candidate_fence(
                candidate,
                data.candidate_fence,
                candidate_sources,
                allowed_source_kinds=_NON_PROMOTION_SOURCE_KINDS,
            )
            _require_no_open_conflict(candidate)
            _require_proposed_candidate(candidate)
            if not candidate_sources:
                raise MemoryTransitionError('provenance', 'candidate supersession requires non-empty provenance')
            _verify_memory_fences(memories, fences)
            loser = memories[data.loser_memory_fence.memory_id]
            loser_version = versions[loser.id]
            _require_active_memory(loser)
            sanitized_view = _revalidated_sanitized_view(
                candidate,
                sanitized_title=data.sanitized_title,
                sanitized_body=data.sanitized_body,
            )
            effective_scope = _revalidated_effective_scope(
                candidate,
                candidate_sources,
                effective_visibility_scope=data.effective_visibility_scope,
                effective_team_id=data.effective_team_id,
            )
            result, result_version, result_sources = _create_candidate_memory(
                candidate,
                candidate_sources,
                title=sanitized_view.title if sanitized_view is not None else None,
                body=sanitized_view.body if sanitized_view is not None else None,
                scope_override=effective_scope,
                sanitize_metadata=sanitized_view is not None,
            )
            loser.stale = True
            loser_sources = _current_version_sources(loser_version)
            transition_id = uuid.uuid4()
            loser_document = _write_exact(
                memory=loser,
                version=loser_version,
                transition_id=transition_id,
                sources=loser_sources,
            )
            result_document = _write_exact(
                memory=result,
                version=result_version,
                transition_id=transition_id,
                sources=result_sources,
            )
            _fault_boundary('exact_document')
            embedding_work = _embedding_for_active_result(result, result_document)
            link = _create_semantic_link(source=loser, result=result, link_type=LinkType.SUPERSEDED_BY)
            provenance_hash = memory_version_provenance_hash(result_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.SUPERSEDE,
                fingerprint=fingerprint,
                candidate=candidate,
                memory=loser,
                from_version=loser_version,
                to_version=loser_version,
                result_memory=result,
                result_version=result_version,
                exact_document=loser_document,
                result_exact_document=result_document,
                embedding_work=embedding_work,
                semantic_link=link,
                provenance_hash=provenance_hash,
            )
            _advance_memory_pointer(loser, transition_row, loser_version)
            _advance_memory_pointer(result, transition_row, result_version)
            candidate.status = CandidateStatus.PROMOTED
            candidate.promoted_memory_id = result.id
            candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])
            _fault_boundary('candidate_pointer')
            _finish_candidate_work(
                candidate,
                claim=data.work_claim,
                claimed_work=claimed_work,
                result_memory_id=result.id,
            )

            return _transition_result(transition_row)


class SupersedeMemories:
    def execute(self, data: SupersedeMemoriesInput) -> MemoryTransitionResult:
        _reject_existing_memory_work_claim(data.work_claim)
        request = data.request
        if data.source_memory_fence.memory_id == data.result_memory_fence.memory_id:
            raise MemoryTransitionError('memory_state', 'supersession requires two distinct memories')
        fences = (data.source_memory_fence, data.result_memory_fence)
        fingerprint = _request_fingerprint(
            request,
            action='supersede',
            subject_id=data.source_memory_fence.memory_id,
            command={
                'source_memory_fence': _memory_fence_value(data.source_memory_fence),
                'result_memory_fence': _memory_fence_value(data.result_memory_fence),
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            memories, versions = _lock_declared_memories(request, fences)
            documents = _lock_exact_documents(versions)
            existing = _existing_transition(request, fingerprint=fingerprint)
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            _verify_memory_fences(memories, fences)
            source = memories[data.source_memory_fence.memory_id]
            result = memories[data.result_memory_fence.memory_id]
            source_version = versions[source.id]
            result_version = versions[result.id]
            _require_active_memory(source)
            _require_active_memory(result)
            source.stale = True
            source_sources = _current_version_sources(source_version)
            result_sources = _current_version_sources(result_version)
            transition_id = uuid.uuid4()
            source_document = _write_exact(
                memory=source,
                version=source_version,
                transition_id=transition_id,
                sources=source_sources,
            )
            _fault_boundary('exact_document')
            result_document = documents[result.id]
            link = _create_semantic_link(source=source, result=result, link_type=LinkType.SUPERSEDED_BY)
            provenance_hash = memory_version_provenance_hash(result_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.SUPERSEDE,
                fingerprint=fingerprint,
                memory=source,
                from_version=source_version,
                to_version=source_version,
                result_memory=result,
                result_version=result_version,
                exact_document=source_document,
                result_exact_document=result_document,
                semantic_link=link,
                provenance_hash=provenance_hash,
            )
            _advance_memory_pointer(source, transition_row, source_version)
            _fault_boundary('candidate_pointer')

            return _transition_result(transition_row)


def _apply_memory_state(memory: Memory, transition_type: str) -> None:
    if transition_type == MemoryTransitionType.MARK_STALE:
        _require_active_memory(memory)
        memory.stale = True
        return
    if transition_type == MemoryTransitionType.REFUTE:
        if memory.status == MemoryStatus.ARCHIVED or memory.refuted:
            raise MemoryTransitionError('memory_state', 'memory cannot be refuted from its current state')
        memory.status = MemoryStatus.REFUTED
        memory.refuted = True
        return
    if transition_type == MemoryTransitionType.RESTORE:
        if memory.status == MemoryStatus.APPROVED and not memory.stale and not memory.refuted:
            raise MemoryTransitionError('memory_state', 'active memory does not require restoration')
        memory.status = MemoryStatus.APPROVED
        memory.stale = False
        memory.refuted = False
        return
    if transition_type == MemoryTransitionType.ARCHIVE:
        if memory.status == MemoryStatus.ARCHIVED:
            raise MemoryTransitionError('memory_state', 'memory is already archived')
        memory.status = MemoryStatus.ARCHIVED
        memory.stale = True
        return
    raise MemoryTransitionError('command', f'unsupported memory state transition {transition_type}')


def _execute_memory_state(data: MemoryStateInput, *, transition_type: str) -> MemoryTransitionResult:
    _reject_existing_memory_work_claim(data.work_claim)
    request = data.request
    fingerprint = _request_fingerprint(
        request,
        action=transition_type,
        subject_id=data.memory_fence.memory_id,
        command={
            'memory_fence': _memory_fence_value(data.memory_fence),
            'work_claim': _work_claim_value(data.work_claim),
        },
    )
    fences = (data.memory_fence,)
    with transaction.atomic():
        memories, versions = _lock_declared_memories(request, fences)
        _lock_exact_documents(versions)
        existing = _existing_transition(request, fingerprint=fingerprint)
        if existing is not None:
            return _transition_result(existing, duplicate=True)
        _verify_memory_fences(memories, fences)
        memory = memories[data.memory_fence.memory_id]
        version = versions[memory.id]
        _apply_memory_state(memory, transition_type)
        version_sources = _current_version_sources(version)
        transition_id = uuid.uuid4()
        document = _write_exact(
            memory=memory,
            version=version,
            transition_id=transition_id,
            sources=version_sources,
        )
        _fault_boundary('exact_document')
        embedding_work = _embedding_for_active_result(memory, document)
        provenance_hash = memory_version_provenance_hash(version_sources)
        transition_row = _commit_transition(
            request=request,
            transition_id=transition_id,
            transition_type=transition_type,
            fingerprint=fingerprint,
            memory=memory,
            from_version=version,
            to_version=version,
            result_memory=memory,
            result_version=version,
            exact_document=document,
            result_exact_document=document,
            embedding_work=embedding_work,
            provenance_hash=provenance_hash,
        )
        _advance_memory_pointer(memory, transition_row, version)
        _fault_boundary('candidate_pointer')

        return _transition_result(transition_row)


class MarkMemoryStale:
    def execute(self, data: MemoryStateInput) -> MemoryTransitionResult:
        return _execute_memory_state(data, transition_type=MemoryTransitionType.MARK_STALE)


class RefuteMemory:
    def execute(self, data: MemoryStateInput) -> MemoryTransitionResult:
        return _execute_memory_state(data, transition_type=MemoryTransitionType.REFUTE)


class RestoreMemory:
    def execute(self, data: MemoryStateInput) -> MemoryTransitionResult:
        return _execute_memory_state(data, transition_type=MemoryTransitionType.RESTORE)


class ArchiveMemory:
    def execute(self, data: MemoryStateInput) -> MemoryTransitionResult:
        return _execute_memory_state(data, transition_type=MemoryTransitionType.ARCHIVE)


class OpenMemoryConflict:
    def execute(self, data: OpenMemoryConflictInput) -> MemoryConflict:
        request = data.request
        fingerprint = _request_fingerprint(
            request,
            action='conflict_open',
            subject_id=data.candidate_fence.candidate_id,
            command={
                'candidate_fence': _candidate_fence_value(data.candidate_fence),
                'memory_fence': _memory_fence_value(data.memory_fence),
                'evidence_hash': data.evidence_hash,
                'redacted_reason': data.redacted_reason,
                'work_claim': _work_claim_value(data.work_claim),
            },
        )
        with transaction.atomic():
            claimed_work = _lock_optional_work(data.work_claim, request)
            if data.work_claim is None:
                _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
            candidate = _lock_candidate(request, data.candidate_fence)
            if claimed_work is not None:
                _require_claimed_candidate_work(claimed_work, candidate)
            candidate_sources = _source_rows(candidate)
            fences = (data.memory_fence,)
            memories, versions = _lock_declared_memories(request, fences)
            memory = memories[data.memory_fence.memory_id]
            locked_conflicts = list(
                MemoryConflict.objects.select_for_update()
                .filter(candidate_id=candidate.id, memory_id=memory.id)
                .order_by('id')
            )
            documents = _lock_exact_documents(versions)
            existing = _existing_transition(request, fingerprint=fingerprint, subject_id=candidate.id)
            if existing is not None:
                try:
                    return MemoryConflict.objects.get(opened_transition_id=existing.id)
                except MemoryConflict.DoesNotExist as error:
                    raise MemoryTransitionError(
                        'conflict_state',
                        'conflict transition has no durable conflict',
                    ) from error
            _candidate_fence(
                candidate,
                data.candidate_fence,
                candidate_sources,
                allowed_source_kinds=_NON_PROMOTION_SOURCE_KINDS,
            )
            _require_proposed_candidate(candidate)
            if not candidate_sources:
                raise MemoryTransitionError('provenance', 'conflict candidate requires non-empty provenance')
            _verify_memory_fences(memories, fences)
            _require_active_memory(memory)
            _require_sha256(data.evidence_hash, field='conflict evidence hash')
            if locked_conflicts:
                raise MemoryTransitionError('conflict_exists', 'candidate and memory already have conflict evidence')
            version = versions[memory.id]
            version_sources = _current_version_sources(version)
            document = documents[memory.id]
            link = _create_conflict_link(candidate=candidate, memory=memory)
            transition_id = uuid.uuid4()
            provenance_hash = memory_version_provenance_hash(version_sources)
            transition_row = _commit_transition(
                request=request,
                transition_id=transition_id,
                transition_type=MemoryTransitionType.CONFLICT_OPEN,
                fingerprint=fingerprint,
                candidate=candidate,
                memory=memory,
                from_version=version,
                to_version=version,
                result_memory=memory,
                result_version=version,
                exact_document=document,
                result_exact_document=document,
                semantic_link=link,
                provenance_hash=provenance_hash,
                audit_ids={
                    'conflict_evidence_hash': data.evidence_hash,
                    'conflict_reason': data.redacted_reason,
                },
            )
            conflict = MemoryConflict.objects.create(
                organization_id=memory.organization_id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                candidate=candidate,
                memory=memory,
                memory_version=version,
                semantic_link=link,
                opened_transition=transition_row,
                evidence_hash=data.evidence_hash,
            )
            _fault_boundary('conflict')
            _finish_candidate_work(
                candidate,
                claim=data.work_claim,
                claimed_work=claimed_work,
                result_memory_id=None,
                completion='product_succeeded',
            )

            return conflict


@dataclass(slots=True)
class _ConflictResolutionLocks:
    claimed_work: WorkflowWork | None
    candidate: MemoryCandidate
    candidate_sources: list[MemoryCandidateSource]
    memories: dict[uuid.UUID, Memory]
    versions: dict[uuid.UUID, MemoryVersion]
    conflicts: list[MemoryConflict]
    documents: dict[uuid.UUID, RetrievalDocument]


@dataclass(slots=True)
class _ConflictResolutionOutcome:
    affected_memory: Memory
    affected_version: MemoryVersion
    affected_document: RetrievalDocument
    result_memory: Memory
    result_version: MemoryVersion
    result_document: RetrievalDocument
    embedding_work: WorkflowWork | None
    semantic_link: MemoryLink | None
    provenance_hash: str
    pointer_memories: tuple[tuple[Memory, MemoryVersion], ...]


def _normalize_conflict_resolution(
    data: ResolveMemoryConflictInput,
) -> tuple[tuple[uuid.UUID, ...], tuple[MemoryFence, ...]]:
    if data.resolution not in MemoryConflictResolution.values:
        raise MemoryTransitionError('command', 'unsupported conflict resolution')
    if not data.conflict_ids or len(set(data.conflict_ids)) != len(data.conflict_ids):
        raise MemoryTransitionError('stale_decision', 'conflict resolution requires a distinct non-empty set')
    if len(data.conflict_memory_fences) != len(data.conflict_ids):
        raise MemoryTransitionError('stale_decision', 'conflict ids and memory fences must be complete')

    return (
        tuple(sorted(data.conflict_ids, key=str)),
        tuple(sorted(data.conflict_memory_fences, key=lambda fence: str(fence.memory_id))),
    )


def _conflict_resolution_fingerprint(
    data: ResolveMemoryConflictInput,
    conflict_ids: tuple[uuid.UUID, ...],
    fences: tuple[MemoryFence, ...],
) -> str:
    return _request_fingerprint(
        data.request,
        action='conflict_resolve',
        subject_id=data.candidate_fence.candidate_id,
        command={
            'candidate_fence': _candidate_fence_value(data.candidate_fence),
            'conflict_ids': [str(conflict_id) for conflict_id in conflict_ids],
            'conflict_memory_fences': [_memory_fence_value(fence) for fence in fences],
            'resolution': data.resolution,
            'selected_memory_fence': (
                _memory_fence_value(data.selected_memory_fence) if data.selected_memory_fence else None
            ),
            'title': data.title,
            'body': data.body,
            'work_claim': _work_claim_value(data.work_claim),
        },
    )


def _lock_conflict_resolution_rows(
    data: ResolveMemoryConflictInput,
    conflict_ids: tuple[uuid.UUID, ...],
    fences: tuple[MemoryFence, ...],
) -> _ConflictResolutionLocks:
    request = data.request
    claimed_work = _lock_optional_work(data.work_claim, request)
    if data.work_claim is None:
        _lock_unclaimed_candidate_work(request, data.candidate_fence.candidate_id)
    candidate = _lock_candidate(request, data.candidate_fence)
    if claimed_work is not None:
        _require_claimed_candidate_work(claimed_work, candidate)
    candidate_sources = _source_rows(candidate)
    memories, versions = _lock_declared_memories(request, fences)
    conflicts = list(MemoryConflict.objects.select_for_update().filter(id__in=conflict_ids).order_by('id'))
    documents = _lock_exact_documents(versions)

    return _ConflictResolutionLocks(
        claimed_work=claimed_work,
        candidate=candidate,
        candidate_sources=candidate_sources,
        memories=memories,
        versions=versions,
        conflicts=conflicts,
        documents=documents,
    )


def _validate_conflict_resolution_rows(
    data: ResolveMemoryConflictInput,
    conflict_ids: tuple[uuid.UUID, ...],
    fences: tuple[MemoryFence, ...],
    locked: _ConflictResolutionLocks,
) -> dict[uuid.UUID, MemoryFence]:
    _candidate_fence(
        locked.candidate,
        data.candidate_fence,
        locked.candidate_sources,
        allowed_source_kinds=_NON_PROMOTION_SOURCE_KINDS,
    )
    _require_proposed_candidate(locked.candidate)
    if not locked.candidate_sources:
        raise MemoryTransitionError('provenance', 'conflict candidate requires non-empty provenance')
    _verify_memory_fences(locked.memories, fences)
    if len(locked.conflicts) != len(conflict_ids):
        raise MemoryTransitionError('stale_decision', 'declared conflict set no longer exists', retryable=True)
    current_open_ids = tuple(
        MemoryConflict.objects.filter(candidate_id=locked.candidate.id, resolved_transition__isnull=True)
        .order_by('id')
        .values_list('id', flat=True)
    )
    if current_open_ids != conflict_ids:
        raise MemoryTransitionError(
            'stale_decision',
            'declared conflicts are not the complete open set',
            retryable=True,
        )
    fence_by_memory = {fence.memory_id: fence for fence in fences}
    if len(fence_by_memory) != len(fences):
        raise MemoryTransitionError(
            'stale_decision',
            'conflict memory fences must be distinct',
            retryable=True,
        )
    for conflict in locked.conflicts:
        if not _scope_matches(conflict, data.request.scope) or conflict.candidate_id != locked.candidate.id:
            raise MemoryTransitionError('scope', 'conflict is outside the declared scope')
        fence = fence_by_memory.get(conflict.memory_id)
        if fence is None or conflict.memory_version_id != fence.current_version_id:
            raise MemoryTransitionError(
                'stale_decision',
                'conflict memory version has drifted',
                retryable=True,
            )

    return fence_by_memory


def _selected_conflict_target(
    data: ResolveMemoryConflictInput,
    locked: _ConflictResolutionLocks,
    fence_by_memory: dict[uuid.UUID, MemoryFence],
) -> tuple[Memory | None, MemoryVersion | None]:
    requires_selected = data.resolution in (
        MemoryConflictResolution.MERGE_CANDIDATE,
        MemoryConflictResolution.SUPERSEDE_MEMORY,
    )
    if requires_selected:
        if data.selected_memory_fence is None:
            raise MemoryTransitionError('command', 'selected memory is required for this resolution')
        declared_selected = fence_by_memory.get(data.selected_memory_fence.memory_id)
        if declared_selected != data.selected_memory_fence:
            raise MemoryTransitionError(
                'stale_decision',
                'selected memory is not in the conflict set',
                retryable=True,
            )
        memory = locked.memories[data.selected_memory_fence.memory_id]
        _require_active_memory(memory)
        return memory, locked.versions[memory.id]
    if data.selected_memory_fence is not None:
        raise MemoryTransitionError('command', 'selected memory is not allowed for this resolution')

    return None, None


def _validate_conflict_resolution_content(data: ResolveMemoryConflictInput) -> None:
    if data.resolution != MemoryConflictResolution.REJECT_CANDIDATE and (not data.title or not data.body):
        raise MemoryTransitionError('command', 'published conflict outcomes require title and body')


def _publish_conflict_outcome(
    data: ResolveMemoryConflictInput,
    locked: _ConflictResolutionLocks,
    transition_id: uuid.UUID,
    base: _ConflictResolutionOutcome,
) -> _ConflictResolutionOutcome:
    memory, version, sources = _create_candidate_memory(
        locked.candidate,
        locked.candidate_sources,
        title=data.title,
        body=data.body,
    )
    document = _write_exact(memory=memory, version=version, transition_id=transition_id, sources=sources)
    _fault_boundary('exact_document')
    embedding_work = _embedding_for_active_result(memory, document)

    return _ConflictResolutionOutcome(
        affected_memory=base.affected_memory,
        affected_version=base.affected_version,
        affected_document=base.affected_document,
        result_memory=memory,
        result_version=version,
        result_document=document,
        embedding_work=embedding_work,
        semantic_link=None,
        provenance_hash=memory_version_provenance_hash(sources),
        pointer_memories=((memory, version),),
    )


def _merge_conflict_outcome(
    data: ResolveMemoryConflictInput,
    locked: _ConflictResolutionLocks,
    transition_id: uuid.UUID,
    selected_memory: Memory,
    selected_version: MemoryVersion,
) -> _ConflictResolutionOutcome:
    version, sources = _create_revision_version(
        selected_memory,
        selected_version,
        title=data.title or '',
        body=data.body or '',
        candidate_sources=locked.candidate_sources,
    )
    document = _write_exact(memory=selected_memory, version=version, transition_id=transition_id, sources=sources)
    _fault_boundary('exact_document')
    embedding_work = _embedding_for_active_result(selected_memory, document)

    return _ConflictResolutionOutcome(
        affected_memory=selected_memory,
        affected_version=selected_version,
        affected_document=document,
        result_memory=selected_memory,
        result_version=version,
        result_document=document,
        embedding_work=embedding_work,
        semantic_link=None,
        provenance_hash=memory_version_provenance_hash(sources),
        pointer_memories=((selected_memory, version),),
    )


def _supersede_conflict_outcome(
    data: ResolveMemoryConflictInput,
    locked: _ConflictResolutionLocks,
    transition_id: uuid.UUID,
    selected_memory: Memory,
    selected_version: MemoryVersion,
) -> _ConflictResolutionOutcome:
    selected_memory.stale = True
    loser_sources = _current_version_sources(selected_version)
    result_memory, result_version, result_sources = _create_candidate_memory(
        locked.candidate,
        locked.candidate_sources,
        title=data.title,
        body=data.body,
    )
    affected_document = _write_exact(
        memory=selected_memory,
        version=selected_version,
        transition_id=transition_id,
        sources=loser_sources,
    )
    result_document = _write_exact(
        memory=result_memory,
        version=result_version,
        transition_id=transition_id,
        sources=result_sources,
    )
    _fault_boundary('exact_document')
    embedding_work = _embedding_for_active_result(result_memory, result_document)
    link = _create_semantic_link(
        source=selected_memory,
        result=result_memory,
        link_type=LinkType.SUPERSEDED_BY,
    )

    return _ConflictResolutionOutcome(
        affected_memory=selected_memory,
        affected_version=selected_version,
        affected_document=affected_document,
        result_memory=result_memory,
        result_version=result_version,
        result_document=result_document,
        embedding_work=embedding_work,
        semantic_link=link,
        provenance_hash=memory_version_provenance_hash(result_sources),
        pointer_memories=((selected_memory, selected_version), (result_memory, result_version)),
    )


def _reject_conflict_outcome(locked: _ConflictResolutionLocks) -> _ConflictResolutionOutcome:
    first_conflict = locked.conflicts[0]
    memory = locked.memories[first_conflict.memory_id]
    version = locked.versions[memory.id]
    document = locked.documents[memory.id]
    sources = _current_version_sources(version)

    return _ConflictResolutionOutcome(
        affected_memory=memory,
        affected_version=version,
        affected_document=document,
        result_memory=memory,
        result_version=version,
        result_document=document,
        embedding_work=None,
        semantic_link=None,
        provenance_hash=memory_version_provenance_hash(sources),
        pointer_memories=(),
    )


def _build_conflict_resolution_outcome(
    data: ResolveMemoryConflictInput,
    locked: _ConflictResolutionLocks,
    transition_id: uuid.UUID,
    selected_memory: Memory | None,
    selected_version: MemoryVersion | None,
) -> _ConflictResolutionOutcome:
    base = _reject_conflict_outcome(locked)
    if data.resolution == MemoryConflictResolution.PUBLISH_CANDIDATE:
        return _publish_conflict_outcome(data, locked, transition_id, base)
    if data.resolution == MemoryConflictResolution.MERGE_CANDIDATE:
        if selected_memory is None or selected_version is None:
            raise MemoryTransitionError('command', 'selected merge memory is missing')
        return _merge_conflict_outcome(data, locked, transition_id, selected_memory, selected_version)
    if data.resolution == MemoryConflictResolution.SUPERSEDE_MEMORY:
        if selected_memory is None or selected_version is None:
            raise MemoryTransitionError('command', 'selected superseded memory is missing')
        return _supersede_conflict_outcome(data, locked, transition_id, selected_memory, selected_version)

    return base


def _commit_conflict_resolution(
    data: ResolveMemoryConflictInput,
    conflict_ids: tuple[uuid.UUID, ...],
    fingerprint: str,
    transition_id: uuid.UUID,
    locked: _ConflictResolutionLocks,
    outcome: _ConflictResolutionOutcome,
) -> MemoryTransitionResult:
    to_version = (
        outcome.result_version if outcome.affected_memory.id == outcome.result_memory.id else outcome.affected_version
    )
    transition_row = _commit_transition(
        request=data.request,
        transition_id=transition_id,
        transition_type=MemoryTransitionType.CONFLICT_RESOLVE,
        fingerprint=fingerprint,
        candidate=locked.candidate,
        memory=outcome.affected_memory,
        from_version=outcome.affected_version,
        to_version=to_version,
        result_memory=outcome.result_memory,
        result_version=outcome.result_version,
        exact_document=outcome.affected_document,
        result_exact_document=outcome.result_document,
        embedding_work=outcome.embedding_work,
        semantic_link=outcome.semantic_link,
        provenance_hash=outcome.provenance_hash,
        audit_ids={
            'conflict_ids': ','.join(str(conflict_id) for conflict_id in conflict_ids),
            'resolution': data.resolution,
        },
    )
    for memory, version in outcome.pointer_memories:
        _advance_memory_pointer(memory, transition_row, version)
    rejected = data.resolution == MemoryConflictResolution.REJECT_CANDIDATE
    locked.candidate.status = CandidateStatus.REJECTED if rejected else CandidateStatus.PROMOTED
    locked.candidate.promoted_memory_id = None if rejected else outcome.result_memory.id
    locked.candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])
    _fault_boundary('resolution')
    closed = MemoryConflict.objects.filter(
        id__in=conflict_ids,
        resolved_transition__isnull=True,
    ).update(
        resolved_transition=transition_row,
        resolution=data.resolution,
        resolved_at=timezone.now(),
    )
    if closed != len(conflict_ids):
        raise MemoryTransitionError('stale_decision', 'conflict set changed while resolving', retryable=True)
    _fault_boundary('conflict')
    _finish_candidate_work(
        locked.candidate,
        claim=data.work_claim,
        claimed_work=locked.claimed_work,
        result_memory_id=None if rejected else outcome.result_memory.id,
    )

    return _transition_result(transition_row)


class ResolveMemoryConflict:
    def execute(self, data: ResolveMemoryConflictInput) -> MemoryTransitionResult:
        conflict_ids, fences = _normalize_conflict_resolution(data)
        fingerprint = _conflict_resolution_fingerprint(data, conflict_ids, fences)
        with transaction.atomic():
            locked = _lock_conflict_resolution_rows(data, conflict_ids, fences)
            existing = _existing_transition(
                data.request,
                fingerprint=fingerprint,
                subject_id=locked.candidate.id,
            )
            if existing is not None:
                return _transition_result(existing, duplicate=True)
            fence_by_memory = _validate_conflict_resolution_rows(data, conflict_ids, fences, locked)
            selected_memory, selected_version = _selected_conflict_target(data, locked, fence_by_memory)
            _validate_conflict_resolution_content(data)
            transition_id = uuid.uuid4()
            outcome = _build_conflict_resolution_outcome(
                data,
                locked,
                transition_id,
                selected_memory,
                selected_version,
            )
            return _commit_conflict_resolution(
                data,
                conflict_ids,
                fingerprint,
                transition_id,
                locked,
                outcome,
            )
