from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.context.services import authorized_retrieval_documents, cosine_similarity
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    CurationDecision,
    CurationOutcome,
    EvidenceTier,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    RetrievalDocument,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkResolutionReason,
)
from engram.memory.candidate_decision_work import (
    build_candidate_decision_input,
    candidate_decision_snapshot,
)
from engram.memory.candidate_parsing import strip_json_fence
from engram.memory.conflict_links import clear_candidate_conflict_links
from engram.memory.curation_judge import (
    CurationEvidenceContext,
    CurationJudgeError,
    CurationJudgeInput,
    CurationJudgeResult,
    JudgeCurationCandidate,
)
from engram.memory.curation_judge import (
    build_curation_evidence_context as _judge_build_curation_evidence_context,
)
from engram.memory.curation_shortlist import (
    BuildCurationShortlist,
    BuildCurationShortlistInput,
    CurationShortlist,
    CurationShortlistError,
    revalidate_curation_shortlist,
)
from engram.memory.deterministic_gates import (
    DeterministicGateDisposition,
    DeterministicGateResult,
    DeterministicTerminalOutcome,
    EffectiveCandidateScope,
    EvaluateDeterministicCandidateGates,
    SanitizedCandidateView,
)
from engram.memory.escalation import escalation_reason
from engram.memory.import_provenance import candidate_evidence_manifest
from engram.memory.services import (
    MemoryWorkerError,
    redact_text,
    redact_value,
)
from engram.memory.services import (
    PromoteMemoryCandidate as _LegacyPromoteMemoryCandidate,
)
from engram.memory.services import (
    PromoteMemoryCandidateInput as _LegacyPromoteMemoryCandidateInput,
)
from engram.memory.transitions import (
    CandidateFence,
    MemoryFence,
    MemoryTransitionError,
    MergeMemoryCandidate,
    MergeMemoryCandidateInput,
    OpenMemoryConflict,
    OpenMemoryConflictInput,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    ReviseMemoryFromCandidate,
    ReviseMemoryFromCandidateInput,
    SupersedeMemoryWithCandidate,
    SupersedeMemoryWithCandidateInput,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)
from engram.memory.work_execution import WorkClaim, finish_work_claim, lock_work_fence
from engram.memory.workflow_work import canonical_json_bytes
from engram.model_policy.models import ModelPolicy
from engram.model_policy.services import (
    EmbeddingCallInput,
    ModelPolicyError,
    ProviderCallInput,
    ProviderCallResult,
    ProviderSecretError,
    ResolvedModelPolicy,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CurateMemoryCandidateInput:
    candidate_id: uuid.UUID
    correlation_id: str = ''


@dataclass(frozen=True)
class CurateMemoryCandidateResult:
    decision: str
    candidate: MemoryCandidate
    memory: Memory | None = None
    memory_version: MemoryVersion | None = None
    retrieval_document: RetrievalDocument | None = None
    superseded_memory: Memory | None = None
    duplicate: bool = False
    near_dup_score: float | None = None


@dataclass(frozen=True)
class _JudgeOutcome:
    decision: str
    reason: str
    judge_context: dict[str, object] | None = None


_GRAY_BAND_WIDTH = Decimal('0.10')
_JUDGE_DECISIONS = frozenset({'merge', 'keep_both', 'reject', 'contradicts'})
_DEFAULT_JUDGE_DECISION = 'keep_both'
_EVIDENCE_ID_KEY_SUFFIXES = ('_id', '_ids')
_MAX_EVIDENCE_SOURCE_IDS = 50


def _evidence_entry_ids(entry: object) -> list[str]:
    if isinstance(entry, str):
        return [entry]

    if not isinstance(entry, dict):
        return []

    if entry.get('type') == 'conflict':
        return []

    ids: list[str] = []
    for key, value in entry.items():
        if not str(key).endswith(_EVIDENCE_ID_KEY_SUFFIXES):
            continue
        if isinstance(value, list | tuple):
            ids.extend(str(item) for item in value if item)
        elif value:
            ids.append(str(value))

    return ids


def _evidence_source_ids(candidate: MemoryCandidate) -> list[str]:
    collected: list[str] = []
    for entry in candidate.evidence:
        collected.extend(_evidence_entry_ids(entry))

    unique: list[str] = []
    seen: set[str] = set()
    for item in collected:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
        if len(unique) >= _MAX_EVIDENCE_SOURCE_IDS:
            break

    return unique


def _audit_curator_action(
    *,
    candidate: MemoryCandidate,
    event_type: str,
    decision: str,
    reason: str = '',
    near_dup_score: float | None = None,
    threshold: Decimal | None = None,
    judge_context: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
    target_type: str = 'memory_candidate',
    target_id: str | None = None,
    request_id: str = '',
    correlation_id: str = '',
) -> None:
    metadata: dict[str, object] = {
        'candidate_id': str(candidate.id),
        'decision': decision,
        'reason': reason,
        'near_dup_score': f'{near_dup_score:.2f}' if near_dup_score is not None else None,
        'threshold': str(threshold) if threshold is not None else None,
        'source_observation_id': str(candidate.source_observation_id) if candidate.source_observation_id else None,
        'evidence_source_ids': _evidence_source_ids(candidate),
    }
    if judge_context:
        metadata['judge'] = judge_context
    if extra:
        metadata.update(extra)

    AuditEvent.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        event_type=event_type,
        actor_type='system',
        actor_id='curator',
        target_type=target_type,
        target_id=target_id if target_id is not None else str(candidate.id),
        capability='memories:review',
        result=AuditResult.RECORDED,
        request_id=request_id,
        correlation_id=correlation_id,
        metadata=redact_value(metadata),
    )


def _judge_context_snapshot(title: str, body: str) -> dict[str, object]:
    return {
        'title': redact_text(title)[:120],
        'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
        'body_length': len(body),
    }


def _build_judge_context(
    policy: ModelPolicy,
    result: ProviderCallResult,
    candidate: MemoryCandidate,
    memory: Memory,
) -> dict[str, object]:
    return {
        'policy_id': str(policy.id),
        'policy_version': policy.version,
        'provider': result.provider,
        'model': result.model,
        'provider_call_record_id': str(result.call_record_id),
        'candidate': _judge_context_snapshot(candidate.title, candidate.body),
        'existing_memory': {
            'memory_id': str(memory.id),
            **_judge_context_snapshot(memory.title, memory.body),
        },
    }


def resolve_curator_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization).values_list('curator_enabled', flat=True).first()
    )
    if enabled is None:
        return True

    return enabled


