from __future__ import annotations

import hashlib
import json
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
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    RetrievalDocument,
)
from engram.memory.candidate_parsing import strip_json_fence
from engram.memory.conflict_links import clear_candidate_conflict_links, conflict_candidate_target
from engram.memory.escalation import escalation_reason
from engram.memory.services import (
    MemoryWorkerError,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    redact_text,
    redact_value,
)
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
) -> MemoryLink | None:
    loser = Memory.objects.select_for_update().get(id=loser.id)
    if loser.stale:
        return None

    loser.stale = True
    loser.save(update_fields=['stale', 'updated_at'])
    RetrievalDocument.objects.filter(memory=loser).update(stale=True, updated_at=timezone.now())
    link, _created = MemoryLink.objects.get_or_create(
        memory=loser,
        link_type=LinkType.SUPERSEDED_BY,
        target=str(winner.id),
        defaults={
            'organization': loser.organization,
            'project': loser.project,
            'label': '',
        },
    )
    _audit_curator_action(
        candidate=candidate,
        event_type='MemorySuperseded',
        decision='superseded',
        near_dup_score=score,
        threshold=threshold,
        judge_context=judge_context,
        target_type='memory',
        target_id=str(loser.id),
        request_id=request_id,
        correlation_id=correlation_id,
        extra={'winner_memory_id': str(winner.id), 'loser_memory_id': str(loser.id)},
    )

    return link


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
        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

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
        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
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
                MemoryLink.objects.get_or_create(
                    memory=existing_memory,
                    link_type=LinkType.CONFLICTS_WITH,
                    target=conflict_candidate_target(locked.id),
                    defaults={
                        'organization': existing_memory.organization,
                        'project': existing_memory.project,
                        'label': 'contradiction claim',
                    },
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

        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
        if promotion.duplicate or loser.id == promotion.memory.id:
            return CurateMemoryCandidateResult(
                decision='promoted',
                candidate=promotion.candidate,
                memory=promotion.memory,
                memory_version=promotion.memory_version,
                retrieval_document=promotion.retrieval_document,
                duplicate=True,
            )

        with transaction.atomic():
            supersede_memory_system(
                loser,
                promotion.memory,
                promotion.candidate,
                score=score,
                threshold=threshold,
                judge_context=judge_context,
                request_id=f'curator:{candidate.id}',
                correlation_id=data.correlation_id,
            )

        return CurateMemoryCandidateResult(
            decision='superseded',
            candidate=promotion.candidate,
            memory=promotion.memory,
            memory_version=promotion.memory_version,
            retrieval_document=promotion.retrieval_document,
            superseded_memory=loser,
            duplicate=True,
            near_dup_score=score,
        )

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
