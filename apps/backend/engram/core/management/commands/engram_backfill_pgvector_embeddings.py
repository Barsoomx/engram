from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.core.models import RetrievalDocument, VectorField
from engram.model_policy.services import EMBEDDING_DIMENSION


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
        skipped = 0
        last_id = None
        while True:
            queryset = (
                RetrievalDocument.objects.filter(embedding_pgvector__isnull=True)
                .exclude(embedding_vector=[])
                .order_by('id')
            )
            if last_id is not None:
                queryset = queryset.filter(id__gt=last_id)
            batch = list(queryset[:batch_size])
            if not batch:
                break

            last_id = batch[-1].id
            fitting = []
            for document in batch:
                if len(document.embedding_vector) != EMBEDDING_DIMENSION:
                    skipped += 1
                    continue

                document.embedding_pgvector = list(document.embedding_vector)
                fitting.append(document)
            if fitting:
                RetrievalDocument.objects.bulk_update(fitting, ['embedding_pgvector'])
                updated += len(fitting)

        if options.get('as_json'):
            self.stdout.write(json.dumps({'updated': updated, 'skipped': skipped}))

            return

        self.stdout.write(f'backfilled embedding_pgvector for {updated} documents, skipped {skipped}')
