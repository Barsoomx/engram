from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.aware_time import require_aware
from engram.memory.work_dispatch import queue_work_attempt

_RECOVERABLE_WORK_TYPES = (
    WorkflowWorkType.OBSERVATION_PROCESSING,
    WorkflowWorkType.DAILY_DIGEST,
    WorkflowWorkType.WEEKLY_DIGEST,
    WorkflowWorkType.MEMORY_EMBEDDING,
)


@dataclass(frozen=True, slots=True)
class WorkRecoveryResult:
    scanned: int
    queued: int


def _due_predicate(as_of: datetime) -> Q:
    retry_due = Q(
        execution_state=WorkflowWorkExecutionState.RETRY_WAIT,
        next_retry_at__isnull=False,
        next_retry_at__lte=as_of,
    )
    lease_expired = Q(
        execution_state=WorkflowWorkExecutionState.LEASED,
        lease_expires_at__isnull=False,
        lease_expires_at__lt=as_of,
    )

    return retry_due | lease_expired


def _strand_ids(as_of: datetime) -> list[uuid.UUID]:
    return list(
        WorkflowWork.objects.filter(
            work_type__in=_RECOVERABLE_WORK_TYPES,
            disposition=WorkflowWorkDisposition.REQUIRED,
        )
        .filter(_due_predicate(as_of))
        .order_by('created_at', 'id')
        .values_list('id', flat=True)
    )


def _is_due(work: WorkflowWork, as_of: datetime) -> bool:
    if (
        work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
        and work.next_retry_at is not None
        and work.next_retry_at <= as_of
    ):
        return True

    return (
        work.execution_state == WorkflowWorkExecutionState.LEASED
        and work.lease_expires_at is not None
        and work.lease_expires_at < as_of
    )


def _blocking_attempt_exists(work: WorkflowWork, as_of: datetime) -> bool:
    runs = WorkflowRun.objects.filter(
        work_id=work.id,
        status__in=(WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING),
    )
    if not runs.exists():
        return False

    lease_expired = (
        work.execution_state == WorkflowWorkExecutionState.LEASED
        and work.lease_expires_at is not None
        and work.lease_expires_at < as_of
    )
    if not lease_expired:
        return True

    return runs.filter(status=WorkflowRunStatus.QUEUED).exists()


def _recover_one(work_id: uuid.UUID, as_of: datetime) -> bool:
    with transaction.atomic():
        try:
            work = WorkflowWork.objects.select_for_update().get(id=work_id)
        except WorkflowWork.DoesNotExist:
            return False
        if work.work_type not in _RECOVERABLE_WORK_TYPES or work.disposition != WorkflowWorkDisposition.REQUIRED:
            return False
        if not _is_due(work, as_of):
            return False
        if _blocking_attempt_exists(work, as_of):
            return False
        run = queue_work_attempt(work_id=work.id, now=as_of, origin=WorkflowRunOrigin.RECONCILIATION)

        return run.dispatched_at == as_of


class RecoverStrandedWork:
    def execute(self, as_of: datetime | None = None) -> WorkRecoveryResult:
        if as_of is None:
            as_of = timezone.now()
        require_aware(as_of)

        work_ids = _strand_ids(as_of)
        queued = sum(1 for work_id in work_ids if _recover_one(work_id, as_of))

        return WorkRecoveryResult(scanned=len(work_ids), queued=queued)


def recover_scheduled_stranded_work(*, as_of: datetime) -> int:
    return RecoverStrandedWork().execute(as_of=as_of).queued
