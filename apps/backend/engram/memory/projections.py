from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from engram.core.models import Memory, MemoryVersion, RetrievalDocument


@dataclass(frozen=True, slots=True)
class ExactMemoryProjection:
    document_values: dict[str, object]
    exact_projection_hash: str


def _attr(obj: object, name: str, default: object = None) -> object:
    value = getattr(obj, name, default)
    if value is not None:
        return value
    metadata = getattr(obj, 'metadata', None) or getattr(obj, 'source_metadata', None)
    if isinstance(metadata, dict):
        return metadata.get(name, default)
    return default


def _canonical(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json_bytes(value: object) -> bytes:
    from engram.memory.workflow_work import canonical_json_bytes

    return canonical_json_bytes(_canonical(value))


def build_exact_memory_projection(
    *, memory: object, version: object, transition_id: uuid.UUID, sources: list[object] | tuple[object, ...]
) -> ExactMemoryProjection:
    file_paths = _attr(version, 'file_paths', _attr(memory, 'file_paths', [])) or []
    symbols = _attr(version, 'symbols', _attr(memory, 'symbols', [])) or []
    exact_terms = _attr(version, 'exact_terms', _attr(memory, 'exact_terms', [])) or []
    source_observation_ids = _attr(version, 'source_observation_ids', _attr(memory, 'source_observation_ids', [])) or []
    title = _attr(version, 'title', _attr(memory, 'title', '')) or ''
    body = _attr(version, 'body', _attr(memory, 'body', '')) or ''
    full_text = _attr(version, 'full_text', _attr(memory, 'full_text', '')) or f'{title}\n{body}'.strip()
    visibility = _attr(memory, 'visibility_scope', 'project')
    source_values = [
        {
            'id': _attr(source, 'id'),
            'source_kind': _attr(source, 'source_kind', _attr(source, 'source_type', _attr(source, 'kind', ''))),
            'source_content_hash': _attr(source, 'source_content_hash', _attr(source, 'content_hash', '')),
            'candidate_source_id': _attr(source, 'candidate_source_id'),
            'source_memory_version_id': _attr(source, 'source_memory_version_id', _attr(source, 'memory_version_id')),
        }
        for source in sources
    ]
    document_values = {
        'organization_id': _attr(memory, 'organization_id'),
        'project_id': _attr(memory, 'project_id'),
        'team_id': _attr(memory, 'team_id'),
        'memory_id': _attr(memory, 'id'),
        'memory_version_id': _attr(version, 'id'),
        'transition_id': transition_id,
        'content_hash': _attr(version, 'content_hash', ''),
        'title': title,
        'body': body,
        'visibility_scope': visibility,
        'status': _attr(memory, 'status', ''),
        'stale': bool(_attr(memory, 'stale', False)),
        'refuted': bool(_attr(memory, 'refuted', False)),
        'file_paths': file_paths,
        'symbols': symbols,
        'exact_terms': exact_terms,
        'source_observation_ids': source_observation_ids,
        'full_text': full_text,
        'sources': source_values,
    }
    exact_hash = hashlib.sha256(_json_bytes(document_values)).hexdigest()

    return ExactMemoryProjection(document_values=document_values, exact_projection_hash=exact_hash)


def _require_scope(memory: object, version: object) -> None:
    fields = ('organization_id', 'project_id', 'team_id')
    for field in fields:
        version_value = getattr(version, field, None)
        if field == 'team_id' and not hasattr(version, field):
            continue
        if getattr(memory, field, None) != version_value:
            raise ValueError(f'memory version scope mismatch: {field}')
    if getattr(version, 'memory_id', None) != getattr(memory, 'id', None):
        raise ValueError('memory version does not belong to memory scope')


def write_exact_memory_projection(
    *, memory: Memory, version: MemoryVersion, transition_id: uuid.UUID, sources: list[object] | tuple[object, ...]
) -> RetrievalDocument:
    if not transaction.get_connection().in_atomic_block:
        raise TransactionManagementError('exact projection writer requires an active transaction')
    _require_scope(memory, version)
    projection = build_exact_memory_projection(
        memory=memory,
        version=version,
        transition_id=transition_id,
        sources=sources,
    )
    values = projection.document_values
    documents = RetrievalDocument.objects.select_for_update().filter(memory_version_id=version.id)
    document = documents.first()
    if document is None:
        document = RetrievalDocument(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            team_id=memory.team_id,
            memory_id=memory.id,
            memory_version_id=version.id,
        )
    else:
        RetrievalDocument.objects.filter(memory_id=memory.id).exclude(id=document.id).update(stale=True)
    document.organization_id = memory.organization_id
    document.project_id = memory.project_id
    document.team_id = memory.team_id
    document.memory_id = memory.id
    document.memory_version_id = version.id
    document.visibility_scope = values['visibility_scope']
    document.source_observation_ids = values['source_observation_ids']
    document.file_paths = values['file_paths']
    document.symbols = values['symbols']
    document.exact_terms = values['exact_terms']
    document.full_text = values['full_text']
    document.stale = False
    document.refuted = bool(values['refuted'])
    document.metadata = {'projection': _canonical(values)}
    if hasattr(document, 'projection_contract_version'):
        document.projection_contract_version = 1
    if hasattr(document, 'exact_projection_hash'):
        document.exact_projection_hash = projection.exact_projection_hash
    for field, empty in (
        ('embedding_reference', ''),
        ('embedding_vector', []),
        ('embedding_pgvector', None),
        ('embedding_projection_hash', ''),
        ('embedding_projected_at', None),
    ):
        if hasattr(document, field):
            setattr(document, field, empty)
    document.save()
    RetrievalDocument.objects.filter(memory_id=memory.id).exclude(id=document.id).update(stale=True)

    return document


def create_embedding_work_and_signal(*, document: RetrievalDocument) -> tuple[object, bool]:
    from engram.memory.work_dispatch import queue_work_attempt
    from engram.memory.workflow_work import CreateWorkflowWorkInput, create_work

    snapshot = {
        'schema': 'memory_embedding/v1',
        'retrieval_document_id': str(document.id),
        'memory_id': str(document.memory_id),
        'memory_version_id': str(document.memory_version_id),
        'exact_projection_hash': document.exact_projection_hash,
    }
    data = CreateWorkflowWorkInput(
        organization_id=document.organization_id,
        project_id=document.project_id,
        work_type='memory_embedding',
        subject_type='retrieval_document',
        subject_id=document.id,
        input_snapshot=snapshot,
    )
    work, created = create_work(data)
    if created:
        queue_work_attempt(work_id=work.id, now=timezone.now(), origin='memory_transition')

    return work, created


def _load_current_transition(memory: Memory, transition_id: object) -> object | None:
    try:
        from engram.core.models import MemoryTransition

        return MemoryTransition.objects.filter(id=transition_id, memory_id=memory.id).first()
    except (ImportError, AttributeError):
        return None


def _transition_matches_projection(transition: object, document: RetrievalDocument, version_id: uuid.UUID) -> bool:
    document_ids = {
        getattr(transition, 'result_exact_document_id', None),
        getattr(transition, 'exact_document_id', None),
    }
    version_ids = {
        getattr(transition, 'result_version_id', None),
        getattr(transition, 'to_version_id', None),
    }
    document_ids.discard(None)
    version_ids.discard(None)
    if document_ids and document.id not in document_ids:
        return False
    if version_ids and version_id not in version_ids:
        return False
    return True


def _memory_version_matches_projection(memory: Memory, document: RetrievalDocument, version_id: uuid.UUID) -> bool:
    if getattr(memory, 'current_version_id', None) not in (None, version_id):
        return False
    current_version = getattr(memory, 'current_version', None)
    if current_version is None:
        return True
    try:
        return int(current_version) == int(document.memory_version.version)
    except (AttributeError, TypeError, ValueError):
        return True


def _projection_is_current(
    document: RetrievalDocument, memory: Memory, version_id: uuid.UUID, expected_hash: str
) -> bool:
    if getattr(document, 'exact_projection_hash', '') != expected_hash:
        return False
    if document.memory_id != memory.id or document.memory_version_id != version_id or document.stale:
        return False
    if getattr(memory, 'transition_contract_version', 0) != 1:
        return False
    transition_id = getattr(memory, 'current_transition_id', None)
    if transition_id is None:
        return False
    transition = _load_current_transition(memory, transition_id)
    if transition is not None and not _transition_matches_projection(transition, document, version_id):
        return False
    if not _memory_version_matches_projection(memory, document, version_id):
        return False
    return not bool(getattr(memory, 'stale', False) or getattr(memory, 'refuted', False))


def complete_embedding_projection(
    *,
    claim: object,
    expected_projection_hash: str,
    embedding: list[float] | tuple[float, ...],
    provider_call_id: uuid.UUID,
    now: datetime,
) -> RetrievalDocument | None:
    from engram.memory.work_execution import finish_work_claim, lock_work_fence

    with transaction.atomic():
        work, _run = lock_work_fence(claim=claim, now=now)
        try:
            document = (
                RetrievalDocument.objects.select_for_update()
                .select_related('memory', 'memory_version')
                .get(
                    id=work.subject_id,
                )
            )
        except RetrievalDocument.DoesNotExist:
            finish_work_claim(claim=claim, now=now, completion='product_no_signal')
            return None
        memory = Memory.objects.select_for_update().get(id=document.memory_id)
        snapshot_hash = work.input_snapshot.get('exact_projection_hash')
        expected_version = uuid.UUID(str(work.input_snapshot.get('memory_version_id')))
        if snapshot_hash != expected_projection_hash or not _projection_is_current(
            document, memory, expected_version, expected_projection_hash
        ):
            finish_work_claim(claim=claim, now=now, completion='product_no_signal')
            return None
        document.embedding_vector = list(embedding)
        if hasattr(document, 'embedding_pgvector'):
            document.embedding_pgvector = list(embedding)
        document.embedding_reference = f'provider:{provider_call_id}'
        if hasattr(document, 'embedding_projection_hash'):
            document.embedding_projection_hash = expected_projection_hash
        if hasattr(document, 'embedding_projected_at'):
            document.embedding_projected_at = now
        document.save()
        finish_work_claim(
            claim=claim,
            now=now,
            completion='product_succeeded',
            result_memory_id=memory.id,
        )

        return document
