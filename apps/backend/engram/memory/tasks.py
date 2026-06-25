from __future__ import annotations

import uuid

from engram.celery_app import app
from engram.memory.services import MemoryCandidateWorkerInput, ProcessObservationRecorded


@app.task(name='engram.memory.process_observation_recorded_outbox')
def process_observation_recorded_outbox(outbox_event_id: str) -> str:
    result = ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(outbox_event_id=uuid.UUID(outbox_event_id)),
    )

    return str(result.candidate.id)
