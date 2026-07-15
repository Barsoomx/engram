from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from engram.core.models import CandidateStatus, MemoryCandidate, MemoryConflict, Organization
from engram.memory.conflict_links import clear_candidate_conflict_links
from engram.memory.curation import _audit_curator_action
from engram.memory.services import redact_text, resolve_auto_approve_threshold

_TTL_REASON = 'review_ttl_expired'


@dataclass(frozen=True)
class ExpireStaleCandidatesResult:
    scanned: int
    rejected: int


class ExpireStaleCandidates:
    def execute(self) -> ExpireStaleCandidatesResult:
        cutoff = timezone.now() - timedelta(days=settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS)

        unresolved_conflicts = MemoryConflict.objects.filter(
            candidate_id=OuterRef('pk'),
            resolved_transition__isnull=True,
        )
        stale = MemoryCandidate.objects.filter(
            status=CandidateStatus.PROPOSED,
            created_at__lt=cutoff,
        ).filter(~Exists(unresolved_conflicts))
        thresholds = self._resolve_thresholds(stale)
        if not thresholds:
            return ExpireStaleCandidatesResult(scanned=0, rejected=0)

        batch = settings.ENGRAM_CANDIDATE_TTL_BATCH
        eligible_ids = list(
            stale.filter(self._confidence_filter(thresholds))
            .order_by('created_at')
            .values_list('id', flat=True)[:batch],
        )

        rejected = self._reject_batch(eligible_ids)

        return ExpireStaleCandidatesResult(scanned=len(eligible_ids), rejected=rejected)

    def _resolve_thresholds(self, stale: QuerySet[MemoryCandidate]) -> dict[uuid.UUID, Decimal]:
        org_ids = list(stale.values_list('organization_id', flat=True).distinct())

        return {
            organization.id: resolve_auto_approve_threshold(organization)
            for organization in Organization.objects.filter(id__in=org_ids)
        }

    def _confidence_filter(self, thresholds: dict[uuid.UUID, Decimal]) -> Q:
        combined = Q()
        for organization_id, threshold in thresholds.items():
            combined |= Q(organization_id=organization_id) & (Q(confidence__lt=threshold) | Q(confidence__isnull=True))

        return combined

    def _reject_batch(self, candidate_ids: list[uuid.UUID]) -> int:
        rejected = 0
        with transaction.atomic():
            locked = (
                MemoryCandidate.objects.select_for_update(skip_locked=True, of=('self',))
                .filter(id__in=candidate_ids, status=CandidateStatus.PROPOSED)
                .select_related('organization', 'project', 'team', 'source_observation')
            )
            for candidate in locked:
                if MemoryConflict.objects.filter(
                    candidate_id=candidate.id,
                    resolved_transition__isnull=True,
                ).exists():
                    continue
                candidate.status = CandidateStatus.REJECTED
                candidate.save(update_fields=['status', 'updated_at'])
                clear_candidate_conflict_links(candidate)
                _audit_curator_action(
                    candidate=candidate,
                    event_type='MemoryAutoRejected',
                    decision='rejected',
                    reason=_TTL_REASON,
                    extra={
                        'ttl_days': settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS,
                        'body_length': len(redact_text(candidate.body).strip()),
                    },
                )
                rejected += 1

        return rejected
