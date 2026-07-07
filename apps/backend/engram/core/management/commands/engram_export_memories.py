from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Prefetch
from django.utils import timezone

from engram.core.models import Memory, MemoryStatus, RetrievalDocument
from engram.core.redaction import redact_value


class Command(BaseCommand):
    help = 'Export an organization/project approved memories to a JSON backup file.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization-id', required=True)
        parser.add_argument('--project-id', required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--team-id', required=False)
        parser.add_argument('--all-statuses', action='store_true')

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = uuid.UUID(str(options['organization_id']))
        project_id = uuid.UUID(str(options['project_id']))
        team_id = uuid.UUID(str(options['team_id'])) if options['team_id'] else None
        output_path = Path(str(options['output']))

        payload = export_memories(
            organization_id=organization_id,
            project_id=project_id,
            team_id=team_id,
            all_statuses=bool(options['all_statuses']),
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('w', encoding='utf-8') as target:
            json.dump(payload, target, indent=2, sort_keys=True)

        self.stdout.write(f'memory_count={payload["memory_count"]}')


def export_memories(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    all_statuses: bool = False,
) -> dict[str, Any]:
    memories = (
        Memory.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
        )
        .prefetch_related(
            Prefetch(
                'retrieval_documents',
                queryset=RetrievalDocument.objects.select_related('memory_version'),
            ),
            'versions',
            'links',
        )
        .order_by('title')
    )

    if not all_statuses:
        memories = memories.filter(status=MemoryStatus.APPROVED)

    if team_id is not None:
        memories = memories.filter(team_id=team_id)

    serialized_memories = [_serialize_memory(memory) for memory in memories]

    return {
        'organization_id': str(organization_id),
        'project_id': str(project_id),
        'team_id': str(team_id) if team_id is not None else None,
        'exported_at': timezone.now().isoformat(),
        'memory_count': len(serialized_memories),
        'memories': serialized_memories,
    }


def _serialize_memory(memory: Memory) -> dict[str, Any]:
    versions = [
        {
            'version': version.version,
            'body': redact_value(version.body).value,
            'content_hash': redact_value(version.content_hash).value,
            'source_observation_id': str(version.source_observation_id) if version.source_observation_id else None,
        }
        for version in memory.versions.all().order_by('version')
    ]

    retrieval_document = _serialize_retrieval_document(memory)

    links = [
        {
            'link_type': link.link_type,
            'target': redact_value(link.target).value,
            'created_at': link.created_at.isoformat(),
        }
        for link in memory.links.all()
    ]

    return {
        'id': str(memory.id),
        'title': redact_value(memory.title).value,
        'body': redact_value(memory.body).value,
        'status': memory.status,
        'confidence': str(memory.confidence) if memory.confidence is not None else None,
        'stale': memory.stale,
        'refuted': memory.refuted,
        'kind': memory.kind,
        'team_id': str(memory.team_id) if memory.team_id else None,
        'visibility_scope': memory.visibility_scope,
        'current_version': memory.current_version,
        'metadata': redact_value(memory.metadata).value,
        'created_at': memory.created_at.isoformat(),
        'versions': versions,
        'links': links,
        'retrieval_document': retrieval_document,
    }


def _serialize_retrieval_document(memory: Memory) -> dict[str, Any] | None:
    document = memory.retrieval_documents.order_by('-memory_version__version').first()

    if document is None:
        return None

    return {
        'file_paths': redact_value(document.file_paths).value,
        'symbols': redact_value(document.symbols).value,
        'exact_terms': redact_value(document.exact_terms).value,
        'full_text': redact_value(document.full_text).value,
        'visibility_scope': document.visibility_scope,
    }
