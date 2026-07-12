from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import F, Max
from django.db.models.functions import Coalesce
from django.utils import timezone

from engram.core.models import AgentSession, SessionStatus

if TYPE_CHECKING:
    from engram.memory.session_lifecycle import EndSessionResult

_DEFAULT_IDLE_TIMEOUT_MINUTES = 30


@dataclass(frozen=True)
class SweepStaleSessionsResult:
    ended_session_ids: tuple[uuid.UUID, ...]
    distillable_session_ids: tuple[uuid.UUID, ...]


class SweepStaleSessions:
    def execute(self) -> SweepStaleSessionsResult:
        cutoff = timezone.now() - self._idle_timeout()
        stale_session_ids = list(
            AgentSession.objects.filter(status=SessionStatus.ACTIVE)
            .annotate(
                last_activity=Coalesce(Max('raw_events__received_at'), F('started_at'), F('updated_at')),
            )
            .filter(last_activity__lt=cutoff)
            .values_list('id', flat=True),
        )

        ended_session_ids: list[uuid.UUID] = []
        distillable_session_ids: list[uuid.UUID] = []
        for session_id in stale_session_ids:
            result = self._end_session(session_id)
            if result is None:
                continue

            ended_session_ids.append(session_id)
            if result.initial_signal_created:
                distillable_session_ids.append(session_id)

        return SweepStaleSessionsResult(
            ended_session_ids=tuple(ended_session_ids),
            distillable_session_ids=tuple(distillable_session_ids),
        )

    def _idle_timeout(self) -> timedelta:
        minutes = int(os.getenv('ENGRAM_SESSION_IDLE_TIMEOUT_MINUTES', str(_DEFAULT_IDLE_TIMEOUT_MINUTES)))
        return timedelta(minutes=minutes)

    def _end_session(self, session_id: uuid.UUID) -> EndSessionResult | None:
        from engram.memory.session_lifecycle import EndSession

        scope = AgentSession.objects.filter(id=session_id).values('organization_id', 'project_id').first()
        if scope is None:
            return None

        result = EndSession().execute(
            organization_id=scope['organization_id'],
            project_id=scope['project_id'],
            session_id=session_id,
            ended_at=None,
            source='idle',
        )
        if not result.transitioned:
            return None

        return result
