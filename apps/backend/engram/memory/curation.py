from __future__ import annotations

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
from engram.memory.conflict_links import clear_candidate_conflict_links
from engram.memory.escalation import escalation_reason
from engram.memory.services import (
    MemoryWorkerError,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    redact_text,
    strip_json_fence,
)
from engram.model_policy.services import (
    EmbeddingCallInput,
    ModelPolicyError,
    ProviderCallInput,
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


_GRAY_BAND_WIDTH = Decimal('0.10')
_JUDGE_DECISIONS = frozenset({'merge', 'keep_both', 'reject', 'contradicts'})
_DEFAULT_JUDGE_DECISION = 'keep_both'


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
    *,
    request_id: str = '',
    correlation_id: str = '',
    score: float | None = None,
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
    AuditEvent.objects.create(
        organization=loser.organization,
        project=loser.project,
        team=loser.team,
        event_type='MemorySuperseded',
        actor_type='system',
        target_type='memory',
        target_id=str(loser.id),
        capability='memories:review',
        result=AuditResult.RECORDED,
        request_id=request_id,
        correlation_id=correlation_id,
        metadata={
            'winner_memory_id': str(winner.id),
            'near_dup_score': f'{score:.2f}' if score is not None else '',
        },
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
            return self._promote(candidate, 'passthrough')

        embedding = embed_candidate(candidate)
        if embedding is not None:
            return self._curate_with_embedding(candidate, embedding, data)

        return self._promote(candidate, 'promoted')

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
            return self._promote(candidate, 'promoted')

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
        decision, reason = self._judge_decision(candidate, document.memory, data)
        if decision == 'merge':
            return self._supersede(candidate, near_dup, data)

        if decision == 'reject':
            return self._reject(
                candidate,
                data,
                reason='near_dup_judge_reject',
                metadata_extra={'near_dup_score': f'{score:.2f}'},
            )

        if decision == 'contradicts':
            return self._hold_for_conflict(candidate, document.memory, score, reason, data)

        return self._promote(candidate, 'promoted')

    def _judge_decision(
        self,
        candidate: MemoryCandidate,
        memory: Memory,
        data: CurateMemoryCandidateInput,
    ) -> tuple[str, str]:
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
            return _DEFAULT_JUDGE_DECISION, ''

        decision = parse_curation_decision(result.generated_body)
        reason = parse_curation_reason(result.generated_body)
        logger.info(
            'curation_judge_decision',
            candidate_id=str(candidate.id),
            memory_id=str(memory.id),
            decision=decision,
            reason=redact_text(reason),
        )

        return decision, reason

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

    def _promote(self, candidate: MemoryCandidate, decision: str) -> CurateMemoryCandidateResult:
        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

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
        metadata_extra: dict[str, object] | None = None,
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
                metadata: dict[str, object] = {
                    'reason': reason,
                    'body_length': len(redact_text(locked.body).strip()),
                }
                if metadata_extra:
                    metadata.update(metadata_extra)
                AuditEvent.objects.create(
                    organization=locked.organization,
                    project=locked.project,
                    team=locked.team,
                    event_type='MemoryAutoRejected',
                    actor_type='system',
                    target_type='memory_candidate',
                    target_id=str(locked.id),
                    capability='memories:review',
                    result=AuditResult.RECORDED,
                    correlation_id=data.correlation_id,
                    metadata=metadata,
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
                AuditEvent.objects.create(
                    organization=locked.organization,
                    project=locked.project,
                    team=locked.team,
                    event_type='MemoryCandidateHeldForReview',
                    actor_type='system',
                    target_type='memory_candidate',
                    target_id=str(locked.id),
                    capability='memories:review',
                    result=AuditResult.RECORDED,
                    correlation_id=data.correlation_id,
                    metadata={'reason': metadata_reason, 'candidate_id': str(locked.id)},
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
                    AuditEvent.objects.create(
                        organization=locked.organization,
                        project=locked.project,
                        team=locked.team,
                        event_type='MemoryConflictDetected',
                        actor_type='system',
                        target_type='memory_candidate',
                        target_id=str(locked.id),
                        capability='memories:review',
                        result=AuditResult.RECORDED,
                        correlation_id=data.correlation_id,
                        metadata={
                            'candidate_id': str(locked.id),
                            'memory_id': str(existing_memory.id),
                            'near_dup_score': f'{score:.2f}',
                            'reason': stored_reason,
                        },
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
                request_id=f'curator:{candidate.id}',
                correlation_id=data.correlation_id,
                score=score,
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
