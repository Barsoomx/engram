from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, F, Max
from django.db.models.functions import Coalesce
from django.utils import timezone

from engram.core.models import AgentSession, SessionStatus

_DEFAULT_IDLE_TIMEOUT_MINUTES = 30


@dataclass(frozen=True)
class SweepStaleSessionsResult:
    ended_session_ids: tuple[uuid.UUID, ...]
    distillable_session_ids: tuple[uuid.UUID, ...]


class SweepStaleSessions:
    def execute(self) -> SweepStaleSessionsResult:
        cutoff = timezone.now() - self._idle_timeout()
        stale_sessions = (
            AgentSession.objects.filter(status=SessionStatus.ACTIVE)
            .annotate(
                last_activity=Coalesce(Max('raw_events__received_at'), F('started_at'), F('updated_at')),
                observation_count=Count('observations', distinct=True),
            )
            .filter(last_activity__lt=cutoff)
            .values_list('id', 'observation_count')
        )

        ended_session_ids: list[uuid.UUID] = []
        distillable_session_ids: list[uuid.UUID] = []
        for session_id, observation_count in stale_sessions:
            if not self._end_session(session_id):
                continue

            ended_session_ids.append(session_id)
            if observation_count > 0:
                distillable_session_ids.append(session_id)

        return SweepStaleSessionsResult(
            ended_session_ids=tuple(ended_session_ids),
            distillable_session_ids=tuple(distillable_session_ids),
        )

    def _idle_timeout(self) -> timedelta:
        minutes = int(os.getenv('ENGRAM_SESSION_IDLE_TIMEOUT_MINUTES', str(_DEFAULT_IDLE_TIMEOUT_MINUTES)))
        return timedelta(minutes=minutes)

    def _end_session(self, session_id: uuid.UUID) -> bool:
        now = timezone.now()
        updated = AgentSession.objects.filter(id=session_id, status=SessionStatus.ACTIVE).update(
            status=SessionStatus.ENDED,
            ended_at=now,
            updated_at=now,
        )
        return updated == 1
