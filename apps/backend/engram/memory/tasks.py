from __future__ import annotations

import uuid

from celery import shared_task

from engram.memory.services import MemoryCandidateWorkerInput, ProcessObservationRecorded


@shared_task(name='engram.memory.process_observation_recorded_outbox')
def process_observation_recorded_outbox(outbox_event_id: str) -> str:
    result = ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(outbox_event_id=uuid.UUID(outbox_event_id)),
    )

    return str(result.candidate.id)
