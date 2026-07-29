from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    MemoryCandidate,
    MemoryConflict,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.core.redaction import redact_value

logger = structlog.get_logger(__name__)

TTL_REASON = 'review_ttl_expired'
TTL_EVENT_TYPE = 'MemoryAutoRejected'

_TTL_DAYS_ENV = 'ENGRAM_CANDIDATE_REVIEW_TTL_DAYS'
_TTL_BATCH_ENV = 'ENGRAM_CANDIDATE_TTL_BATCH'
_TTL_DRY_RUN_ENV = 'ENGRAM_CANDIDATE_TTL_DRY_RUN'
_DEFAULT_TTL_DAYS = 30
_DEFAULT_BATCH = 500
_MIN_TTL_DAYS = 1
_MAX_TTL_DAYS = 3650
_MIN_BATCH = 1
_MAX_BATCH = 10000
_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})
_INACTIVE_EXECUTION_STATES = frozenset(
    {
        WorkflowWorkExecutionState.SETTLED,
        WorkflowWorkExecutionState.TERMINAL_FAILURE,
    }
)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    value = int(raw) if raw is not None else default
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be within {minimum}..{maximum}')

    return value


def candidate_review_ttl_days() -> int:
    return _bounded_int(_TTL_DAYS_ENV, _DEFAULT_TTL_DAYS, _MIN_TTL_DAYS, _MAX_TTL_DAYS)


def candidate_ttl_batch() -> int:
    return _bounded_int(_TTL_BATCH_ENV, _DEFAULT_BATCH, _MIN_BATCH, _MAX_BATCH)


def candidate_ttl_dry_run() -> bool:
    # Kill switch: when set, the sweep can only ever preview, whatever the caller asks for.
    return os.environ.get(_TTL_DRY_RUN_ENV, '').strip().lower() in _TRUTHY


@dataclass(frozen=True, slots=True)
class ExpireStaleCandidatesResult:
    scanned: int
    rejected: int
    dry_run: bool = False
    candidate_ids: tuple[str, ...] = field(default_factory=tuple)


class ExpireStaleCandidates:
    def execute(self, *, as_of: datetime | None = None, dry_run: bool | None = None) -> ExpireStaleCandidatesResult:
        now = as_of if as_of is not None else timezone.now()
        # The environment switch is a kill switch, not a default: once an operator forces preview, no
        # caller may override it. Absent the switch, an explicit argument decides.
        preview_only = candidate_ttl_dry_run() or (True if dry_run is None else dry_run)
        ttl_days = candidate_review_ttl_days()
        cutoff = now - timedelta(days=ttl_days)
        candidate_ids = list(self._expirable(cutoff).values_list('id', flat=True)[: candidate_ttl_batch()])
        if preview_only:
            logger.info(
                'candidate_ttl_preview',
                ttl_days=ttl_days,
                cutoff=cutoff.isoformat(),
                scanned=len(candidate_ids),
                candidate_ids=[str(candidate_id) for candidate_id in candidate_ids],
            )

            return ExpireStaleCandidatesResult(
                scanned=len(candidate_ids),
                rejected=0,
                dry_run=True,
                candidate_ids=tuple(str(candidate_id) for candidate_id in candidate_ids),
            )

        expired = self._expire(candidate_ids, cutoff=cutoff, ttl_days=ttl_days, now=now)
        logger.info(
            'candidate_ttl_expired',
            ttl_days=ttl_days,
            cutoff=cutoff.isoformat(),
            scanned=len(candidate_ids),
            rejected=len(expired),
        )

        return ExpireStaleCandidatesResult(
            scanned=len(candidate_ids),
            rejected=len(expired),
            dry_run=False,
            candidate_ids=tuple(str(candidate_id) for candidate_id in expired),
        )

    def _expirable(self, cutoff: datetime) -> QuerySet[MemoryCandidate]:
        unresolved_conflicts = MemoryConflict.objects.filter(
            candidate_id=OuterRef('pk'),
            resolved_transition__isnull=True,
        )
        active_work = WorkflowWork.objects.filter(
            organization_id=OuterRef('organization_id'),
            project_id=OuterRef('project_id'),
            work_type=WorkflowWorkType.CANDIDATE_DECISION,
            subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
            subject_id=OuterRef('pk'),
            disposition=WorkflowWorkDisposition.REQUIRED,
        ).exclude(execution_state__in=_INACTIVE_EXECUTION_STATES)

        return (
            MemoryCandidate.objects.filter(status=CandidateStatus.PROPOSED, created_at__lt=cutoff)
            .annotate(
                has_unresolved_conflict=Exists(unresolved_conflicts),
                has_active_decision_work=Exists(active_work),
            )
            .filter(has_unresolved_conflict=False, has_active_decision_work=False)
            .order_by('created_at', 'id')
        )

    def _expire(
        self,
        candidate_ids: list[uuid.UUID],
        *,
        cutoff: datetime,
        ttl_days: int,
        now: datetime,
    ) -> list[uuid.UUID]:
        if not candidate_ids:
            return []

        expired: list[uuid.UUID] = []
        with transaction.atomic():
            locked = (
                self._expirable(cutoff)
                .select_for_update(skip_locked=True, of=('self',))
                .filter(id__in=candidate_ids)
                .select_related('organization', 'project', 'team')
            )
            for candidate in locked:
                candidate.status = CandidateStatus.REJECTED
                candidate.save(update_fields=['status', 'updated_at'])
                self._audit(candidate, ttl_days=ttl_days, now=now)
                expired.append(candidate.id)

        return expired

    def _audit(self, candidate: MemoryCandidate, *, ttl_days: int, now: datetime) -> None:
        metadata = {
            'candidate_id': str(candidate.id),
            'decision': 'rejected',
            'reason': TTL_REASON,
            'ttl_days': ttl_days,
            'candidate_created_at': candidate.created_at.isoformat(),
            'expired_at': now.isoformat(),
            'confidence': str(candidate.confidence) if candidate.confidence is not None else None,
        }
        AuditEvent.objects.create(
            organization=candidate.organization,
            project=candidate.project,
            team=candidate.team,
            event_type=TTL_EVENT_TYPE,
            actor_type='system',
            actor_id='candidate-ttl',
            target_type='memory_candidate',
            target_id=str(candidate.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            metadata=redact_value(metadata).value,
        )

        return
