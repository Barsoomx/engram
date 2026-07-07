from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.db.models import Count
from django.utils import timezone

from engram.core.models import AgentSession, SessionStatus, WorkflowRun, WorkflowRunStatus, WorkflowRunType

logger = structlog.get_logger(__name__)

_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_MAX_ATTEMPTS = 2
_DEFAULT_TRANSIENT_MAX_ATTEMPTS = 10

_TRANSIENT_FAILURE_MARKERS = (
    'provider returned 402',
    'provider returned 429',
    'provider returned 5',
    'provider timed out',
    'provider unreachable',
)


def is_transient_failure(failure_reason: str | None) -> bool:
    if not failure_reason:
        return False

    normalized = failure_reason.strip().lower()

    return any(marker in normalized for marker in _TRANSIENT_FAILURE_MARKERS)


@dataclass(frozen=True)
class RetryFailedDistillationsResult:
    retriable_session_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _SessionEvaluation:
    retriable: bool
    abandoned: bool
    failed_count: int
    transient_count: int


class RetryFailedDistillations:
    def execute(self) -> RetryFailedDistillationsResult:
        cutoff = timezone.now() - self._cooldown()
        max_attempts = self._max_attempts()
        transient_max_attempts = self._transient_max_attempts()

        ended_session_ids = list(
            AgentSession.objects.filter(status=SessionStatus.ENDED)
            .annotate(observation_count=Count('observations', distinct=True))
            .filter(observation_count__gt=0)
            .values_list('id', flat=True),
        )
        if not ended_session_ids:
            return RetryFailedDistillationsResult(retriable_session_ids=())

        runs_by_session = self._runs_by_session([str(session_id) for session_id in ended_session_ids])

        retriable_session_ids: list[uuid.UUID] = []
        for session_id in ended_session_ids:
            evaluation = self._evaluate(
                runs_by_session.get(str(session_id), []),
                cutoff,
                max_attempts,
                transient_max_attempts,
            )
            if evaluation.retriable:
                retriable_session_ids.append(session_id)
                continue

            if evaluation.abandoned:
                logger.warning(
                    'distillation_reconciler_abandoned',
                    session_id=str(session_id),
                    failed_count=evaluation.failed_count,
                    transient_count=evaluation.transient_count,
                )

        return RetryFailedDistillationsResult(retriable_session_ids=tuple(retriable_session_ids))

    def _runs_by_session(self, session_id_strings: list[str]) -> dict[str, list[WorkflowRun]]:
        runs = WorkflowRun.objects.filter(
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            input_snapshot__session_id__in=session_id_strings,
        ).order_by('created_at', 'id')

        runs_by_session: dict[str, list[WorkflowRun]] = {}
        for run in runs:
            runs_by_session.setdefault(run.input_snapshot.get('session_id'), []).append(run)

        return runs_by_session

    def _evaluate(
        self,
        session_runs: list[WorkflowRun],
        cutoff: object,
        max_attempts: int,
        transient_max_attempts: int,
    ) -> _SessionEvaluation:
        if not session_runs:
            return _SessionEvaluation(retriable=False, abandoned=False, failed_count=0, transient_count=0)

        if any(run.status == WorkflowRunStatus.SUCCEEDED for run in session_runs):
            return _SessionEvaluation(retriable=False, abandoned=False, failed_count=0, transient_count=0)

        failed_runs = [run for run in session_runs if run.status == WorkflowRunStatus.FAILED]
        failed_count = len(failed_runs)
        transient_count = sum(1 for run in failed_runs if is_transient_failure(run.failure_reason))
        non_transient_count = failed_count - transient_count

        if non_transient_count >= max_attempts or transient_count >= transient_max_attempts:
            return _SessionEvaluation(
                retriable=False,
                abandoned=True,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        latest_run = session_runs[-1]
        if latest_run.status != WorkflowRunStatus.FAILED:
            return _SessionEvaluation(
                retriable=False,
                abandoned=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        if latest_run.finished_at is None or latest_run.finished_at >= cutoff:
            return _SessionEvaluation(
                retriable=False,
                abandoned=False,
                failed_count=failed_count,
                transient_count=transient_count,
            )

        return _SessionEvaluation(
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
