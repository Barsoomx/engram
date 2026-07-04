from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.core.models import MemoryReviewExample


class Command(BaseCommand):
    help = 'Export review-decision examples as JSON Lines for curator evaluation.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization', required=True, dest='organization_id')
        parser.add_argument('--project', default=None, dest='project_id')
        parser.add_argument('--output', default='-', dest='output')

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = uuid.UUID(str(options['organization_id']))
        project_id = uuid.UUID(str(options['project_id'])) if options['project_id'] else None
        output = str(options['output'])

        if output == '-':
            exported = export_review_examples(
                organization_id=organization_id,
                project_id=project_id,
                write=self.stdout.write,
            )
        else:
            with Path(output).open('w', encoding='utf-8') as target:
                exported = export_review_examples(
                    organization_id=organization_id,
                    project_id=project_id,
                    write=lambda line: target.write(f'{line}\n'),
                )

        self.stderr.write(f'exported={exported}')


def export_review_examples(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID | None,
    write: Callable[[str], None],
) -> int:
    queryset = MemoryReviewExample.objects.filter(organization_id=organization_id).order_by('created_at')

    if project_id is not None:
        queryset = queryset.filter(project_id=project_id)

    exported = 0
    for example in queryset.iterator(chunk_size=500):
        write(json.dumps(_serialize_review_example(example)))
        exported += 1

    return exported


def _serialize_review_example(example: MemoryReviewExample) -> dict[str, Any]:
    return {
        'id': str(example.id),
        'created_at': example.created_at.isoformat(),
        'item_type': example.item_type,
        'item_id': example.item_id,
        'action': example.action,
        'reason': example.reason,
        'snapshot': example.snapshot,
        'curator_context': example.curator_context,
        'actor_id': example.actor_id,
    }
