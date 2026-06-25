from __future__ import annotations

import json
import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.core.models import CandidateStatus, MemoryCandidate
from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput


class Command(BaseCommand):
    help = 'Promote one memory candidate into approved memory and index it.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('candidate_id_positional', nargs='?')
        parser.add_argument('--candidate-id')
        parser.add_argument('--project-id')
        parser.add_argument('--latest', action='store_true')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        candidate_id = self._candidate_id(options)
        result = PromoteMemoryCandidate().execute(
            PromoteMemoryCandidateInput(candidate_id=candidate_id),
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

    def _candidate_id(self, options: dict[str, Any]) -> uuid.UUID:
        raw_candidate_id = options.get('candidate_id') or options.get('candidate_id_positional')
        if raw_candidate_id:
            return uuid.UUID(str(raw_candidate_id))
        if options.get('latest') and options.get('project_id'):
            return self._latest_project_candidate_id(uuid.UUID(str(options['project_id'])))

        raise CommandError('Pass --candidate-id ID, candidate_id, or --project-id ID --latest')

    def _latest_project_candidate_id(self, project_id: uuid.UUID) -> uuid.UUID:
        try:
            candidate = MemoryCandidate.objects.filter(
                project_id=project_id,
                status=CandidateStatus.PROPOSED,
            ).latest('created_at')
        except MemoryCandidate.DoesNotExist as error:
            raise CommandError('No proposed memory candidate found for project') from error

        return candidate.id
