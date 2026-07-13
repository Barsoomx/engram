from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from uuid import UUID

from engram.core.models import (
    Memory,
    RetrievalDocument,
    VisibilityScope,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)

_DIGEST_KIND = 'digest'
_SCHEMA = 'digest_visibility/v1'
_UNPROVEN = 'digest_visibility_unproven'
_REQUIRED_KEYS = (
    'workflow_work_id',
    'input_digest',
    'output_identity',
    'allowed_team_ids',
    'output_visibility_scope',
    'output_team_id',
)
_DIGEST_WORK_TYPES = (WorkflowWorkType.DAILY_DIGEST, WorkflowWorkType.WEEKLY_DIGEST)


def proven_digest_memory(memory: Memory) -> bool:
    return digest_visibility_failure(memory) is None


def digest_visibility_failure(memory: Memory) -> str | None:
    if memory.kind != _DIGEST_KIND:
        return None

    meta = _visibility_metadata(memory)
    if meta is None:
        return _UNPROVEN

    work = _load_work(meta)
    documents = list(
        RetrievalDocument.objects.filter(memory_id=memory.id).only('memory_id', 'visibility_scope', 'team_id'),
    )
    if _prove(memory, meta, work, documents):
        return None

    return _UNPROVEN


def proven_digest_memory_map(memories: Iterable[Memory]) -> dict[UUID, bool]:
    result: dict[UUID, bool] = {}
    digests: list[Memory] = []
    for memory in memories:
        if memory.kind != _DIGEST_KIND:
            result[memory.id] = True
        elif memory.id not in result and memory.id not in {digest.id for digest in digests}:
            digests.append(memory)

    if not digests:
        return result

    metadata_by_memory = {memory.id: _visibility_metadata(memory) for memory in digests}
    works = _load_works(metadata_by_memory.values())
    documents_by_memory = _load_documents([memory.id for memory in digests])

    for memory in digests:
        meta = metadata_by_memory[memory.id]
        result[memory.id] = _prove(memory, meta, _work_for(meta, works), documents_by_memory.get(memory.id, []))

    return result


def _load_works(metas: Iterable[dict[str, object] | None]) -> dict[UUID, WorkflowWork]:
    work_ids = {
        work_id for meta in metas if meta is not None and (work_id := _parse_uuid(meta.get('workflow_work_id')))
    }
    if not work_ids:
        return {}

    return {work.id: work for work in WorkflowWork.objects.filter(id__in=work_ids)}


def _load_documents(memory_ids: list[UUID]) -> dict[UUID, list[RetrievalDocument]]:
    documents_by_memory: dict[UUID, list[RetrievalDocument]] = defaultdict(list)
    for document in RetrievalDocument.objects.filter(memory_id__in=memory_ids).only(
        'memory_id',
        'visibility_scope',
        'team_id',
    ):
        documents_by_memory[document.memory_id].append(document)

    return documents_by_memory


def _work_for(meta: dict[str, object] | None, works: dict[UUID, WorkflowWork]) -> WorkflowWork | None:
    if meta is None:
        return None

    work_id = _parse_uuid(meta.get('workflow_work_id'))

    return works.get(work_id) if work_id is not None else None


def unproven_digest_memory_ids(memories: Iterable[Memory]) -> set[UUID]:
    return {memory_id for memory_id, proven in proven_digest_memory_map(memories).items() if not proven}


def _visibility_metadata(memory: Memory) -> dict[str, object] | None:
    metadata = memory.metadata
    if not isinstance(metadata, dict):
        return None

    visibility = metadata.get('digest_visibility')
    if not isinstance(visibility, dict):
        return None

    if visibility.get('schema') != _SCHEMA:
        return None

    for key in _REQUIRED_KEYS:
        if key not in visibility:
            return None

    return visibility


def _load_work(meta: dict[str, object]) -> WorkflowWork | None:
    work_id = _parse_uuid(meta.get('workflow_work_id'))
    if work_id is None:
        return None

    return WorkflowWork.objects.filter(id=work_id).first()


def _prove(
    memory: Memory,
    meta: dict[str, object] | None,
    work: WorkflowWork | None,
    documents: list[RetrievalDocument],
) -> bool:
    if meta is None or work is None:
        return False

    if not _work_is_completed_digest(work, memory):
        return False

    if not _snapshot_matches_metadata(work, meta):
        return False

    if not _memory_visibility_matches(memory, meta):
        return False

    return _documents_visibility_match(memory, documents)


def _work_is_completed_digest(work: WorkflowWork, memory: Memory) -> bool:
    if not isinstance(work.input_snapshot, dict):
        return False

    if work.work_type not in _DIGEST_WORK_TYPES:
        return False

    if (
        work.disposition != WorkflowWorkDisposition.COMPLETE
        or work.resolution_reason != WorkflowWorkResolutionReason.SUCCEEDED
    ):
        return False

    return work.organization_id == memory.organization_id and work.project_id == memory.project_id


def _snapshot_matches_metadata(work: WorkflowWork, meta: dict[str, object]) -> bool:
    snapshot = work.input_snapshot

    return (
        snapshot.get('input_digest') == meta.get('input_digest')
        and _output_identity(work) == meta.get('output_identity')
        and _team_id_list(snapshot.get('allowed_team_ids')) == _team_id_list(meta.get('allowed_team_ids'))
        and snapshot.get('output_visibility_scope') == meta.get('output_visibility_scope')
        and _normalize(snapshot.get('output_team_id')) == _normalize(meta.get('output_team_id'))
    )


def _output_identity(work: WorkflowWork) -> str | None:
    from engram.memory.digest_work import digest_output_identity

    try:
        return digest_output_identity(work)
    except (KeyError, TypeError):
        return None


def _memory_visibility_matches(memory: Memory, meta: dict[str, object]) -> bool:
    scope = meta.get('output_visibility_scope')
    if scope == 'project':
        return memory.visibility_scope == VisibilityScope.PROJECT and memory.team_id is None

    if scope == 'team':
        return memory.visibility_scope == VisibilityScope.TEAM and _normalize(memory.team_id) == _normalize(
            meta.get('output_team_id')
        )

    return False


def _documents_visibility_match(memory: Memory, documents: list[RetrievalDocument]) -> bool:
    for document in documents:
        if document.visibility_scope != memory.visibility_scope:
            return False

        if _normalize(document.team_id) != _normalize(memory.team_id):
            return False

    return True


def _team_id_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []

    return sorted(str(item) for item in value)


def _normalize(value: object) -> str | None:
    if value is None:
        return None

    return str(value)


def _parse_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value

    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