def resolve_curator_llm_judge_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('curator_llm_judge_enabled', flat=True)
        .first()
    )
    if enabled is None:
        return False

    return enabled


def curation_judge_system_prompt() -> str:
    return (
        'You are a memory curation judge for a software engineering memory store.\n'
        'You are given a new candidate memory and an existing near-duplicate memory.\n'
        'Decide how to reconcile them.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly two keys "decision" and "reason".\n'
        '- "decision" is one of "merge", "keep_both", "reject", "contradicts".\n'
        '- "reason" is one short sentence explaining the decision.\n'
        '- "merge": the same durable fact; the new candidate should supersede the existing memory.\n'
        '- "keep_both": the two memories are distinct, compatible durable facts and both should be kept.\n'
        '- "reject": the new candidate adds no durable value beyond the existing memory.\n'
        '- "contradicts": the candidate asserts the opposite of the existing memory '
        '(not a duplicate, not unrelated).\n'
        '- Do not name any AI assistant, tool, or product by brand.'
    )


def curation_judge_prompt(candidate: MemoryCandidate, memory: Memory) -> str:
    return '\n\n'.join(
        [
            '\n'.join(
                [
                    'New candidate memory:',
                    f'Title: {redact_text(candidate.title)}',
                    f'Body: {redact_text(candidate.body)}',
                ],
            ),
            '\n'.join(
                [
                    'Existing near-duplicate memory:',
                    f'Title: {redact_text(memory.title)}',
                    f'Body: {redact_text(memory.body)}',
                ],
            ),
        ],
    )


def parse_curation_decision(raw_body: str) -> str:
    try:
        parsed = json.loads(strip_json_fence(raw_body))
    except (json.JSONDecodeError, TypeError):
        return _DEFAULT_JUDGE_DECISION

    if not isinstance(parsed, dict):
        return _DEFAULT_JUDGE_DECISION

    decision = str(parsed.get('decision') or '').strip().lower()
    if decision in _JUDGE_DECISIONS:
        return decision

    return _DEFAULT_JUDGE_DECISION


def parse_curation_reason(raw_body: str) -> str:
    try:
        parsed = json.loads(strip_json_fence(raw_body))
    except (json.JSONDecodeError, TypeError):
        return ''

    if not isinstance(parsed, dict):
        return ''

    return str(parsed.get('reason') or '').strip()


def resolve_near_dup_threshold(organization: Organization) -> Decimal:
    threshold = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('near_dup_threshold', flat=True)
        .first()
    )
    if threshold is not None:
        return threshold

    return Decimal(str(settings.ENGRAM_NEAR_DUP_THRESHOLD))


def is_low_signal(candidate: MemoryCandidate) -> bool:
    body = redact_text(candidate.body).strip()
    if not body:
        return True
    if body == redact_text(candidate.title).strip():
        return True

    return False


def embed_candidate(candidate: MemoryCandidate) -> list[float] | None:
    try:
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
                task_type='embedding',
            ),
        )
        result = get_provider_gateway(resolved.policy).embed(
            EmbeddingCallInput(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
                policy=resolved.policy,
                request_id=f'curator:{candidate.id}:embedding',
                trace_id=f'curator:{candidate.id}',
                text=f'{candidate.title}\n{candidate.body}',
            ),
        )
    except ModelPolicyError:
        return None
    except ProviderSecretError as error:
        logger.warning(
            'curator_embedding_skipped',
            organization_id=str(candidate.organization_id),
            project_id=str(candidate.project_id),
            candidate_id=str(candidate.id),
            error=str(error),
        )

        return None

    return list(result.embedding)


def find_near_duplicate(
    candidate_embedding: list[float],
    documents: tuple[RetrievalDocument, ...],
    threshold: Decimal,
) -> tuple[RetrievalDocument, float] | None:
    if not candidate_embedding:
        return None

    floor = float(threshold)
    best_document: RetrievalDocument | None = None
    best_score = 0.0
    for document in documents:
        vector = document.embedding_vector
        if not vector:
            continue
        score = cosine_similarity(candidate_embedding, list(vector))
        if score < floor:
            continue
        if best_document is None or score > best_score:
            best_document = document
            best_score = score
    if best_document is None:
        return None

    return best_document, best_score


def supersede_memory_system(
    loser: Memory,
    winner: Memory,
    candidate: MemoryCandidate,
    *,
    score: float | None = None,
    threshold: Decimal | None = None,
    judge_context: dict[str, object] | None = None,
    request_id: str = '',
    correlation_id: str = '',
) -> object | None:
    if candidate.decision_work_contract_version != 1:
        raise MemoryWorkerError('legacy supersede path is disabled; transition contract v1 is required')

    from engram.memory.transitions import (
        SupersedeMemories,
        SupersedeMemoriesInput,
        TransitionRequest,
        TransitionScope,
        build_memory_fence,
    )

    idempotency_key = f'request:curator:{candidate.id}:supersede:{loser.id}:v1'
    result = SupersedeMemories().execute(
        SupersedeMemoriesInput(
            request=TransitionRequest(
                scope=TransitionScope(
                    organization_id=candidate.organization_id,
                    project_id=candidate.project_id,
                    team_id=candidate.team_id,
                ),
                idempotency_key=idempotency_key,
                actor_type='system',
                actor_id='curator',
                capability='memories:review',
                request_id=request_id or idempotency_key,
                correlation_id=correlation_id,
                reason='curator:supersede',
                origin='curator',
            ),
            source_memory_fence=build_memory_fence(loser),
            result_memory_fence=build_memory_fence(winner),
        ),
    )

    return result.transition.semantic_link


