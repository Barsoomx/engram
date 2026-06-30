from __future__ import annotations

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
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)

logger = structlog.get_logger(__name__)

_MIN_SIGNAL_CHARS = 24


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


def resolve_curator_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization).values_list('curator_enabled', flat=True).first()
    )
    if enabled is None:
        return True

    return enabled


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
    if len(body) < _MIN_SIGNAL_CHARS:
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
                near_dup = self._find_near_duplicate(candidate, embedding)
                if near_dup is not None:
                    return self._supersede(candidate, near_dup, data)

            return self._promote(candidate, 'promoted')

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

    def _reject(self, candidate: MemoryCandidate, data: CurateMemoryCandidateInput) -> CurateMemoryCandidateResult:
        candidate.status = CandidateStatus.REJECTED
        candidate.save(update_fields=['status', 'updated_at'])
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
            metadata={
                'reason': 'low_signal',
                'body_length': len(redact_text(candidate.body).strip()),
            },
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

    def _find_near_duplicate(
        self,
        candidate: MemoryCandidate,
        embedding: list[float],
    ) -> tuple[RetrievalDocument, float] | None:
        documents = self._authorized_documents(candidate)
        threshold = resolve_near_dup_threshold(candidate.organization)

        return find_near_duplicate(embedding, documents, threshold)

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
