from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from engram.core.models import AgentSession, SessionStatus, WorkflowRun, WorkflowRunStatus, WorkflowRunType

_DEFAULT_COOLDOWN_MINUTES = 30
_DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class RetryFailedDistillationsResult:
    retriable_session_ids: tuple[uuid.UUID, ...]


class RetryFailedDistillations:
    def execute(self) -> RetryFailedDistillationsResult:
        cutoff = timezone.now() - self._cooldown()
        max_attempts = self._max_attempts()

        ended_session_ids = list(
            AgentSession.objects.filter(status=SessionStatus.ENDED)
            .annotate(observation_count=Count('observations', distinct=True))
            .filter(observation_count__gt=0)
            .values_list('id', flat=True),
        )
        if not ended_session_ids:
            return RetryFailedDistillationsResult(retriable_session_ids=())

        runs_by_session = self._runs_by_session([str(session_id) for session_id in ended_session_ids])

        retriable_session_ids = tuple(
            session_id
            for session_id in ended_session_ids
            if self._is_retriable(runs_by_session.get(str(session_id), []), cutoff, max_attempts)
        )

        return RetryFailedDistillationsResult(retriable_session_ids=retriable_session_ids)

    def _runs_by_session(self, session_id_strings: list[str]) -> dict[str, list[WorkflowRun]]:
        runs = WorkflowRun.objects.filter(
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            input_snapshot__session_id__in=session_id_strings,
        ).order_by('created_at', 'id')

        runs_by_session: dict[str, list[WorkflowRun]] = {}
        for run in runs:
            runs_by_session.setdefault(run.input_snapshot.get('session_id'), []).append(run)

        return runs_by_session

    def _is_retriable(self, session_runs: list[WorkflowRun], cutoff: object, max_attempts: int) -> bool:
        if not session_runs:
            return False

        if any(run.status == WorkflowRunStatus.SUCCEEDED for run in session_runs):
            return False

        failed_count = sum(1 for run in session_runs if run.status == WorkflowRunStatus.FAILED)
        if failed_count >= max_attempts:
            return False

        latest_run = session_runs[-1]
        if latest_run.status != WorkflowRunStatus.FAILED:
            return False

        if latest_run.finished_at is None or latest_run.finished_at >= cutoff:
            return False

        return True

    def _cooldown(self) -> timedelta:
        minutes = int(os.getenv('ENGRAM_DISTILL_RECONCILE_COOLDOWN_MINUTES', str(_DEFAULT_COOLDOWN_MINUTES)))
        return timedelta(minutes=minutes)

    def _max_attempts(self) -> int:
        return int(os.getenv('ENGRAM_DISTILL_RECONCILE_MAX_ATTEMPTS', str(_DEFAULT_MAX_ATTEMPTS)))
