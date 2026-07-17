from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.work_dispatch import queue_work_attempt

logger = structlog.get_logger(__name__)

_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_TRANSIENT_MAX_ATTEMPTS = 10
_LEGACY_CONTRACT_VERSION = 0
_POISONED_RUN_FAILURE_REASON = 'legacy_run_contract_mismatch'

_TRANSIENT_FAILURE_MARKERS = (
    'provider returned 402',
    'provider returned 429',
    'provider returned 5',
    'provider timed out',
    'provider unreachable',
)


def _v1_managed_session() -> Exists:
    return Exists(
        AgentSession.objects.filter(
            id=OuterRef('subject_id'),
            end_work_contract_version=1,
        )
    )


def is_transient_failure(failure_reason: str | None) -> bool:
    if not failure_reason:
        return False

    normalized = failure_reason.strip().lower()

    return any(marker in normalized for marker in _TRANSIENT_FAILURE_MARKERS)


@dataclass(frozen=True)
class RetriedWork:
    work_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True)
class ResignaledWork:
    work_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True)
class RetryFailedDistillationsResult:
    retried: tuple[RetriedWork, ...]
    unlinked_run_ids: tuple[uuid.UUID, ...]
    resignaled: tuple[ResignaledWork, ...] = ()


@dataclass(frozen=True)
class _WorkEvaluation:
    retriable: bool
    abandoned: bool
    resignal_queued: bool
    failed_count: int
    transient_count: int


