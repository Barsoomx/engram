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
    MemoryLink,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    RetrievalDocument,
)
from engram.core.redaction import redact_value
from engram.memory.conflict_links import clear_candidate_conflict_links
from engram.memory.escalation import escalation_reason
from engram.memory.services import (
    MemoryWorkerError,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    redact_text,
    strip_json_fence,
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


def audit_curator_action(
    *,
    candidate: MemoryCandidate,
    event_type: str,
    decision: str,
    reason: str = '',
    near_dup_score: float | None = None,
    threshold: Decimal | None = None,
    judge_context: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
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
        target_type='memory_candidate',
        target_id=str(candidate.id),
        capability='memories:review',
        result=AuditResult.RECORDED,
        metadata=redact_value(metadata).value,
    )


def _judge_context_snapshot(title: str, body: str) -> dict[str, object]:
    return {
        'title': redact_text(title)[:120],
        'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
        'body_length': len(body),
    }


def build_judge_context(
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
    judge_context: dict[str, object] | None = None,
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
    audit_curator_action(
        candidate=candidate,
        event_type='MemorySuperseded',
        decision='superseded',
        near_dup_score=score,
        judge_context=judge_context,
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

        if is_low_signal(candidate):
            return self._reject(candidate, data)

        reason = escalation_reason(candidate)
        if reason:
            return self._hold_for_escalation(candidate, reason, data)

        if not resolve_curator_enabled(candidate.organization):
            return self._promote(candidate, 'passthrough', route='passthrough')

        embedding = embed_candidate(candidate)
        if embedding is not None:
            return self._curate_with_embedding(candidate, embedding, data)

        return self._promote(candidate, 'promoted', route='embedding_unavailable')

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
            return self._promote(candidate, 'promoted', route='no_duplicate')

        _document, score = near_dup
        if score >= float(threshold):
            return self._supersede(candidate, near_dup, data)

        return self._judge(candidate, near_dup, data)

    def _judge(
        self,
        candidate: MemoryCandidate,
        near_dup: tuple[RetrievalDocument, float],
        data: CurateMemoryCandidateInput,
    ) -> CurateMemoryCandidateResult:
        document, score = near_dup
        outcome = self._judge_decision(candidate, document.memory, data)
        if outcome.decision == 'merge':
            return self._supersede(candidate, near_dup, data, judge_context=outcome.judge_context)

        if outcome.decision == 'reject':
            return self._reject(
                candidate,
                data,
                reason='near_dup_judge_reject',
                near_dup_score=score,
                judge_context=outcome.judge_context,
            )

        if outcome.decision == 'contradicts':
            return self._hold_for_conflict(
                candidate,
                document.memory,
                score,
                outcome.reason,
                data,
                judge_context=outcome.judge_context,
            )

        return self._promote(candidate, 'promoted', route='judge_keep_both', judge_context=outcome.judge_context)

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

        return _JudgeOutcome(decision, reason, build_judge_context(resolved.policy, result, candidate, memory))

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
        *,
        route: str,
        judge_context: dict[str, object] | None = None,
    ) -> CurateMemoryCandidateResult:
        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
        if not promotion.duplicate:
            audit_curator_action(
                candidate=promotion.candidate,
                event_type='MemoryCuratorPromoted',
                decision=route,
                judge_context=judge_context,
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
    ) -> CurateMemoryCandidateResult:
        already_settled = False
        with transaction.atomic():
            locked = self._lock_candidate(candidate.id)
            if locked.status != CandidateStatus.PROPOSED:
                already_settled = True
            else:
                locked.status = CandidateStatus.REJECTED
                locked.save(update_fields=['status', 'updated_at'])
                clear_candidate_conflict_links(locked)
                audit_curator_action(
                    candidate=locked,
                    event_type='MemoryAutoRejected',
                    decision='rejected',
                    reason=reason,
                    near_dup_score=near_dup_score,
                    judge_context=judge_context,
                    extra={'body_length': len(redact_text(locked.body).strip())},
                )

        if already_settled:
            return self._reconcile_already_handled(locked)

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
        with transaction.atomic():
            locked = self._lock_candidate(candidate.id)
            if locked.status != CandidateStatus.PROPOSED:
                already_settled = True
            elif not self._has_escalation_audit(locked):
                audit_curator_action(
                    candidate=locked,
                    event_type='MemoryCandidateHeldForReview',
                    decision='held_escalation',
                    reason=metadata_reason,
                )

        if already_settled:
            return self._reconcile_already_handled(locked)

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
    ) -> CurateMemoryCandidateResult:
        stored_reason = redact_text(reason)[:200]
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
                    target=f'candidate:{locked.id}',
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
                    audit_curator_action(
                        candidate=locked,
                        event_type='MemoryConflictDetected',
                        decision='held_conflict',
                        reason=stored_reason,
                        near_dup_score=score,
                        judge_context=judge_context,
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
    ) -> CurateMemoryCandidateResult:
        document, score = near_dup
        loser = document.memory
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
                judge_context=judge_context,
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

        return authorized_retrieval_documents(candidate.organization, candidate.project, scope)
