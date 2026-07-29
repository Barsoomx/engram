from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.memory.candidate_ttl import ExpireStaleCandidates


class Command(BaseCommand):
    help = 'Preview or apply the review TTL sweep over proposed memory candidates.'

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_false', dest='apply')
        mode.add_argument('--apply', action='store_true', dest='apply')
        parser.set_defaults(apply=False)
        parser.add_argument('--format', choices=('text', 'json'), default='text')

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            result = ExpireStaleCandidates().execute(dry_run=not options['apply'])
        except ValueError as error:
            raise CommandError(str(error)) from error

        payload = {
            'dry_run': result.dry_run,
            'scanned': result.scanned,
            'rejected': result.rejected,
            'candidate_ids': list(result.candidate_ids),
        }
        if options['format'] == 'json':
            self.stdout.write(json.dumps(payload, sort_keys=True, separators=(',', ':')))

            return

        self.stdout.write(f'dry_run={str(result.dry_run).lower()}')
        self.stdout.write(f'scanned={result.scanned}')
        self.stdout.write(f'rejected={result.rejected}')
        for candidate_id in result.candidate_ids:
            self.stdout.write(f'candidate_id={candidate_id}')

        return
