from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    Observation,
    SessionStatus,
    WorkflowSubjectType,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.tasks import dispatch_work_task, distill_session_work_v1
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    create_work,
    resolve_work_no_input,
)

_LIFECYCLE_EVENT_TYPES = ('session_start', 'session_end')


@dataclass(frozen=True, slots=True)
class EndSessionResult:
    session_id: uuid.UUID
    transitioned: bool
    work_id: uuid.UUID | None
    work_created: bool
    disposition: str | None
    upper_sequence_inclusive: int | None
    initial_signal_created: bool


class EndSession:
    def execute(
        self,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        session_id: uuid.UUID,
        ended_at: datetime | None,
        source: Literal['explicit', 'idle'],
    ) -> EndSessionResult:
        if ended_at is not None and timezone.is_naive(ended_at):
            raise ValueError('ended_at must be timezone-aware')

        with transaction.atomic():
            session = AgentSession.objects.select_for_update(of=('self',)).get(
                id=session_id,
                organization_id=organization_id,
                project_id=project_id,
            )

            return self._end_session_locked(session)

    def _end_session_locked(self, session: AgentSession) -> EndSessionResult:
        if session.status != SessionStatus.ACTIVE:
            return EndSessionResult(
                session_id=session.id,
                transitioned=False,
                work_id=None,
                work_created=False,
                disposition=None,
                upper_sequence_inclusive=None,
                initial_signal_created=False,
            )

        upper = self._useful_upper(session)

        session.status = SessionStatus.ENDED
        session.ended_at = timezone.now()
        session.end_work_contract_version = 1
        session.save(update_fields=['status', 'ended_at', 'end_work_contract_version', 'updated_at'])

        work, created = create_work(
            CreateWorkflowWorkInput(
                organization_id=session.organization_id,
                project_id=session.project_id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=session.id,
                input_snapshot={
                    'schema': 'session_distillation_input/v1',
                    'session_id': str(session.id),
                    'lower_sequence_exclusive': 0,
                    'upper_sequence_inclusive': upper,
                },
            )
        )

        disposition = work.disposition
        initial_signal_created = False
        if upper == 0 and created and work.disposition == WorkflowWorkDisposition.REQUIRED:
            resolved = resolve_work_no_input(
                work.id,
                organization_id=session.organization_id,
                project_id=session.project_id,
            )
            disposition = resolved.disposition
        elif upper > 0 and created:
            dispatch_work_task(distill_session_work_v1, work.id)
            initial_signal_created = True

        return EndSessionResult(
            session_id=session.id,
            transitioned=True,
            work_id=work.id,
            work_created=created,
            disposition=disposition,
            upper_sequence_inclusive=upper,
            initial_signal_created=initial_signal_created,
        )

    def _useful_upper(self, session: AgentSession) -> int:
        upper = (
            Observation.objects.filter(
                organization_id=session.organization_id,
                project_id=session.project_id,
                session_id=session.id,
            )
            .filter(
                Q(source_metadata__event_type__isnull=True) | ~Q(source_metadata__event_type__in=_LIFECYCLE_EVENT_TYPES)
            )
            .aggregate(upper=Max('session_sequence'))
            .get('upper')
        )

        return upper or 0