class CurateMemoryCandidate:
    def execute(self, data: CurateMemoryCandidateInput) -> CurateMemoryCandidateResult:
        candidate = self._read_candidate(data.candidate_id)
        if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
            return self._replay(candidate)
        if candidate.status != CandidateStatus.PROPOSED:
            raise MemoryWorkerError('Only proposed memory candidates can be curated')
        if self._has_unresolved_conflict(candidate):
            return CurateMemoryCandidateResult(decision='held_conflict', candidate=candidate, memory=None)

        if is_low_signal(candidate):
            return self._reject(candidate, data)

        reason = escalation_reason(candidate)
        if reason:
            return self._hold_for_escalation(candidate, reason, data)

        if not resolve_curator_enabled(candidate.organization):
            return self._promote(candidate, 'passthrough', data, route='passthrough')

        embedding = embed_candidate(candidate)
        if embedding is not None:
            return self._curate_with_embedding(candidate, embedding, data)

        return self._promote(candidate, 'promoted', data, route='embedding_unavailable')

    def _curate_with_embedding(
        self,
        candidate: MemoryCandidate,
        embedding: list[float],
        data: CurateMemoryCandidateInput,
    ) -> CurateMemoryCandidateResult:
        threshold = resolve_near_dup_threshold(candidate.organization)
        judge_enabled = resolve_curator_llm_judge_enabled(candidate.organization)
        floor = threshold - _GRAY_BAND_WIDTH if judge_enabled else threshold
        documents = self._authorized_documents(candidate)
        near_dup = find_near_duplicate(embedding, documents, floor)
        if near_dup is None:
            return self._promote(candidate, 'promoted', data, route='no_duplicate', threshold=threshold)

        _document, score = near_dup
        if score >= float(threshold):
            return self._supersede(candidate, near_dup, data, threshold=threshold)

        return self._judge(candidate, near_dup, data, threshold=threshold)

    def _judge(
        self,
        candidate: MemoryCandidate,
        near_dup: tuple[RetrievalDocument, float],
        data: CurateMemoryCandidateInput,
        *,
        threshold: Decimal | None = None,
    ) -> CurateMemoryCandidateResult:
        document, score = near_dup
        outcome = self._judge_decision(candidate, document.memory, data)
        if outcome.decision == 'merge':
            return self._supersede(candidate, near_dup, data, judge_context=outcome.judge_context, threshold=threshold)

        if outcome.decision == 'reject':
            return self._reject(
                candidate,
                data,
                reason='near_dup_judge_reject',
                near_dup_score=score,
                judge_context=outcome.judge_context,
                threshold=threshold,
            )

        if outcome.decision == 'contradicts':
            return self._hold_for_conflict(
                candidate,
                document.memory,
                score,
                outcome.reason,
                data,
                judge_context=outcome.judge_context,
                threshold=threshold,
            )

        return self._promote(
            candidate,
            'promoted',
            data,
            route='judge_keep_both',
            judge_context=outcome.judge_context,
            threshold=threshold,
        )

    def _judge_decision(
        self,
        candidate: MemoryCandidate,
        memory: Memory,
        data: CurateMemoryCandidateInput,
    ) -> _JudgeOutcome:
        try:
            resolved = self._resolve_judge_policy(candidate)
            result = get_provider_gateway(resolved.policy).call(
                ProviderCallInput(
                    organization_id=candidate.organization_id,
                    project_id=candidate.project_id,
                    team_id=candidate.team_id,
                    policy=resolved.policy,
                    request_id=f'curator:{candidate.id}:judge',
                    trace_id=data.correlation_id or f'curator:{candidate.id}',
                    prompt=curation_judge_prompt(candidate, memory),
                    system_prompt=curation_judge_system_prompt(),
                    response_kind='curation_judgment',
                ),
            )
        except (ModelPolicyError, ProviderSecretError):
            return _JudgeOutcome(_DEFAULT_JUDGE_DECISION, '')

        decision = parse_curation_decision(result.generated_body)
        reason = parse_curation_reason(result.generated_body)
        logger.info(
            'curation_judge_decision',
            candidate_id=str(candidate.id),
            memory_id=str(memory.id),
            decision=decision,
            reason=redact_text(reason),
        )

        return _JudgeOutcome(decision, reason, _build_judge_context(resolved.policy, result, candidate, memory))

    def _resolve_judge_policy(self, candidate: MemoryCandidate) -> ResolvedModelPolicy:
        try:
            return ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=candidate.organization_id,
                    project_id=candidate.project_id,
                    team_id=candidate.team_id,
                    task_type='curation',
                ),
            )
        except ModelPolicyError:
            return ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=candidate.organization_id,
                    project_id=candidate.project_id,
                    team_id=candidate.team_id,
                    task_type='generation',
                ),
            )

    def _read_candidate(self, candidate_id: uuid.UUID) -> MemoryCandidate:
        try:
            return MemoryCandidate.objects.select_related('organization', 'project', 'team').get(id=candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('memory candidate not found') from error

    def _lock_candidate(self, candidate_id: uuid.UUID) -> MemoryCandidate:
        try:
            return (
                MemoryCandidate.objects.select_for_update(of=('self',))
                .select_related('organization', 'project', 'team')
                .get(id=candidate_id)
            )
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('memory candidate not found') from error

    def _has_unresolved_conflict(self, candidate: MemoryCandidate) -> bool:
        return MemoryConflict.objects.filter(
            candidate_id=candidate.id,
            resolved_transition__isnull=True,
        ).exists()

    def _typed_candidate_fence(self, candidate: MemoryCandidate) -> object:
        from engram.memory.candidate_decision_work import evidence_manifest
        from engram.memory.transitions import CandidateFence

        _entries, manifest_hash = evidence_manifest(candidate)
        return CandidateFence(
            candidate_id=candidate.id,
            candidate_content_hash=candidate.content_hash,
            evidence_manifest_hash=manifest_hash,
        )

    def _typed_request(
        self,
        candidate: MemoryCandidate,
        data: CurateMemoryCandidateInput,
        *,
        action: str,
        reason: str,
        memory_id: uuid.UUID | None = None,
    ) -> object:
        from engram.memory.transitions import TransitionRequest, TransitionScope

        subject_id = memory_id or candidate.id
        key = f'request:curator:{candidate.id}:{action}:{subject_id}:v1'
        return TransitionRequest(
            scope=TransitionScope(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
            ),
            idempotency_key=key,
            actor_type='system',
            actor_id='curator',
            capability='memories:review',
            request_id=f'curator:{candidate.id}',
            correlation_id=data.correlation_id,
            reason=reason,
            origin='curator',
        )

    def _typed_evidence_hash(self, candidate: MemoryCandidate, memory: Memory) -> str:
        from engram.memory.candidate_decision_work import evidence_manifest

        _entries, manifest_hash = evidence_manifest(candidate)
        memory_version_id = (
            MemoryVersion.objects.filter(memory_id=memory.id, version=memory.current_version)
            .values_list('id', flat=True)
            .first()
        )
        payload = {
            'schema': 'memory_conflict_evidence/v1',
            'candidate_id': str(candidate.id),
            'candidate_content_hash': candidate.content_hash,
            'evidence_manifest_hash': manifest_hash,
            'memory_id': str(memory.id),
            'memory_version_id': str(memory_version_id) if memory_version_id is not None else None,
            'memory_body_sha256': hashlib.sha256(memory.body.encode()).hexdigest(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _replay(self, candidate: MemoryCandidate) -> CurateMemoryCandidateResult:
        promotion = _LegacyPromoteMemoryCandidate().execute(
            _LegacyPromoteMemoryCandidateInput(candidate_id=candidate.id)
        )

        return CurateMemoryCandidateResult(
            decision='promoted',
            candidate=promotion.candidate,
            memory=promotion.memory,
            memory_version=promotion.memory_version,
            retrieval_document=promotion.retrieval_document,
            duplicate=True,
        )

    def _promote(
        self,
        candidate: MemoryCandidate,
        decision: str,
        data: CurateMemoryCandidateInput,
        *,
        route: str,
        judge_context: dict[str, object] | None = None,
        threshold: Decimal | None = None,
    ) -> CurateMemoryCandidateResult:
        if self._has_unresolved_conflict(candidate):
            return CurateMemoryCandidateResult(decision='held_conflict', candidate=candidate, memory=None)
        promotion = _LegacyPromoteMemoryCandidate().execute(
            _LegacyPromoteMemoryCandidateInput(candidate_id=candidate.id)
        )
        if not promotion.duplicate:
            _audit_curator_action(
                candidate=promotion.candidate,
                event_type='MemoryCuratorPromoted',
                decision=route,
                judge_context=judge_context,
                threshold=threshold,
                correlation_id=data.correlation_id,
                extra={'memory_id': str(promotion.memory.id)},
            )

        return CurateMemoryCandidateResult(
            decision=decision,
            candidate=promotion.candidate,
            memory=promotion.memory,
            memory_version=promotion.memory_version,
            retrieval_document=promotion.retrieval_document,
            duplicate=promotion.duplicate,
        )

    def _reject(
        self,
        candidate: MemoryCandidate,
        data: CurateMemoryCandidateInput,
        *,
        reason: str = 'low_signal',
        near_dup_score: float | None = None,
        judge_context: dict[str, object] | None = None,
        threshold: Decimal | None = None,
    ) -> CurateMemoryCandidateResult:
        already_settled = False
        held_conflict = False
        with transaction.atomic():
            locked = self._lock_candidate(candidate.id)
            if locked.status != CandidateStatus.PROPOSED:
                already_settled = True
            elif self._has_unresolved_conflict(locked):
                held_conflict = True
            else:
                locked.status = CandidateStatus.REJECTED
                locked.save(update_fields=['status', 'updated_at'])
                clear_candidate_conflict_links(locked)
                _audit_curator_action(
                    candidate=locked,
                    event_type='MemoryAutoRejected',
                    decision='rejected',
                    reason=reason,
                    near_dup_score=near_dup_score,
                    judge_context=judge_context,
                    threshold=threshold,
                    correlation_id=data.correlation_id,
                    extra={'body_length': len(redact_text(locked.body).strip())},
                )

        if already_settled:
            return self._reconcile_already_handled(locked)
        if held_conflict:
            return CurateMemoryCandidateResult(decision='held_conflict', candidate=locked, memory=None)

        return CurateMemoryCandidateResult(decision='rejected', candidate=locked, memory=None)

    def _reconcile_already_handled(self, candidate: MemoryCandidate) -> CurateMemoryCandidateResult:
        if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
            return self._replay(candidate)

        return CurateMemoryCandidateResult(decision='rejected', candidate=candidate, memory=None)

    def _hold_for_escalation(
        self,
        candidate: MemoryCandidate,
        reason: str,
        data: CurateMemoryCandidateInput,
    ) -> CurateMemoryCandidateResult:
        metadata_reason = f'escalation:{reason}'
        already_settled = False
        held_conflict = False
        with transaction.atomic():
            locked = self._lock_candidate(candidate.id)
            if locked.status != CandidateStatus.PROPOSED:
                already_settled = True
            elif self._has_unresolved_conflict(locked):
                held_conflict = True
            elif not self._has_escalation_audit(locked):
                _audit_curator_action(
                    candidate=locked,
                    event_type='MemoryCandidateHeldForReview',
                    decision='held_escalation',
                    reason=metadata_reason,
                    correlation_id=data.correlation_id,
                )

        if already_settled:
            return self._reconcile_already_handled(locked)
        if held_conflict:
            return CurateMemoryCandidateResult(decision='held_conflict', candidate=locked, memory=None)

        return CurateMemoryCandidateResult(decision='held_escalation', candidate=locked, memory=None)

    def _has_escalation_audit(self, candidate: MemoryCandidate) -> bool:
        audits = AuditEvent.objects.filter(
            organization=candidate.organization,
            target_type='memory_candidate',
            target_id=str(candidate.id),
            event_type='MemoryCandidateHeldForReview',
        )

        return any(str(audit.metadata.get('reason', '')).startswith('escalation:') for audit in audits)

    def _hold_for_conflict(
        self,
        candidate: MemoryCandidate,
        existing_memory: Memory,
        score: float,
        reason: str,
        data: CurateMemoryCandidateInput,
        *,
        judge_context: dict[str, object] | None = None,
        threshold: Decimal | None = None,
    ) -> CurateMemoryCandidateResult:
        stored_reason = redact_text(reason)[:200]
        if candidate.decision_work_contract_version == 1:
            from engram.memory.transitions import OpenMemoryConflict, OpenMemoryConflictInput, build_memory_fence

            _conflict = OpenMemoryConflict().execute(
                OpenMemoryConflictInput(
                    request=self._typed_request(
                        candidate,
                        data,
                        action='conflict_open',
                        memory_id=existing_memory.id,
                        reason=stored_reason,
                    ),
                    candidate_fence=self._typed_candidate_fence(candidate),
                    memory_fence=build_memory_fence(existing_memory),
                    evidence_hash=self._typed_evidence_hash(candidate, existing_memory),
                    redacted_reason=stored_reason,
                ),
            )
            locked = self._read_candidate(candidate.id)
            return CurateMemoryCandidateResult(
                decision='held_conflict',
                candidate=locked,
                memory=None,
                near_dup_score=score,
            )

        already_settled = False
        with transaction.atomic():
            locked = self._lock_candidate(candidate.id)
            if locked.status != CandidateStatus.PROPOSED:
                already_settled = True
            else:
                has_conflict_entry = any(
                    isinstance(entry, dict)
                    and entry.get('type') == 'conflict'
                    and entry.get('memory_id') == str(existing_memory.id)
                    for entry in locked.evidence
                )
                if not has_conflict_entry:
                    locked.evidence = [
                        *locked.evidence,
                        {'type': 'conflict', 'memory_id': str(existing_memory.id), 'reason': stored_reason},
                    ]
                    locked.save(update_fields=['evidence', 'updated_at'])
                    _audit_curator_action(
                        candidate=locked,
                        event_type='MemoryConflictDetected',
                        decision='held_conflict',
                        reason=stored_reason,
                        near_dup_score=score,
                        judge_context=judge_context,
                        threshold=threshold,
                        correlation_id=data.correlation_id,
                        extra={'memory_id': str(existing_memory.id)},
                    )

        if already_settled:
            return self._reconcile_already_handled(locked)

        return CurateMemoryCandidateResult(
            decision='held_conflict', candidate=locked, memory=None, near_dup_score=score
        )

    def _supersede(
        self,
        candidate: MemoryCandidate,
        near_dup: tuple[RetrievalDocument, float],
        data: CurateMemoryCandidateInput,
        *,
        judge_context: dict[str, object] | None = None,
        threshold: Decimal | None = None,
    ) -> CurateMemoryCandidateResult:
        document, score = near_dup
        loser = document.memory
        if candidate.decision_work_contract_version == 1:
            from engram.memory.transitions import (
                SupersedeMemoryWithCandidate,
                SupersedeMemoryWithCandidateInput,
                build_memory_fence,
            )

            transition_result = SupersedeMemoryWithCandidate().execute(
                SupersedeMemoryWithCandidateInput(
                    request=self._typed_request(
                        candidate,
                        data,
                        action='supersede',
                        reason='curator:supersede',
                    ),
                    candidate_fence=self._typed_candidate_fence(candidate),
                    loser_memory_fence=build_memory_fence(loser),
                ),
            )
            winner = self._read_candidate(candidate.id)
            loser.refresh_from_db()
            return CurateMemoryCandidateResult(
                decision='superseded',
                candidate=winner,
                memory=transition_result.memory,
                memory_version=transition_result.memory_version,
                retrieval_document=transition_result.retrieval_document,
                superseded_memory=loser,
                duplicate=transition_result.duplicate,
                near_dup_score=score,
            )

        raise MemoryWorkerError('legacy supersede path is disabled; transition contract v1 is required')

    def _authorized_documents(self, candidate: MemoryCandidate) -> tuple[RetrievalDocument, ...]:
        scope = EffectiveScope(
            organization_id=candidate.organization_id,
            identity_id=candidate.organization_id,
            api_key_id=candidate.organization_id,
            project_ids=(candidate.project_id,),
            team_ids=(candidate.team_id,) if candidate.team_id else (),
            capabilities=(),
            actor_type='system',
            actor_id='curator',
            project_bound=False,
        )

        return authorized_retrieval_documents(
            candidate.organization,
            candidate.project,
            scope,
            include_embeddings=True,
        )


_DETERMINISTIC_COMPARISON_HASH = hashlib.sha256(b'curation_decision.deterministic_no_comparison.v1').hexdigest()

_VERDICT_OUTCOME = {
    'publish_new': CurationOutcome.PUBLISH_NEW,
    'merge_evidence': CurationOutcome.MERGE_EVIDENCE,
    'revise_memory': CurationOutcome.REVISE_MEMORY,
    'supersede_memory': CurationOutcome.SUPERSEDE_MEMORY,
    'reject_candidate': CurationOutcome.REJECT_CANDIDATE,
    'open_conflict': CurationOutcome.OPEN_CONFLICT,
}


_CANDIDATE_DECISION_FALSY = frozenset({'0', 'false', 'no', 'off'})


def candidate_decision_enabled(work: WorkflowWork) -> bool:
    raw = os.environ.get('ENGRAM_CANDIDATE_DECISION_ENABLED')
    if raw is None:
        return True

    return raw.strip().lower() not in _CANDIDATE_DECISION_FALSY


def _fault_boundary(_point: str) -> None:
    return None


def resolve_candidate_embedding(
    candidate: MemoryCandidate,
    view: SanitizedCandidateView,
    scope: EffectiveCandidateScope,
) -> tuple[float, ...] | None:
    try:
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
                task_type='embedding',
            ),
        )
        result = get_provider_gateway(resolved.policy).embed(
            EmbeddingCallInput(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
                policy=resolved.policy,
                request_id=f'curation-decision:{candidate.id}:embedding',
                trace_id=f'curation-decision:{candidate.id}',
                text=f'{view.title}\n{view.body}',
            ),
        )
    except (ModelPolicyError, ProviderSecretError):
        return None

    return tuple(result.embedding)


def build_curation_shortlist(data: BuildCurationShortlistInput) -> CurationShortlist:
    return BuildCurationShortlist.execute(data)


def build_curation_evidence_context(candidate_id: uuid.UUID, shortlist: CurationShortlist) -> CurationEvidenceContext:
    return _judge_build_curation_evidence_context(candidate_id, shortlist)


def judge_curation_candidate(data: CurationJudgeInput) -> CurationJudgeResult:
    return JudgeCurationCandidate().execute(data)


def _operational(reason: str, message: str) -> MemoryTransitionError:
    return MemoryTransitionError(reason, message, retryable=True)


class DecideMemoryCandidate:
    def execute(self, *, work: WorkflowWork, claim: WorkClaim) -> None:
        candidate = self._load_candidate(work)
        if self._is_superseded_generation(work, candidate):
            self._settle_superseded_generation(claim)

            return

        gate = EvaluateDeterministicCandidateGates().execute(work.id)
        if gate.disposition == DeterministicGateDisposition.RETRY:
            raise _operational(gate.operational_reason or 'stale_decision', 'deterministic gate requires retry')

        if gate.disposition == DeterministicGateDisposition.TERMINAL:
            self._settle_deterministic(work, candidate, claim, gate)

            return

        self._settle_model_decision(work, candidate, claim, gate)

        return

    def _load_candidate(self, work: WorkflowWork) -> MemoryCandidate:
        if work.subject_type != WorkflowSubjectType.MEMORY_CANDIDATE:
            raise MemoryWorkerError(
                'workflow work subject type does not match candidate task',
                code='work_contract_invalid',
            )
        try:
            return MemoryCandidate.objects.select_related('organization', 'project', 'team', 'source_observation').get(
                id=work.subject_id,
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
            )
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('candidate is outside workflow work scope', code='work_scope_invalid') from error

    def _is_superseded_generation(self, work: WorkflowWork, candidate: MemoryCandidate) -> bool:
        try:
            current = candidate_decision_snapshot(build_candidate_decision_input(candidate))
        except (TypeError, ValueError) as error:
            raise MemoryWorkerError(
                'candidate decision evidence is invalid',
                code='work_fingerprint_mismatch',
            ) from error

        return current != work.input_snapshot

    def _settle_superseded_generation(self, claim: WorkClaim) -> None:
        finish_work_claim(
            claim=claim,
            now=timezone.now(),
            completion='product_no_signal',
            resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
        )

        return

    def _settle_deterministic(
        self,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        claim: WorkClaim,
        gate: DeterministicGateResult,
    ) -> None:
        if gate.terminal_outcome == DeterministicTerminalOutcome.REJECT_CANDIDATE:
            with transaction.atomic():
                lock_work_fence(claim=claim, now=timezone.now())
                locked = MemoryCandidate.objects.select_for_update().get(id=candidate.id)
                if self._is_superseded_generation(work, locked):
                    finish_work_claim(
                        claim=claim,
                        now=timezone.now(),
                        completion='product_no_signal',
                        resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
                    )

                    return
                if locked.status == CandidateStatus.PROPOSED:
                    locked.status = CandidateStatus.REJECTED
                    locked.save(update_fields=['status', 'updated_at'])
                finish_work_claim(claim=claim, now=timezone.now(), completion='product_no_signal')
                self._write_decision(
                    work=work,
                    candidate=locked,
                    scope=gate.effective_scope,
                    outcome=CurationOutcome.REJECT_CANDIDATE,
                    reason_code=gate.reason_code,
                    evidence_tier=EvidenceTier.NONE,
                    comparison_manifest_hash=_DETERMINISTIC_COMPARISON_HASH,
                    target_memory_version_id=None,
                    transition=None,
                    conflict=None,
                    judge=None,
                    redacted_reason='',
                )

            return

        if gate.requires_transition is False:
            self._settle_exact_duplicate(work, candidate, claim, gate)

            return

        memory_fence = self._target_memory_fence(gate.target_memory_version_id, candidate)
        with transaction.atomic():
            result = MergeMemoryCandidate().execute(
                MergeMemoryCandidateInput(
                    request=self._request(work, candidate),
                    candidate_fence=self._candidate_fence(candidate),
                    memory_fence=memory_fence,
                    title=candidate.title,
                    body=candidate.body,
                    work_claim=claim,
                    sanitized_title=gate.sanitized_candidate.title,
                    sanitized_body=gate.sanitized_candidate.body,
                ),
            )
            self._write_decision(
                work=work,
                candidate=candidate,
                scope=gate.effective_scope,
                outcome=CurationOutcome.MERGE_EVIDENCE,
                reason_code=gate.reason_code,
                evidence_tier=EvidenceTier.NONE,
                comparison_manifest_hash=_DETERMINISTIC_COMPARISON_HASH,
                target_memory_version_id=gate.target_memory_version_id,
                transition=result.transition,
                conflict=None,
                judge=None,
                redacted_reason='',
            )

        return

    def _settle_exact_duplicate(
        self,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        claim: WorkClaim,
        gate: DeterministicGateResult,
    ) -> None:
        with transaction.atomic():
            lock_work_fence(claim=claim, now=timezone.now())
            locked = MemoryCandidate.objects.select_for_update().get(id=candidate.id)
            if self._is_superseded_generation(work, locked):
                finish_work_claim(
                    claim=claim,
                    now=timezone.now(),
                    completion='product_no_signal',
                    resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
                )

                return
            target_memory = self._current_target_memory(gate.target_memory_version_id)
            if locked.status == CandidateStatus.PROPOSED:
                locked.status = CandidateStatus.PROMOTED
                locked.promoted_memory_id = target_memory.id
                locked.save(update_fields=['status', 'promoted_memory', 'updated_at'])
            finish_work_claim(
                claim=claim,
                now=timezone.now(),
                completion='product_succeeded',
                result_memory_id=target_memory.id,
            )
            self._write_decision(
                work=work,
                candidate=locked,
                scope=gate.effective_scope,
                outcome=CurationOutcome.MERGE_EVIDENCE,
                reason_code=gate.reason_code,
                evidence_tier=EvidenceTier.NONE,
                comparison_manifest_hash=_DETERMINISTIC_COMPARISON_HASH,
                target_memory_version_id=gate.target_memory_version_id,
                transition=None,
                conflict=None,
                judge=None,
                redacted_reason='',
            )

        return

    def _current_target_memory(self, target_memory_version_id: uuid.UUID | None) -> Memory:
        if target_memory_version_id is None:
            raise _operational('stale_decision', 'exact duplicate settlement requires a target version')
        try:
            version = (
                MemoryVersion.objects.select_for_update().select_related('memory').get(id=target_memory_version_id)
            )
        except MemoryVersion.DoesNotExist as error:
            raise _operational('stale_decision', 'exact duplicate target no longer exists') from error
        memory = version.memory
        if memory.current_version != version.version or memory.stale or memory.refuted:
            raise _operational('stale_decision', 'exact duplicate target advanced after gate evaluation')

        return memory

    def _settle_model_decision(
        self,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        claim: WorkClaim,
        gate: DeterministicGateResult,
    ) -> None:
        view = gate.sanitized_candidate
        scope = gate.effective_scope
        embedding = resolve_candidate_embedding(candidate, view, scope)
        _fault_boundary('after_embedding')
        if embedding is None:
            raise _operational('embedding_provider_unavailable', 'candidate embedding is unavailable')

        try:
            shortlist = build_curation_shortlist(self._shortlist_input(work, view, scope, embedding))
        except CurationShortlistError as error:
            raise _operational(error.code, 'authorized shortlist build failed') from error

        try:
            evidence = build_curation_evidence_context(candidate.id, shortlist)
        except CurationJudgeError as error:
            raise _operational(error.code, 'evidence context build failed') from error

        try:
            judge_input = self._judge_input(work, candidate, view, scope, shortlist, evidence)
            judge_result = judge_curation_candidate(judge_input)
        except (CurationJudgeError, ModelPolicyError, ProviderSecretError) as error:
            raise _operational(
                getattr(error, 'code', None) or 'judge_provider_unavailable',
                'curation judge is unavailable',
            ) from error
        _fault_boundary('after_judge')

        verdict = judge_result.verdict
        self._validate_verdict(verdict, shortlist, judge_result)
        target_version_id = verdict.target_memory_version_id
        if verdict.outcome == 'reject_candidate':
            self._settle_model_rejection(
                work=work,
                candidate=candidate,
                claim=claim,
                scope=scope,
                view=view,
                embedding=embedding,
                verdict=verdict,
                evidence=evidence,
                shortlist=shortlist,
                judge_result=judge_result,
                target_memory_version_id=target_version_id,
            )

            return

        memory_fence = None
        if target_version_id is not None:
            memory_fence = self._shortlist_memory_fence(target_version_id, shortlist)

        _fault_boundary('before_transition')
        with transaction.atomic():
            self._revalidate_shortlist(work, view, scope, embedding, shortlist)
            transition, conflict = self._apply_transition(work, candidate, claim, verdict, memory_fence, view, scope)
            self._write_decision(
                work=work,
                candidate=candidate,
                scope=scope,
                outcome=_VERDICT_OUTCOME[verdict.outcome],
                reason_code=verdict.reason_code,
                evidence_tier=evidence.candidate.tier,
                comparison_manifest_hash=shortlist.manifest_hash,
                target_memory_version_id=target_version_id,
                transition=transition,
                conflict=conflict,
                judge=judge_result,
                redacted_reason=redact_text(verdict.reason)[:500],
            )

        return

    def _settle_model_rejection(
        self,
        *,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        claim: WorkClaim,
        scope: EffectiveCandidateScope,
        view: SanitizedCandidateView,
        embedding: tuple[float, ...],
        verdict: object,
        evidence: CurationEvidenceContext,
        shortlist: CurationShortlist,
        judge_result: CurationJudgeResult,
        target_memory_version_id: uuid.UUID | None,
    ) -> None:
        _fault_boundary('before_transition')
        with transaction.atomic():
            lock_work_fence(claim=claim, now=timezone.now())
            locked = MemoryCandidate.objects.select_for_update().get(id=candidate.id)
            if self._is_superseded_generation(work, locked):
                finish_work_claim(
                    claim=claim,
                    now=timezone.now(),
                    completion='product_no_signal',
                    resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
                )

                return
            self._revalidate_shortlist(work, view, scope, embedding, shortlist)
            if locked.status == CandidateStatus.PROPOSED:
                locked.status = CandidateStatus.REJECTED
                locked.save(update_fields=['status', 'updated_at'])
            finish_work_claim(claim=claim, now=timezone.now(), completion='product_no_signal')
            self._write_decision(
                work=work,
                candidate=locked,
                scope=scope,
                outcome=CurationOutcome.REJECT_CANDIDATE,
                reason_code=verdict.reason_code,
                evidence_tier=evidence.candidate.tier,
                comparison_manifest_hash=shortlist.manifest_hash,
                target_memory_version_id=target_memory_version_id,
                transition=None,
                conflict=None,
                judge=judge_result,
                redacted_reason=redact_text(verdict.reason)[:500],
            )

        return

    def _apply_transition(
        self,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        claim: WorkClaim,
        verdict: object,
        memory_fence: MemoryFence | None,
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
    ) -> tuple[object | None, object | None]:
        outcome = verdict.outcome
        request = self._request(work, candidate)
        candidate_fence = self._candidate_fence(candidate)
        if outcome == 'publish_new':
            result = PromoteMemoryCandidate().execute(
                PromoteMemoryCandidateInput(
                    request=request,
                    candidate_fence=candidate_fence,
                    work_claim=claim,
                    sanitized_title=view.title,
                    sanitized_body=view.body,
                    effective_visibility_scope=scope.visibility_scope,
                    effective_team_id=scope.team_id,
                ),
            )

            return result.transition, None

        if outcome == 'merge_evidence':
            result = MergeMemoryCandidate().execute(
                MergeMemoryCandidateInput(
                    request=request,
                    candidate_fence=candidate_fence,
                    memory_fence=memory_fence,
                    title=candidate.title,
                    body=candidate.body,
                    work_claim=claim,
                    sanitized_title=view.title,
                    sanitized_body=view.body,
                ),
            )

            return result.transition, None

        if outcome == 'revise_memory':
            result = ReviseMemoryFromCandidate().execute(
                ReviseMemoryFromCandidateInput(
                    request=request,
                    candidate_fence=candidate_fence,
                    memory_fence=memory_fence,
                    title=candidate.title,
                    body=candidate.body,
                    work_claim=claim,
                    sanitized_title=view.title,
                    sanitized_body=view.body,
                ),
            )

            return result.transition, None

        if outcome == 'supersede_memory':
            result = SupersedeMemoryWithCandidate().execute(
                SupersedeMemoryWithCandidateInput(
                    request=request,
                    candidate_fence=candidate_fence,
                    loser_memory_fence=memory_fence,
                    work_claim=claim,
                    sanitized_title=view.title,
                    sanitized_body=view.body,
                    effective_visibility_scope=scope.visibility_scope,
                    effective_team_id=scope.team_id,
                ),
            )

            return result.transition, None

        existing = self._existing_open_conflict(candidate, memory_fence)
        if existing is not None:
            lock_work_fence(claim=claim, now=timezone.now())
            finish_work_claim(
                claim=claim,
                now=timezone.now(),
                completion='product_succeeded',
                result_memory_id=None,
            )

            return None, existing

        conflict = OpenMemoryConflict().execute(
            OpenMemoryConflictInput(
                request=request,
                candidate_fence=candidate_fence,
                memory_fence=memory_fence,
                evidence_hash=self._conflict_evidence_hash(candidate_fence, memory_fence),
                redacted_reason=redact_text(verdict.reason)[:500],
                work_claim=claim,
            ),
        )

        return conflict.opened_transition, conflict

    def _existing_open_conflict(
        self,
        candidate: MemoryCandidate,
        memory_fence: MemoryFence | None,
    ) -> MemoryConflict | None:
        if memory_fence is None:
            return None

        return (
            MemoryConflict.objects.select_for_update()
            .filter(
                candidate_id=candidate.id,
                memory_id=memory_fence.memory_id,
                resolved_transition__isnull=True,
            )
            .order_by('id')
            .first()
        )

    def _validate_verdict(
        self,
        verdict: object,
        shortlist: CurationShortlist,
        judge_result: CurationJudgeResult,
    ) -> None:
        if verdict.outcome not in _VERDICT_OUTCOME:
            raise _operational('judge_invalid_output', 'verdict outcome is not recognised')
        if judge_result.comparison_manifest_hash != shortlist.manifest_hash:
            raise _operational('stale_decision', 'verdict was judged against a different shortlist')
        target_id = verdict.target_memory_version_id
        if target_id is not None and all(entry.memory_version_id != target_id for entry in shortlist.entries):
            raise _operational('judge_reference_invalid', 'verdict target is not in the shortlist')

        return

    def _request(self, work: WorkflowWork, candidate: MemoryCandidate) -> TransitionRequest:
        return TransitionRequest(
            scope=TransitionScope(
                organization_id=candidate.organization_id,
                project_id=candidate.project_id,
                team_id=candidate.team_id,
            ),
            idempotency_key=f'decision-work:{work.id}:settle:v1',
            actor_type='system',
            actor_id='curation-orchestrator',
            capability='memories:write',
            request_id=f'curation-decision:{work.id}',
            correlation_id=str(work.id),
            reason='curation:automatic-decision',
            origin='curation-orchestrator',
        )

    def _candidate_fence(self, candidate: MemoryCandidate) -> CandidateFence:
        _entries, manifest_hash = candidate_evidence_manifest(candidate)

        return CandidateFence(
            candidate_id=candidate.id,
            candidate_content_hash=candidate.content_hash,
            evidence_manifest_hash=manifest_hash,
        )

    def _shortlist_memory_fence(self, target_version_id: uuid.UUID, shortlist: CurationShortlist) -> MemoryFence:
        entry = next((item for item in shortlist.entries if item.memory_version_id == target_version_id), None)
        if entry is None:
            raise _operational('judge_reference_invalid', 'verdict target is not in the shortlist')
        try:
            memory = Memory.objects.get(id=entry.memory_id)
        except Memory.DoesNotExist as error:
            raise _operational('stale_decision', 'target memory no longer exists') from error
        if memory.current_transition_id != entry.current_transition_id:
            raise _operational('stale_decision', 'target advanced after the shortlist snapshot was frozen')

        return build_memory_fence(memory)

    def _target_memory_fence(self, target_version_id: uuid.UUID | None, candidate: MemoryCandidate) -> MemoryFence:
        if target_version_id is None:
            raise _operational('stale_decision', 'deterministic merge requires a target version')
        try:
            memory_id = MemoryVersion.objects.values_list('memory_id', flat=True).get(id=target_version_id)
        except MemoryVersion.DoesNotExist as error:
            raise _operational('stale_decision', 'deterministic merge target no longer exists') from error

        return self._memory_fence(memory_id)

    def _memory_fence(self, memory_id: uuid.UUID) -> MemoryFence:
        try:
            memory = Memory.objects.get(id=memory_id)
        except Memory.DoesNotExist as error:
            raise _operational('stale_decision', 'target memory no longer exists') from error

        return build_memory_fence(memory)

    def _conflict_evidence_hash(self, candidate_fence: CandidateFence, memory_fence: MemoryFence) -> str:
        payload = {
            'schema': 'curation_conflict_evidence/v1',
            'candidate_id': str(candidate_fence.candidate_id),
            'candidate_content_hash': candidate_fence.candidate_content_hash,
            'evidence_manifest_hash': candidate_fence.evidence_manifest_hash,
            'memory_id': str(memory_fence.memory_id),
            'memory_version_id': str(memory_fence.current_version_id) if memory_fence.current_version_id else None,
        }

        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def _revalidate_shortlist(
        self,
        work: WorkflowWork,
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
        embedding: tuple[float, ...],
        shortlist: CurationShortlist,
    ) -> None:
        data = self._shortlist_input(work, view, scope, embedding)
        try:
            unchanged = revalidate_curation_shortlist(data, shortlist)
        except CurationShortlistError as error:
            raise _operational(error.code, 'shortlist revalidation failed') from error
        if not unchanged:
            raise _operational('stale_decision', 'authorized shortlist changed before settlement')

        return

    def _shortlist_input(
        self,
        work: WorkflowWork,
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
        embedding: tuple[float, ...],
    ) -> BuildCurationShortlistInput:
        return BuildCurationShortlistInput(
            organization_id=work.organization_id,
            project_id=work.project_id,
            effective_scope=scope,
            title=view.title,
            body=view.body,
            query_embedding=embedding,
        )

    def _judge_input(
        self,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
        shortlist: CurationShortlist,
        evidence: CurationEvidenceContext,
    ) -> CurationJudgeInput:
        return CurationJudgeInput(
            organization_id=work.organization_id,
            project_id=work.project_id,
            candidate_id=candidate.id,
            candidate=view,
            effective_scope=scope,
            shortlist=shortlist,
            evidence=evidence,
            request_id=f'curation-decision:{work.id}:judge',
            trace_id=f'curation-decision:{work.id}',
        )

    def _write_decision(
        self,
        *,
        work: WorkflowWork,
        candidate: MemoryCandidate,
        scope: EffectiveCandidateScope,
        outcome: str,
        reason_code: str,
        evidence_tier: str,
        comparison_manifest_hash: str,
        target_memory_version_id: uuid.UUID | None,
        transition: object | None,
        conflict: object | None,
        judge: CurationJudgeResult | None,
        redacted_reason: str,
    ) -> CurationDecision:
        evidence_manifest_hash = work.input_snapshot['evidence_manifest_hash']
        if judge is not None:
            judge_status = 'succeeded'
            provider_call_record_id = judge.provider_call_record_id
            policy_id = judge.policy_id
            policy_version = judge.policy_version
            response_hash = judge.response_hash
        else:
            judge_status = 'not_required'
            provider_call_record_id = None
            policy_id = None
            policy_version = None
            response_hash = None
        effective_team_id = scope.team_id if scope.visibility_scope == VisibilityScope.TEAM else None
        payload = {
            'contract': 'curation_decision.v1',
            'work_id': str(work.id),
            'candidate_id': str(candidate.id),
            'input_fingerprint': work.input_fingerprint,
            'outcome': getattr(outcome, 'value', outcome),
            'reason_code': getattr(reason_code, 'value', reason_code),
            'effective_scope': {
                'visibility_scope': scope.visibility_scope,
                'team_id': str(effective_team_id) if effective_team_id is not None else None,
            },
            'target_memory_version_id': str(target_memory_version_id) if target_memory_version_id is not None else None,
            'evidence_tier': getattr(evidence_tier, 'value', evidence_tier),
            'evidence_manifest_hash': evidence_manifest_hash,
            'comparison_manifest_hash': comparison_manifest_hash,
            'judge': {
                'status': judge_status,
                'provider_call_record_id': (
                    str(provider_call_record_id) if provider_call_record_id is not None else None
                ),
                'policy_id': str(policy_id) if policy_id is not None else None,
                'policy_version': policy_version,
                'response_hash': response_hash,
            },
        }
        payload_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

        return CurationDecision.objects.create(
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=candidate.team_id,
            work=work,
            candidate=candidate,
            input_fingerprint=work.input_fingerprint,
            evidence_manifest_hash=evidence_manifest_hash,
            comparison_manifest_hash=comparison_manifest_hash,
            outcome=outcome,
            reason_code=reason_code,
            redacted_reason=redacted_reason,
            effective_visibility_scope=scope.visibility_scope,
            effective_team_id=effective_team_id,
            target_memory_version_id=target_memory_version_id,
            evidence_tier=evidence_tier,
            provider_call_record_id=provider_call_record_id,
            policy_id=policy_id,
            policy_version=policy_version,
            transition=transition,
            conflict=conflict,
            payload_hash=payload_hash,
        )
