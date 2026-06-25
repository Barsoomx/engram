from __future__ import annotations

import json
import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput


class Command(BaseCommand):
    help = 'Promote one memory candidate into approved memory and index it.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('candidate_id')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        result = PromoteMemoryCandidate().execute(
            PromoteMemoryCandidateInput(candidate_id=uuid.UUID(str(options['candidate_id']))),
        )
        body = {
            'candidate_id': str(result.candidate.id),
            'memory_id': str(result.memory.id),
            'memory_version_id': str(result.memory_version.id),
            'retrieval_document_id': str(result.retrieval_document.id),
            'duplicate': result.duplicate,
        }
        if options['as_json']:
            self.stdout.write(json.dumps(body, sort_keys=True))

            return

        for key, value in body.items():
            self.stdout.write(f'{key}={value}')
