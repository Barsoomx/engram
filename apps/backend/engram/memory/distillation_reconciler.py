from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from engram.celery_app import app
from engram.core.models import (
    AgentSession,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)

logger = structlog.get_logger(__name__)

_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_TRANSIENT_MAX_ATTEMPTS = 10
_DISTILL_WORK_TASK_NAME = 'engram.memory.distill_session_work_v1'

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
class RetryFailedDistillationsResult:
    retried: tuple[RetriedWork, ...]
    unlinked_run_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _WorkEvaluation:
    retriable: bool
    abandoned: bool
    failed_count: int
    transient_count: int


class RetryFailedDistillations:
    def execute(self) -> RetryFailedDistillationsResult:
        cutoff = timezone.now() - self._cooldown()
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
        runs_by_work = self._runs_by_work([work.id for work in required_works])

        retried: list[RetriedWork] = []
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
            if not evaluation.retriable:
                continue

            retried_work = self._retry_under_lock(
                work.id,
                cutoff,
                max_attempts,
                transient_max_attempts,
            )
            if retried_work is not None:
                retried.append(retried_work)

        return RetryFailedDistillationsResult(
            retried=tuple(retried),
            unlinked_run_ids=unlinked_run_ids,
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

            run = WorkflowRun.objects.create(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                work_id=work.id,
                run_type=WorkflowRunType.SESSION_DISTILLATION,
                status=WorkflowRunStatus.QUEUED,
                input_snapshot=work.input_snapshot,
            )
            app.send_task(
                _DISTILL_WORK_TASK_NAME,
                args=[str(work.id), str(run.id)],
                kwargs={},
                task_id=f'workflow-work:{work.id}:run:{run.id}',
            )

            return RetriedWork(work_id=work.id, run_id=run.id)

    def _evaluate(
        self,
        work_runs: list[WorkflowRun],
        cutoff: object,
        max_attempts: int,
        transient_max_attempts: int,
    ) -> _WorkEvaluation:
        if not work_runs:
            return _WorkEvaluation(retriable=False, abandoned=False, failed_count=0, transient_count=0)

        if any(run.status == WorkflowRunStatus.SUCCEEDED for run in work_runs):
            return _WorkEvaluation(retriable=False, abandoned=False, failed_count=0, transient_count=0)

        failed_runs = [run for run in work_runs if run.status == WorkflowRunStatus.FAILED]
        failed_count = len(failed_runs)
        transient_count = sum(1 for run in failed_runs if is_transient_failure(run.failure_reason))
        non_transient_count = failed_count - transient_count

        if non_transient_count >= max_attempts or transient_count >= transient_max_attempts:
            return _WorkEvaluation(
                retriable=False,
                abandoned=True,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        latest_run = work_runs[-1]
        if latest_run.status != WorkflowRunStatus.FAILED:
            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        if latest_run.finished_at is None or latest_run.finished_at >= cutoff:
            return _WorkEvaluation(
                retriable=False,
                abandoned=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        return _WorkEvaluation(
            retriable=True,
            abandoned=False,
            failed_count=failed_count,
            transient_count=transient_count,
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
