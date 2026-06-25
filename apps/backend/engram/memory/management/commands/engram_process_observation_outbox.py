from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.core.models import OutboxEvent, OutboxStatus
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    MemoryWorkerError,
    ProcessObservationRecorded,
)


class Command(BaseCommand):
    help = 'Process pending ObservationRecorded outbox events into memory candidates.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        result = process_pending_observations(limit=int(options['limit']))
        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))

            return

        self.stdout.write(f'processed={result["processed"]}')
        self.stdout.write(f'failed={result["failed"]}')


def process_pending_observations(*, limit: int) -> dict[str, object]:
    worker = ProcessObservationRecorded()
    processed = 0
    failed = 0
    candidates: list[str] = []
    errors: list[dict[str, str]] = []
    outbox_events = OutboxEvent.objects.filter(
        event_type='ObservationRecorded',
        status=OutboxStatus.PENDING,
    ).order_by('created_at')[:limit]

    for outbox in outbox_events:
        try:
            result = worker.execute(
                MemoryCandidateWorkerInput(outbox_event_id=outbox.id, worker_id='management-command'),
            )
        except MemoryWorkerError as error:
            failed += 1
            errors.append({'outbox_event_id': str(outbox.id), 'error': str(error)})
            continue

        processed += 1
        candidates.append(str(result.candidate.id))

    return {
        'processed': processed,
        'failed': failed,
        'candidates': candidates,
        'errors': errors,
    }
