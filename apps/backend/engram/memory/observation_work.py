from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Max
from django.db.transaction import TransactionManagementError

from engram.core.models import AgentSession, Observation, RawEventEnvelope


def _require_active_transaction() -> None:
    if not transaction.get_connection().in_atomic_block:
        raise TransactionManagementError('observation sequencing requires an active transaction')


def lock_session_for_observation(*, organization_id: UUID, project_id: UUID, session_id: UUID) -> AgentSession:
    _require_active_transaction()
    return AgentSession.objects.select_for_update(of=('self',)).get(
        organization_id=organization_id, project_id=project_id, id=session_id
    )


def session_has_observation_history(*, session_id: UUID) -> bool:
    has_raw_event = RawEventEnvelope.objects.filter(session_id=session_id).exists()
    has_observation = Observation.objects.filter(session_id=session_id).exists()

    return has_raw_event or has_observation


def allocate_observation_sequence(session: AgentSession) -> int:
    _require_active_transaction()
    current_cursor = session.observation_sequence_cursor
    if current_cursor is None:
        existing_max = (
            Observation.objects.filter(session_id=session.id, session_sequence__gt=0)
            .aggregate(max_sequence=Max('session_sequence'))
            .get('max_sequence')
            or 0
        )
        sequence = existing_max + 1
    else:
        sequence = current_cursor + 1
    session.observation_sequence_cursor = sequence
    session.save(update_fields=['observation_sequence_cursor'])
    return sequence
