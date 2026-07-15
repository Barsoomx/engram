from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import models, transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from engram.context.term_extraction import derive_retrieval_terms
from engram.core.models import (
    Memory,
    MemoryTransition,
    MemoryVersion,
    MemoryVersionSource,
    RetrievalDocument,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import WorkClaim, finish_work_claim, lock_work_fence
from engram.memory.workflow_work import CreateWorkflowWorkInput, canonical_json_bytes, create_work


@dataclass(frozen=True, slots=True)
class ExactMemoryProjection:
    document_values: dict[str, object]
    exact_projection_hash: str


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
    return canonical_json_bytes(_canonical(value))


def build_exact_memory_projection(
    *,
    memory: Memory,
    version: MemoryVersion,
    transition_id: uuid.UUID,
    sources: list[MemoryVersionSource] | tuple[MemoryVersionSource, ...],
) -> ExactMemoryProjection:
    memory_metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
    version_metadata = version.source_metadata if isinstance(version.source_metadata, dict) else {}

    def metadata_value(name: str, default: object) -> object:
        if name in version_metadata:
            return version_metadata[name]
        return memory_metadata.get(name, default)

    title = memory.title
    body = version.body
    file_paths = metadata_value('file_paths', []) or []
    symbols, exact_terms = derive_retrieval_terms(
        {
            'symbols': metadata_value('symbols', []) or [],
            'exact_terms': metadata_value('exact_terms', []) or [],
        },
        title,
        body,
    )
    source_observation_ids = metadata_value('source_observation_ids', []) or []
    full_text = metadata_value('full_text', '') or f'{title}\n\n{body}'.strip()
    source_values = [
        {
            'id': source.id,
            'source_kind': 'candidate_source' if source.candidate_source_id is not None else 'memory_version',
            'source_content_hash': source.source_content_hash,
            'candidate_source_id': source.candidate_source_id,
            'source_memory_version_id': source.source_memory_version_id,
        }
        for source in sources
    ]
    document_values = {
        'organization_id': memory.organization_id,
        'project_id': memory.project_id,
        'team_id': memory.team_id,
        'memory_id': memory.id,
        'memory_version_id': version.id,
        'transition_id': transition_id,
        'content_hash': version.content_hash,
        'title': title,
        'body': body,
        'visibility_scope': memory.visibility_scope,
        'status': memory.status,
        'stale': memory.stale,
        'refuted': memory.refuted,
        'file_paths': file_paths,
        'symbols': symbols,
        'exact_terms': exact_terms,
        'source_observation_ids': source_observation_ids,
        'full_text': full_text,
        'sources': source_values,
    }
    canonical_values = _canonical(document_values)
    if not isinstance(canonical_values, dict):
        raise TypeError('exact projection values must be a mapping')
    exact_hash = hashlib.sha256(_json_bytes(canonical_values)).hexdigest()

    return ExactMemoryProjection(document_values=canonical_values, exact_projection_hash=exact_hash)


def _require_scope(memory: Memory, version: MemoryVersion) -> None:
    for field in ('organization_id', 'project_id'):
        if getattr(memory, field) != getattr(version, field):
            raise ValueError(f'memory version scope mismatch: {field}')
    if version.memory_id != memory.id:
        raise ValueError('memory version does not belong to memory scope')


def write_exact_memory_projection(
    *,
    memory: Memory,
    version: MemoryVersion,
    transition_id: uuid.UUID,
    sources: list[MemoryVersionSource] | tuple[MemoryVersionSource, ...],
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
    previous_hash = document.exact_projection_hash if document is not None else ''
    if document is None:
        document = RetrievalDocument(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            team_id=memory.team_id,
            memory_id=memory.id,
            memory_version_id=version.id,
        )
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
    document.stale = bool(values['stale'])
    document.refuted = bool(values['refuted'])
    document.metadata = {'projection': values}
    document.projection_contract_version = 1
    document.exact_projection_hash = projection.exact_projection_hash
    if previous_hash != projection.exact_projection_hash:
        document.embedding_reference = ''
        document.embedding_vector = []
        document.embedding_pgvector = None
        document.embedding_projection_hash = ''
        document.embedding_projected_at = None
    document.save()
    RetrievalDocument.objects.filter(memory_id=memory.id).exclude(id=document.id).update(stale=True)

    return document


def create_embedding_work_and_signal(*, document: RetrievalDocument) -> tuple[WorkflowWork, bool]:
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


def _load_current_transition(memory: Memory, transition_id: uuid.UUID) -> MemoryTransition | None:
    return (
        MemoryTransition.objects.filter(id=transition_id)
        .filter(models.Q(memory_id=memory.id) | models.Q(result_memory_id=memory.id))
        .first()
    )


def _transition_matches_projection(
    transition: MemoryTransition,
    document: RetrievalDocument,
    version: MemoryVersion,
    work: WorkflowWork,
    expected_hash: str,
) -> bool:
    affected_projection = all(
        (
            transition.memory_id == document.memory_id,
            transition.to_version_id == version.id,
            transition.exact_document_id == document.id,
        )
    )
    result_projection = all(
        (
            transition.result_memory_id == document.memory_id,
            transition.result_version_id == version.id,
            transition.result_exact_document_id == document.id,
        )
    )
    snapshot = work.input_snapshot if isinstance(work.input_snapshot, dict) else {}
    current_hash_work = all(
        (
            work.organization_id == document.organization_id,
            work.project_id == document.project_id,
            work.team_id == document.team_id,
            work.work_type == WorkflowWorkType.MEMORY_EMBEDDING,
            work.subject_type == WorkflowSubjectType.RETRIEVAL_DOCUMENT,
            work.subject_id == document.id,
            snapshot.get('retrieval_document_id') == str(document.id),
            snapshot.get('memory_id') == str(document.memory_id),
            snapshot.get('memory_version_id') == str(version.id),
            snapshot.get('exact_projection_hash') == expected_hash,
        )
    )
    audit_metadata = transition.audit_event.metadata if isinstance(transition.audit_event.metadata, dict) else {}
    affected_work_id = audit_metadata.get('affected_embedding_work_id')
    result_work_matches = transition.embedding_work_id == work.id or work.created_at >= transition.created_at
    affected_work_matches = affected_work_id == str(work.id) or work.created_at >= transition.created_at
    work_matches = current_hash_work and (
        (result_projection and result_work_matches)
        or (affected_projection and transition.memory_id != transition.result_memory_id and affected_work_matches)
    )
    return all(
        (
            transition.organization_id == document.organization_id,
            transition.project_id == document.project_id,
            transition.team_id == document.team_id,
            affected_projection or result_projection,
            work_matches,
        )
    )


def _memory_version_matches_projection(memory: Memory, version: MemoryVersion) -> bool:
    return version.memory_id == memory.id and version.version == memory.current_version and version.body == memory.body


def _projection_is_current(
    document: RetrievalDocument,
    memory: Memory,
    version: MemoryVersion,
    expected_hash: str,
    work: WorkflowWork,
) -> bool:
    if document.exact_projection_hash != expected_hash:
        return False
    if document.memory_id != memory.id or document.memory_version_id != version.id or document.stale:
        return False
    if memory.transition_contract_version != 1:
        return False
    transition_id = memory.current_transition_id
    if transition_id is None:
        return False
    transition = _load_current_transition(memory, transition_id)
    if transition is None or not _transition_matches_projection(
        transition,
        document,
        version,
        work,
        expected_hash,
    ):
        return False
    if not _memory_version_matches_projection(memory, version):
        return False
    return memory.status == 'approved' and not (memory.stale or memory.refuted)


def _snapshot_uuid(work: WorkflowWork, key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(work.input_snapshot[key]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _finish_projection_superseded(*, claim: WorkClaim, now: datetime) -> None:
    finish_work_claim(
        claim=claim,
        now=now,
        completion='product_no_signal',
        resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
    )


def complete_embedding_projection(
    *,
    claim: WorkClaim,
    expected_projection_hash: str,
    embedding: list[float] | tuple[float, ...],
    provider_call_id: uuid.UUID,
    now: datetime,
) -> RetrievalDocument | None:
    with transaction.atomic():
        work, _run = lock_work_fence(claim=claim, now=now)
        snapshot_document_id = _snapshot_uuid(work, 'retrieval_document_id')
        snapshot_memory_id = _snapshot_uuid(work, 'memory_id')
        snapshot_version_id = _snapshot_uuid(work, 'memory_version_id')
        if (
            work.work_type != WorkflowWorkType.MEMORY_EMBEDDING
            or work.subject_type != WorkflowSubjectType.RETRIEVAL_DOCUMENT
            or snapshot_document_id != work.subject_id
            or snapshot_memory_id is None
            or snapshot_version_id is None
        ):
            _finish_projection_superseded(claim=claim, now=now)
            return None
        try:
            memory = Memory.objects.select_for_update().get(id=snapshot_memory_id)
            version = MemoryVersion.objects.select_for_update().get(
                id=snapshot_version_id,
                memory_id=memory.id,
            )
            document = RetrievalDocument.objects.select_for_update(of=('self',)).get(id=work.subject_id)
        except (Memory.DoesNotExist, MemoryVersion.DoesNotExist, RetrievalDocument.DoesNotExist):
            _finish_projection_superseded(claim=claim, now=now)
            return None
        snapshot_hash = work.input_snapshot.get('exact_projection_hash')
        if snapshot_hash != expected_projection_hash or not _projection_is_current(
            document,
            memory,
            version,
            expected_projection_hash,
            work,
        ):
            _finish_projection_superseded(claim=claim, now=now)
            return None
        document.embedding_vector = list(embedding)
        document.embedding_pgvector = list(embedding)
        document.embedding_reference = f'provider:{provider_call_id}'
        document.embedding_projection_hash = expected_projection_hash
        document.embedding_projected_at = now
        document.save()
        finish_work_claim(
            claim=claim,
            now=now,
            completion='product_succeeded',
            result_memory_id=memory.id,
        )

        return document
