from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.conf import settings
from django.db import transaction

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
from engram.memory.services import (
    MemoryWorkerError,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    redact_text,
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
_JUDGE_DECISIONS = frozenset({'merge', 'keep_both', 'reject'})
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
        '- Output a single JSON object only, with exactly one key "decision".\n'
        '- "decision" is one of "merge", "keep_both", "reject".\n'
        '- "merge": the same durable fact; the new candidate should supersede the existing memory.\n'
        '- "keep_both": the two memories are distinct durable facts and both should be kept.\n'
        '- "reject": the new candidate adds no durable value beyond the existing memory.\n'
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
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return _DEFAULT_JUDGE_DECISION

    if not isinstance(parsed, dict):
        return _DEFAULT_JUDGE_DECISION

    decision = str(parsed.get('decision') or '').strip().lower()
    if decision in _JUDGE_DECISIONS:
        return decision

    return _DEFAULT_JUDGE_DECISION


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
            'curator embedding skipped: provider secret unavailable',
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
        with transaction.atomic():
            candidate = self._lock_candidate(data.candidate_id)
            if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
                return self._replay(candidate)
            if candidate.status != CandidateStatus.PROPOSED:
                raise MemoryWorkerError('Only proposed memory candidates can be curated')

            if not resolve_curator_enabled(candidate.organization):
                return self._promote(candidate, 'passthrough')

            if is_low_signal(candidate):
                return self._reject(candidate, data)

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
        decision = self._judge_decision(candidate, document.memory, data)
        if decision == 'merge':
            return self._supersede(candidate, near_dup, data)

        if decision == 'reject':
            return self._reject(
                candidate,
                data,
                reason='near_dup_judge_reject',
                metadata_extra={'near_dup_score': f'{score:.2f}'},
            )

        return self._promote(candidate, 'promoted')

    def _judge_decision(
        self,
        candidate: MemoryCandidate,
        memory: Memory,
        data: CurateMemoryCandidateInput,
    ) -> str:
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
            return _DEFAULT_JUDGE_DECISION

        return parse_curation_decision(result.generated_body)

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
        candidate.status = CandidateStatus.REJECTED
        candidate.save(update_fields=['status', 'updated_at'])
        metadata: dict[str, object] = {
            'reason': reason,
            'body_length': len(redact_text(candidate.body).strip()),
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        AuditEvent.objects.create(
            organization=candidate.organization,
            project=candidate.project,
            team=candidate.team,
            event_type='MemoryAutoRejected',
            actor_type='system',
            target_type='memory_candidate',
            target_id=str(candidate.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            correlation_id=data.correlation_id,
            metadata=metadata,
        )

        return CurateMemoryCandidateResult(decision='rejected', candidate=candidate, memory=None)

    def _supersede(
        self,
        candidate: MemoryCandidate,
        near_dup: tuple[RetrievalDocument, float],
        data: CurateMemoryCandidateInput,
    ) -> CurateMemoryCandidateResult:
        document, score = near_dup
        loser = document.memory
        promotion = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
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
        )

        return authorized_retrieval_documents(candidate.organization, candidate.project, scope)
