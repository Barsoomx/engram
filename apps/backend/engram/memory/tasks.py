from __future__ import annotations

import uuid

from engram.celery_app import app
from engram.memory.services import MemoryCandidateWorkerInput, MemoryWorkerError, ProcessObservationRecorded


@app.task(name='engram.memory.process_observation_recorded')
def process_observation_recorded(observation_id: object) -> str:
    try:
        parsed_observation_id = uuid.UUID(observation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise MemoryWorkerError('malformed observation id') from error

    result = ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(observation_id=parsed_observation_id),
    )

    return str(result.candidate.id)
