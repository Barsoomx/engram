from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.core.models import RetrievalDocument, VectorField


class Command(BaseCommand):
    help = 'Copy stored embedding_vector values into embedding_pgvector where missing (operator tool).'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--batch-size', type=int, default=500, dest='batch_size')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        if VectorField is None:
            raise CommandError('pgvector is not available in this build')

        batch_size = max(1, int(options['batch_size']))
        updated = 0
        while True:
            batch = list(
                RetrievalDocument.objects.filter(embedding_pgvector__isnull=True)
                .exclude(embedding_vector=[])
                .order_by('id')[:batch_size],
            )
            if not batch:
                break

            for document in batch:
                document.embedding_pgvector = list(document.embedding_vector)
            RetrievalDocument.objects.bulk_update(batch, ['embedding_pgvector'])
            updated += len(batch)

        if options.get('as_json'):
            self.stdout.write(json.dumps({'updated': updated}))

            return

        self.stdout.write(f'backfilled embedding_pgvector for {updated} documents')