class RetryFailedDistillations:
    def execute(self) -> RetryFailedDistillationsResult:
        now = timezone.now()
        cutoff = now - self._cooldown()
        max_attempts = self._max_attempts()
        transient_max_attempts = self._transient_max_attempts()

        unlinked_run_ids = tuple(self._unlinked_failed_run_ids())

        required_works = list(
            WorkflowWork.objects.filter(
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                disposition=WorkflowWorkDisposition.REQUIRED,
            )
            .exclude(_v1_managed_session())
            .order_by('created_at', 'id'),
        )
        work_ids = [work.id for work in required_works]
        self._terminalize_poisoned_runs(work_ids, now)
        runs_by_work = self._runs_by_work(work_ids)

        retried: list[RetriedWork] = []
        resignaled: list[ResignaledWork] = []
        for work in required_works:
            evaluation = self._evaluate(
                runs_by_work.get(work.id, []),
                cutoff,
                max_attempts,
                transient_max_attempts,
            )
            if evaluation.abandoned:
                logger.warning(
                    'distillation_reconciler_abandoned',
                    work_id=str(work.id),
                    failed_count=evaluation.failed_count,
                    transient_count=evaluation.transient_count,
                )

                continue
            if evaluation.resignal_queued:
                resignaled_work = self._resignal_under_lock(
                    work.id,
                    now,
                    cutoff,
                    max_attempts,
                    transient_max_attempts,
                )
                if resignaled_work is not None:
                    resignaled.append(resignaled_work)

                continue
            if not evaluation.retriable:
                continue

            retried_work = self._retry_under_lock(
                work.id,
                now,
                cutoff,
                max_attempts,
                transient_max_attempts,
            )
            if retried_work is not None:
                retried.append(retried_work)

        return RetryFailedDistillationsResult(
            retried=tuple(retried),
            unlinked_run_ids=unlinked_run_ids,
            resignaled=tuple(resignaled),
        )

    def _unlinked_failed_run_ids(self) -> list[uuid.UUID]:
        return list(
            WorkflowRun.objects.filter(
                run_type=WorkflowRunType.SESSION_DISTILLATION,
                work__isnull=True,
                status=WorkflowRunStatus.FAILED,
            )
            .order_by('created_at', 'id')
            .values_list('id', flat=True),
        )

    def _terminalize_poisoned_runs(self, work_ids: list[uuid.UUID], now: datetime) -> None:
        if not work_ids:
            return

        poisoned_run_ids = list(
            WorkflowRun.objects.filter(
                run_type=WorkflowRunType.SESSION_DISTILLATION,
                work_id__in=work_ids,
                execution_contract_version=_LEGACY_CONTRACT_VERSION,
                status=WorkflowRunStatus.QUEUED,
            ).values_list('id', flat=True),
        )
        if not poisoned_run_ids:
            return

        WorkflowRun.objects.filter(id__in=poisoned_run_ids).update(
            status=WorkflowRunStatus.FAILED,
            failure_reason=_POISONED_RUN_FAILURE_REASON,
            finished_at=now,
            updated_at=now,
        )
        logger.warning(
            'distillation_reconciler_poisoned_runs_terminalized',
            run_count=len(poisoned_run_ids),
        )

        return

    def _runs_by_work(self, work_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[WorkflowRun]]:
        runs = WorkflowRun.objects.filter(
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            work_id__in=work_ids,
        ).order_by('created_at', 'id')

        runs_by_work: dict[uuid.UUID, list[WorkflowRun]] = {}
        for run in runs:
            runs_by_work.setdefault(run.work_id, []).append(run)

        return runs_by_work

    def _retry_under_lock(
        self,
        work_id: uuid.UUID,
        now: datetime,
        cutoff: object,
        max_attempts: int,
        transient_max_attempts: int,
    ) -> RetriedWork | None:
        with transaction.atomic():
            try:
                work = (
                    WorkflowWork.objects.select_for_update()
                    .exclude(_v1_managed_session())
                    .get(
                        id=work_id,
                        work_type=WorkflowWorkType.SESSION_DISTILLATION,
                        disposition=WorkflowWorkDisposition.REQUIRED,
                    )
                )
            except WorkflowWork.DoesNotExist:
                return None

            runs = list(
                WorkflowRun.objects.filter(
                    work_id=work.id,
                    run_type=WorkflowRunType.SESSION_DISTILLATION,
                ).order_by('created_at', 'id'),
            )
            evaluation = self._evaluate(runs, cutoff, max_attempts, transient_max_attempts)
            if not evaluation.retriable:
                return None

            run = queue_work_attempt(
                work_id=work.id,
                now=now,
                origin=WorkflowRunOrigin.RECONCILIATION,
            )

            return RetriedWork(work_id=work.id, run_id=run.id)

    def _resignal_under_lock(
        self,
        work_id: uuid.UUID,
        now: datetime,
        cutoff: object,
        max_attempts: int,
        transient_max_attempts: int,
    ) -> ResignaledWork | None:
        with transaction.atomic():
            try:
                work = (
                    WorkflowWork.objects.select_for_update()
                    .exclude(_v1_managed_session())
                    .get(
                        id=work_id,
                        work_type=WorkflowWorkType.SESSION_DISTILLATION,
                        disposition=WorkflowWorkDisposition.REQUIRED,
                    )
                )
            except WorkflowWork.DoesNotExist:
                return None

            runs = list(
                WorkflowRun.objects.filter(
                    work_id=work.id,
                    run_type=WorkflowRunType.SESSION_DISTILLATION,
                ).order_by('created_at', 'id'),
            )
            evaluation = self._evaluate(runs, cutoff, max_attempts, transient_max_attempts)
            if not evaluation.resignal_queued:
                return None

            run = queue_work_attempt(
                work_id=work.id,
                now=now,
                origin=WorkflowRunOrigin.RECONCILIATION,
            )
            if run.dispatched_at != now:
                return None

            return ResignaledWork(work_id=work.id, run_id=run.id)

    def _evaluate(
        self,
        work_runs: list[WorkflowRun],
        cutoff: object,
        max_attempts: int,
        transient_max_attempts: int,
    ) -> _WorkEvaluation:
        attempts = [run for run in work_runs if not self._is_poisoned_cleanup(run)]
        if not attempts:
            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                resignal_queued=False,
                failed_count=0,
                transient_count=0,
            )

        if any(run.status == WorkflowRunStatus.SUCCEEDED for run in attempts):
            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                resignal_queued=False,
                failed_count=0,
                transient_count=0,
            )

        failed_runs = [run for run in attempts if run.status == WorkflowRunStatus.FAILED]
        failed_count = len(failed_runs)
        transient_count = sum(1 for run in failed_runs if is_transient_failure(run.failure_reason))
        non_transient_count = failed_count - transient_count

        if non_transient_count >= max_attempts or transient_count >= transient_max_attempts:
            return _WorkEvaluation(
                retriable=False,
                abandoned=True,
                resignal_queued=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        latest_run = attempts[-1]
        if latest_run.status != WorkflowRunStatus.FAILED:
            resignal_queued = (
                latest_run.status == WorkflowRunStatus.QUEUED and latest_run.execution_contract_version == 1
            )

            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                resignal_queued=resignal_queued,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        if latest_run.finished_at is None or latest_run.finished_at >= cutoff:
            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                resignal_queued=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        return _WorkEvaluation(
            retriable=True,
            abandoned=False,
            resignal_queued=False,
            failed_count=failed_count,
            transient_count=transient_count,
        )

    def _is_poisoned_cleanup(self, run: WorkflowRun) -> bool:
        return (
            run.execution_contract_version == _LEGACY_CONTRACT_VERSION
            and run.failure_reason == _POISONED_RUN_FAILURE_REASON
        )

    def _cooldown(self) -> timedelta:
        minutes = int(os.getenv('ENGRAM_DISTILL_RECONCILE_COOLDOWN_MINUTES', str(_DEFAULT_COOLDOWN_MINUTES)))

        return timedelta(minutes=minutes)

    def _max_attempts(self) -> int:
        return int(os.getenv('ENGRAM_DISTILL_RECONCILE_MAX_ATTEMPTS', str(_DEFAULT_MAX_ATTEMPTS)))

    def _transient_max_attempts(self) -> int:
        return int(
            os.getenv('ENGRAM_DISTILL_RECONCILE_TRANSIENT_MAX_ATTEMPTS', str(_DEFAULT_TRANSIENT_MAX_ATTEMPTS)),
        )
