from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from engram.core.models import Memory, MemoryStatus, RetrievalDocument
from engram.core.redaction import redact_value

EXPORT_STREAM_CHUNK_SIZE = 200


def export_queryset(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    all_statuses: bool = False,
) -> QuerySet[Memory]:
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

    return memories


def export_memories(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    all_statuses: bool = False,
) -> dict[str, Any]:
    memories = export_queryset(
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        all_statuses=all_statuses,
    )

    serialized_memories = [_serialize_memory(memory) for memory in memories]

    return {
        'organization_id': str(organization_id),
        'project_id': str(project_id),
        'team_id': str(team_id) if team_id is not None else None,
        'exported_at': timezone.now().isoformat(),
        'memory_count': len(serialized_memories),
        'memories': serialized_memories,
    }


def iter_export_memories_json(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    all_statuses: bool = False,
) -> Iterator[str]:
    memories = export_queryset(
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        all_statuses=all_statuses,
    )

    header = {
        'organization_id': str(organization_id),
        'project_id': str(project_id),
        'team_id': str(team_id) if team_id is not None else None,
        'exported_at': timezone.now().isoformat(),
    }

    yield json.dumps(header)[:-1] + ', "memories": ['

    count = 0

    for memory in memories.iterator(chunk_size=EXPORT_STREAM_CHUNK_SIZE):
        separator = '' if count == 0 else ', '
        yield separator + json.dumps(_serialize_memory(memory))
        count += 1

    yield f'], "memory_count": {count}}}'


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
