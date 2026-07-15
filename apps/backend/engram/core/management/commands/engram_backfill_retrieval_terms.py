from __future__ import annotations

import uuid
from typing import Any

import structlog
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandParser

from engram.context.term_extraction import derive_retrieval_terms
from engram.core.models import RetrievalDocument

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = 'Recompute retrieval symbols/exact_terms for existing RetrievalDocument rows (operator tool).'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization', type=str, default=None, dest='organization_id')
        parser.add_argument('--project', type=str, default=None, dest='project_id')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')

    def handle(self, *args: Any, **options: Any) -> None:
        queryset = RetrievalDocument.objects.select_related('memory', 'memory_version').order_by('id')

        if options['organization_id']:
            queryset = queryset.filter(organization_id=uuid.UUID(options['organization_id']))

        if options['project_id']:
            queryset = queryset.filter(project_id=uuid.UUID(options['project_id']))

        dry_run = options['dry_run']
        scanned = 0
        changed = 0
        failed = 0

        for document in queryset.iterator(chunk_size=200):
            scanned += 1

            memory = document.memory
            metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
            symbols, exact_terms = derive_retrieval_terms(metadata, memory.title, document.memory_version.body)

            if document.symbols == symbols and document.exact_terms == exact_terms:
                continue

            if dry_run:
                changed += 1

                continue

            document.symbols = symbols
            document.exact_terms = exact_terms
            try:
                document.save(update_fields=['symbols', 'exact_terms', 'updated_at'])
            except ValidationError as error:
                failed += 1
                logger.warning(
                    'retrieval_terms_backfill_row_failed',
                    document_id=str(document.id),
                    error=str(error),
                )

                continue

            changed += 1

        if dry_run:
            self.stdout.write(f'would_change={changed} scanned={scanned}')

            return

        self.stdout.write(f'changed={changed} failed={failed} scanned={scanned}')
