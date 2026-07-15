from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from django.db import models, transaction

from engram.core.models import (
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryConflictResolution,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
    MemoryVersion,
    MemoryVersionSource,
    Project,
    RetrievalDocument,
    VectorField,
)
from engram.memory.aware_time import require_aware
from engram.memory.projections import (
    ExactMemoryProjection,
    build_exact_memory_projection,
    create_embedding_work_and_signal,
)
from engram.memory.transitions import canonical_memory_version_sources, memory_version_provenance_hash
from engram.memory.workflow_work import canonical_json_bytes

REPORT_ONLY = 'report_only'
REBUILD_EXACT = 'rebuild_exact'
ENQUEUE_EMBEDDING = 'enqueue_embedding'

ISSUE_CODES = (
    'candidate_transition_missing_or_mismatched',
    'current_transition_missing_or_mismatched',
    'current_version_pointer_mismatched',
    'version_provenance_missing_or_mismatched',
    'transition_audit_missing_or_mismatched',
    'lineage_link_missing_or_mismatched',
    'conflict_relation_missing_or_mismatched',
    'conflict_resolution_incomplete',
    'exact_projection_missing_or_mismatched',
    'embedding_projection_missing',
    'embedding_projection_stale',
    'legacy_transition_observability_missing',
)

_TERMINAL_CANDIDATE_TRANSITIONS = (
    MemoryTransitionType.PROMOTE,
    MemoryTransitionType.REVISE,
    MemoryTransitionType.MERGE,
    MemoryTransitionType.SUPERSEDE,
    MemoryTransitionType.CONFLICT_RESOLVE,
)


@dataclass(frozen=True, slots=True)
class ConsistencyReportInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    after_id: uuid.UUID | None = None
    sample_limit: int = 20


@dataclass(frozen=True, slots=True)
class ConsistencyIssue:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    memory_id: uuid.UUID
    code: str
    classification: str


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    scanned: int
    issues: tuple[ConsistencyIssue, ...]
    counts_by_code: tuple[tuple[str, int], ...]
    next_after_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RebuildProjectionInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    kind: str
    apply: bool = False
    after_id: uuid.UUID | None = None
    batch_size: int = 200


@dataclass(frozen=True, slots=True)
class RebuildProjectionResult:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    kind: str
    apply: bool
    scanned: int
    changed: int
    skipped: int
    next_after_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _MemoryEvaluation:
    memory: Memory
    version: MemoryVersion | None
    transition: MemoryTransition | None
    document: RetrievalDocument | None
    sources: tuple[MemoryVersionSource, ...]
    semantic_codes: tuple[str, ...]
    expected_projection: ExactMemoryProjection | None
    exact_matches: bool
    active: bool
    embedding_code: str | None


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError(f'{field} must be a UUID')

    return value


def _validate_scope_input(
    *,
    organization_id: object,
    project_id: object,
    as_of: datetime,
    after_id: object,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
    organization_uuid = _require_uuid(organization_id, field='organization_id')
    project_uuid = _require_uuid(project_id, field='project_id')
    cursor = None if after_id is None else _require_uuid(after_id, field='after_id')
    if not isinstance(as_of, datetime):
        raise ValueError('as_of must be a datetime')
    require_aware(as_of)
    if not Project.objects.filter(id=project_uuid, organization_id=organization_uuid).exists():
        raise ValueError(f'project {project_uuid} does not belong to organization {organization_uuid}')

    return organization_uuid, project_uuid, cursor


def _memory_page(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
    after_id: uuid.UUID | None,
    size: int,
) -> tuple[tuple[uuid.UUID, ...], uuid.UUID | None]:
    queryset = Memory.objects.filter(
        organization_id=organization_id,
        project_id=project_id,
        created_at__lte=as_of,
    )
    if after_id is not None:
        queryset = queryset.filter(id__gt=after_id)
    ordered = list(queryset.order_by('id').values_list('id', flat=True)[: size + 1])
    page = tuple(ordered[:size])
    next_after_id = page[-1] if len(ordered) > size else None

    return page, next_after_id


def _scope_matches(left: object, right: object) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in ('organization_id', 'project_id', 'team_id'))


def _version_pointer_matches(memory: Memory, version: MemoryVersion | None) -> bool:
    return bool(
        version is not None
        and version.organization_id == memory.organization_id
        and version.project_id == memory.project_id
        and version.memory_id == memory.id
        and version.version == memory.current_version
        and version.body == memory.body
    )


def _transition_side(
    memory: Memory,
    version: MemoryVersion,
    transition_row: MemoryTransition,
) -> str | None:
    result_side = transition_row.result_memory_id == memory.id and transition_row.result_version_id == version.id
    affected_side = transition_row.memory_id == memory.id and transition_row.to_version_id == version.id
    if result_side:
        return 'result'
    if affected_side:
        return 'affected'

    return None


def _document_owner_matches(memory: Memory, version: MemoryVersion, document: RetrievalDocument) -> bool:
    return all(
        (
            document.organization_id == memory.organization_id,
            document.project_id == memory.project_id,
            document.team_id == memory.team_id,
            document.memory_id == memory.id,
            document.memory_version_id == version.id,
        )
    )


def _current_transition_matches(
    memory: Memory,
    version: MemoryVersion | None,
    transition_row: MemoryTransition | None,
    document: RetrievalDocument | None,
) -> bool:
    if version is None or transition_row is None or document is None:
        return False
    if memory.transition_contract_version != 1 or memory.current_transition_id != transition_row.id:
        return False
    if not _scope_matches(memory, transition_row):
        return False
    side = _transition_side(memory, version, transition_row)
    if side is None or not _document_owner_matches(memory, version, document):
        return False
    expected_document_id = (
        transition_row.result_exact_document_id if side == 'result' else transition_row.exact_document_id
    )

    return expected_document_id == document.id


def _candidate_transition_matches(transition_row: MemoryTransition) -> bool:
    candidate = transition_row.candidate
    if candidate is None or not _scope_matches(candidate, transition_row):
        return False
    if transition_row.transition_type != MemoryTransitionType.CONFLICT_RESOLVE:
        return bool(
            candidate.status == CandidateStatus.PROMOTED
            and candidate.promoted_memory_id == transition_row.result_memory_id
        )
    if candidate.status == CandidateStatus.PROMOTED:
        return candidate.promoted_memory_id == transition_row.result_memory_id
    if candidate.status != CandidateStatus.REJECTED:
        return False

    metadata = transition_row.audit_event.metadata
    return bool(
        candidate.promoted_memory_id is None
        and isinstance(metadata, dict)
        and metadata.get('resolution') == MemoryConflictResolution.REJECT_CANDIDATE
    )


def _candidate_transitions_match(memory: Memory) -> bool:
    transitions = tuple(
        MemoryTransition.objects.filter(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            transition_type__in=_TERMINAL_CANDIDATE_TRANSITIONS,
            candidate__isnull=False,
        )
        .filter(models.Q(memory_id=memory.id) | models.Q(result_memory_id=memory.id))
        .select_related('candidate', 'audit_event')
        .order_by('id')
    )
    if not all(_candidate_transition_matches(transition_row) for transition_row in transitions):
        return False

    promoted_candidates = list(
        MemoryCandidate.objects.filter(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            promoted_memory_id=memory.id,
        ).order_by('id')
    )
    for candidate in promoted_candidates:
        if candidate.status != CandidateStatus.PROMOTED:
            return False
        matches = [
            transition_row
            for transition_row in transitions
            if transition_row.candidate_id == candidate.id and transition_row.result_memory_id == memory.id
        ]
        if len(matches) != 1:
            return False

    return True


def _source_version_hash(version: MemoryVersion) -> str:
    value = version.content_hash
    if len(value) == 64 and value.lower() == value and all(character in '0123456789abcdef' for character in value):
        return value

    return hashlib.sha256(
        canonical_json_bytes(
            {
                'memory_version_id': str(version.id),
                'content_hash': value,
                'body': version.body,
            }
        )
    ).hexdigest()


def _version_sources(version: MemoryVersion) -> tuple[MemoryVersionSource, ...]:
    rows = list(
        MemoryVersionSource.objects.filter(memory_version_id=version.id)
        .select_related('candidate_source', 'source_memory_version__memory')
        .order_by('id')
    )

    return tuple(canonical_memory_version_sources(rows))


def _source_rows_match(version: MemoryVersion, sources: tuple[MemoryVersionSource, ...]) -> bool:
    if not sources:
        return False
    for source in sources:
        if (
            source.organization_id != version.organization_id
            or source.project_id != version.project_id
            or source.team_id != version.memory.team_id
            or source.memory_version_id != version.id
        ):
            return False
        if source.candidate_source_id is not None:
            candidate_source = source.candidate_source
            if (
                candidate_source.organization_id != version.organization_id
                or candidate_source.project_id != version.project_id
                or candidate_source.team_id != version.memory.team_id
                or source.source_memory_version_id is not None
                or source.source_content_hash != candidate_source.anchors_hash
            ):
                return False
        elif source.source_memory_version_id is not None:
            source_version = source.source_memory_version
            if (
                source_version.organization_id != version.organization_id
                or source_version.project_id != version.project_id
                or source.candidate_source_id is not None
                or source.source_content_hash != _source_version_hash(source_version)
            ):
                return False
        else:
            return False

    return True


def _provenance_matches(
    version: MemoryVersion | None,
    transition_row: MemoryTransition | None,
) -> tuple[tuple[MemoryVersionSource, ...], bool]:
    if version is None:
        return (), False
    sources = _version_sources(version)
    if not _source_rows_match(version, sources) or transition_row is None:
        return sources, False
    result_version = transition_row.result_version
    result_sources = sources if result_version.id == version.id else _version_sources(result_version)
    if not _source_rows_match(result_version, result_sources):
        return sources, False

    return sources, transition_row.provenance_hash == memory_version_provenance_hash(list(result_sources))


def _audit_matches(transition_row: MemoryTransition) -> bool:
    audit = transition_row.audit_event
    metadata = audit.metadata
    if not isinstance(metadata, dict):
        return False
    scope = metadata.get('scope_filters')
    expected_scope = {
        'organization_id': str(transition_row.organization_id),
        'project_id': str(transition_row.project_id),
        'team_id': str(transition_row.team_id) if transition_row.team_id else None,
    }
    if scope != expected_scope:
        return False
    expected_metadata = {
        'schema': 'memory_transition/v1',
        'transition_type': transition_row.transition_type,
        'transition_id': str(transition_row.id),
        'memory_id': str(transition_row.memory_id),
        'exact_document_id': str(transition_row.exact_document_id),
        'request_fingerprint': transition_row.request_fingerprint,
        'provenance_hash': transition_row.provenance_hash,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        return False
    exact_hash = metadata.get('exact_projection_hash')
    if (
        not isinstance(exact_hash, str)
        or len(exact_hash) != 64
        or any(character not in '0123456789abcdef' for character in exact_hash)
    ):
        return False
    if 'result_exact_document_id' in metadata and (
        metadata['result_exact_document_id'] != str(transition_row.result_exact_document_id)
    ):
        return False

    return all(
        (
            audit.organization_id == transition_row.organization_id,
            audit.project_id == transition_row.project_id,
            audit.team_id == transition_row.team_id,
            audit.event_type == 'MemoryTransitionCommitted',
            audit.target_type == 'memory',
            audit.target_id == str(transition_row.memory_id),
            audit.result == AuditResult.RECORDED,
        )
    )


def _current_audit_projection_matches(
    memory: Memory,
    version: MemoryVersion,
    transition_row: MemoryTransition,
    expected_hash: str,
) -> bool:
    metadata = transition_row.audit_event.metadata
    if not isinstance(metadata, dict):
        return False
    side = _transition_side(memory, version, transition_row)
    if side == 'result':
        recorded = metadata.get('result_exact_projection_hash', metadata.get('exact_projection_hash'))
    elif side == 'affected':
        recorded = metadata.get('exact_projection_hash')
    else:
        return False

    return recorded == expected_hash


def _transition_audits_match(memory: Memory) -> bool:
    transitions = (
        MemoryTransition.objects.filter(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
        )
        .filter(models.Q(memory_id=memory.id) | models.Q(result_memory_id=memory.id))
        .select_related('audit_event')
        .order_by('id')
    )

    return all(_audit_matches(transition_row) for transition_row in transitions)


def _link_matches(transition_row: MemoryTransition, expected_type: str) -> bool:
    link = transition_row.semantic_link
    return bool(
        link is not None
        and link.organization_id == transition_row.organization_id
        and link.project_id == transition_row.project_id
        and link.memory_id == transition_row.memory_id
        and link.link_type == expected_type
        and link.target == str(transition_row.result_memory_id)
    )


def _lineage_link_matches(transition_row: MemoryTransition) -> bool:
    transition_type = transition_row.transition_type
    if transition_type == MemoryTransitionType.MERGE:
        if transition_row.memory_id != transition_row.result_memory_id:
            return _link_matches(transition_row, LinkType.NARROWED_BY)

        return transition_row.semantic_link_id is None
    if transition_type == MemoryTransitionType.SUPERSEDE:
        return _link_matches(transition_row, LinkType.SUPERSEDED_BY)
    if transition_type != MemoryTransitionType.CONFLICT_RESOLVE:
        return True

    metadata = transition_row.audit_event.metadata
    resolution = metadata.get('resolution') if isinstance(metadata, dict) else None
    if resolution == MemoryConflictResolution.SUPERSEDE_MEMORY:
        return _link_matches(transition_row, LinkType.SUPERSEDED_BY)

    return transition_row.semantic_link_id is None


def _lineage_links_match(memory: Memory) -> bool:
    transitions = (
        MemoryTransition.objects.filter(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
        )
        .filter(models.Q(memory_id=memory.id) | models.Q(result_memory_id=memory.id))
        .select_related('semantic_link', 'audit_event')
        .order_by('id')
    )

    return all(_lineage_link_matches(transition_row) for transition_row in transitions)


def _conflict_relations_match(memory: Memory) -> tuple[bool, bool]:
    relation_matches = True
    resolution_matches = True
    conflicts = (
        MemoryConflict.objects.filter(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            memory_id=memory.id,
        )
        .select_related(
            'candidate',
            'memory_version',
            'semantic_link',
            'opened_transition__audit_event',
            'resolved_transition__audit_event',
        )
        .order_by('id')
    )
    for conflict in conflicts:
        link = conflict.semantic_link
        opened = conflict.opened_transition
        opened_metadata = opened.audit_event.metadata
        relation_matches = relation_matches and all(
            (
                conflict.team_id == memory.team_id,
                conflict.memory_version.memory_id == memory.id,
                conflict.memory_version.organization_id == memory.organization_id,
                conflict.memory_version.project_id == memory.project_id,
                conflict.candidate.organization_id == memory.organization_id,
                conflict.candidate.project_id == memory.project_id,
                conflict.candidate.team_id == memory.team_id,
                link.organization_id == memory.organization_id,
                link.project_id == memory.project_id,
                link.memory_id == memory.id,
                link.link_type == LinkType.CONFLICTS_WITH,
                link.target == f'candidate:{conflict.candidate_id}',
                opened.transition_type == MemoryTransitionType.CONFLICT_OPEN,
                opened.organization_id == memory.organization_id,
                opened.project_id == memory.project_id,
                opened.team_id == memory.team_id,
                opened.candidate_id == conflict.candidate_id,
                opened.memory_id == memory.id,
                opened.to_version_id == conflict.memory_version_id,
                opened.result_memory_id == memory.id,
                opened.result_version_id == conflict.memory_version_id,
                opened.semantic_link_id == link.id,
                isinstance(opened_metadata, dict),
                isinstance(opened_metadata, dict)
                and opened_metadata.get('conflict_evidence_hash') == conflict.evidence_hash,
            )
        )
        if conflict.resolved_transition_id is None:
            resolution_matches = resolution_matches and all(
                (
                    conflict.resolution == '',
                    conflict.resolved_at is None,
                )
            )

            continue
        resolved = conflict.resolved_transition
        resolved_metadata = resolved.audit_event.metadata
        conflict_ids = (
            {value for value in str(resolved_metadata.get('conflict_ids', '')).split(',') if value}
            if isinstance(resolved_metadata, dict)
            else set()
        )
        resolution_matches = resolution_matches and all(
            (
                conflict.resolution in MemoryConflictResolution.values,
                conflict.resolved_at is not None,
                resolved.transition_type == MemoryTransitionType.CONFLICT_RESOLVE,
                resolved.organization_id == memory.organization_id,
                resolved.project_id == memory.project_id,
                resolved.team_id == memory.team_id,
                resolved.candidate_id == conflict.candidate_id,
                isinstance(resolved_metadata, dict),
                isinstance(resolved_metadata, dict) and resolved_metadata.get('resolution') == conflict.resolution,
                str(conflict.id) in conflict_ids,
            )
        )

    return relation_matches, resolution_matches


def _exact_projection_matches(
    document: RetrievalDocument,
    projection: ExactMemoryProjection,
) -> bool:
    values = projection.document_values
    return all(
        (
            document.visibility_scope == values['visibility_scope'],
            document.source_observation_ids == values['source_observation_ids'],
            document.file_paths == values['file_paths'],
            document.symbols == values['symbols'],
            document.exact_terms == values['exact_terms'],
            document.full_text == values['full_text'],
            document.stale == values['stale'],
            document.refuted == values['refuted'],
            document.metadata == {'projection': values},
            document.projection_contract_version == 1,
            document.exact_projection_hash == projection.exact_projection_hash,
        )
    )


def _vector_values(value: object) -> list[float] | None:
    if value is None:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _embedding_issue(document: RetrievalDocument) -> str | None:
    json_vector = _vector_values(document.embedding_vector) or []
    pgvector = _vector_values(getattr(document, 'embedding_pgvector', None))
    pgvector_empty = pgvector is None if VectorField is not None else True
    empty = all(
        (
            document.embedding_reference == '',
            json_vector == [],
            pgvector_empty,
            document.embedding_projection_hash == '',
            document.embedding_projected_at is None,
        )
    )
    if empty:
        return 'embedding_projection_missing'
    ready = all(
        (
            document.embedding_reference != '',
            json_vector != [],
            document.embedding_projection_hash == document.exact_projection_hash,
            document.embedding_projected_at is not None,
            VectorField is None or (pgvector is not None and pgvector == json_vector),
        )
    )

    return None if ready else 'embedding_projection_stale'


def _evaluate_memory(memory: Memory, *, lock_related: bool) -> _MemoryEvaluation:
    if memory.transition_contract_version != 1:
        return _MemoryEvaluation(
            memory=memory,
            version=None,
            transition=None,
            document=None,
            sources=(),
            semantic_codes=('legacy_transition_observability_missing',),
            expected_projection=None,
            exact_matches=False,
            active=False,
            embedding_code=None,
        )

    version_query = MemoryVersion.objects.filter(memory_id=memory.id, version=memory.current_version)
    if lock_related:
        version_query = version_query.select_for_update(of=('self',))
    version = version_query.first()
    transition_row = (
        MemoryTransition.objects.select_related('result_version', 'audit_event')
        .filter(id=memory.current_transition_id)
        .first()
        if memory.current_transition_id is not None
        else None
    )
    document = None
    if version is not None:
        document_query = RetrievalDocument.objects.filter(memory_version_id=version.id)
        if lock_related:
            document_query = document_query.select_for_update(of=('self',))
        document = document_query.first()

    version_ok = _version_pointer_matches(memory, version)
    transition_ok = _current_transition_matches(memory, version, transition_row, document)
    candidate_ok = _candidate_transitions_match(memory)
    sources, provenance_ok = _provenance_matches(version, transition_row)
    audit_ok = transition_row is not None and _transition_audits_match(memory)
    lineage_ok = _lineage_links_match(memory)
    conflict_ok, resolution_ok = _conflict_relations_match(memory)

    semantic_codes: list[str] = []
    checks = (
        ('candidate_transition_missing_or_mismatched', candidate_ok),
        ('current_transition_missing_or_mismatched', transition_ok),
        ('current_version_pointer_mismatched', version_ok),
        ('version_provenance_missing_or_mismatched', provenance_ok),
        ('transition_audit_missing_or_mismatched', audit_ok),
        ('lineage_link_missing_or_mismatched', lineage_ok),
        ('conflict_relation_missing_or_mismatched', conflict_ok),
        ('conflict_resolution_incomplete', resolution_ok),
    )
    semantic_codes.extend(code for code, healthy in checks if not healthy)
    expected_projection = None
    exact_matches = False
    if not semantic_codes and version is not None and transition_row is not None and document is not None:
        try:
            expected_projection = build_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_row.id,
                sources=sources,
            )
        except (TypeError, ValueError):
            expected_projection = None
        if expected_projection is not None:
            if not _current_audit_projection_matches(
                memory,
                version,
                transition_row,
                expected_projection.exact_projection_hash,
            ):
                semantic_codes.append('transition_audit_missing_or_mismatched')
                expected_projection = None
            else:
                exact_matches = _exact_projection_matches(document, expected_projection)

    active = bool(
        not semantic_codes
        and exact_matches
        and document is not None
        and memory.status == MemoryStatus.APPROVED
        and not memory.stale
        and not memory.refuted
        and not document.stale
        and not document.refuted
    )
    embedding_code = _embedding_issue(document) if active and document is not None else None

    return _MemoryEvaluation(
        memory=memory,
        version=version,
        transition=transition_row,
        document=document,
        sources=sources,
        semantic_codes=tuple(semantic_codes),
        expected_projection=expected_projection,
        exact_matches=exact_matches,
        active=active,
        embedding_code=embedding_code,
    )


def _issues(evaluation: _MemoryEvaluation) -> tuple[ConsistencyIssue, ...]:
    issues = [
        ConsistencyIssue(
            organization_id=evaluation.memory.organization_id,
            project_id=evaluation.memory.project_id,
            memory_id=evaluation.memory.id,
            code=code,
            classification=REPORT_ONLY,
        )
        for code in evaluation.semantic_codes
    ]
    if not evaluation.semantic_codes and not evaluation.exact_matches:
        issues.append(
            ConsistencyIssue(
                organization_id=evaluation.memory.organization_id,
                project_id=evaluation.memory.project_id,
                memory_id=evaluation.memory.id,
                code='exact_projection_missing_or_mismatched',
                classification=REBUILD_EXACT if evaluation.expected_projection is not None else REPORT_ONLY,
            )
        )
    elif evaluation.embedding_code is not None:
        issues.append(
            ConsistencyIssue(
                organization_id=evaluation.memory.organization_id,
                project_id=evaluation.memory.project_id,
                memory_id=evaluation.memory.id,
                code=evaluation.embedding_code,
                classification=ENQUEUE_EMBEDDING,
            )
        )

    return tuple(issues)


def _apply_exact_projection(document: RetrievalDocument, projection: ExactMemoryProjection) -> None:
    values = projection.document_values
    hash_changed = document.exact_projection_hash != projection.exact_projection_hash
    document.visibility_scope = str(values['visibility_scope'])
    document.source_observation_ids = values['source_observation_ids']
    document.file_paths = values['file_paths']
    document.symbols = values['symbols']
    document.exact_terms = values['exact_terms']
    document.full_text = str(values['full_text'])
    document.stale = bool(values['stale'])
    document.refuted = bool(values['refuted'])
    document.metadata = {'projection': values}
    document.projection_contract_version = 1
    document.exact_projection_hash = projection.exact_projection_hash
    update_fields = [
        'visibility_scope',
        'source_observation_ids',
        'file_paths',
        'symbols',
        'exact_terms',
        'full_text',
        'stale',
        'refuted',
        'metadata',
        'projection_contract_version',
        'exact_projection_hash',
    ]
    if hash_changed:
        document.embedding_reference = ''
        document.embedding_vector = []
        document.embedding_projection_hash = ''
        document.embedding_projected_at = None
        update_fields.extend(
            (
                'embedding_reference',
                'embedding_vector',
                'embedding_projection_hash',
                'embedding_projected_at',
            )
        )
        if VectorField is not None:
            document.embedding_pgvector = None
            update_fields.append('embedding_pgvector')
    document.save(update_fields=[*update_fields, 'updated_at'])

    return


class MemoryConsistencyReporter:
    def execute(self, data: ConsistencyReportInput) -> ConsistencyReport:
        organization_id, project_id, after_id = _validate_scope_input(
            organization_id=data.organization_id,
            project_id=data.project_id,
            as_of=data.as_of,
            after_id=data.after_id,
        )
        if data.sample_limit < 1 or data.sample_limit > 20:
            raise ValueError('sample_limit must be between 1 and 20')
        memory_ids, next_after_id = _memory_page(
            organization_id=organization_id,
            project_id=project_id,
            as_of=data.as_of,
            after_id=after_id,
            size=data.sample_limit,
        )
        issues: list[ConsistencyIssue] = []
        for memory in Memory.objects.filter(id__in=memory_ids).order_by('id'):
            issues.extend(_issues(_evaluate_memory(memory, lock_related=False)))
        counts = Counter(issue.code for issue in issues)

        return ConsistencyReport(
            organization_id=organization_id,
            project_id=project_id,
            as_of=data.as_of,
            scanned=len(memory_ids),
            issues=tuple(issues),
            counts_by_code=tuple((code, counts[code]) for code in ISSUE_CODES if counts[code]),
            next_after_id=next_after_id,
        )


class RebuildMemoryProjections:
    def execute(self, data: RebuildProjectionInput) -> RebuildProjectionResult:
        organization_id, project_id, after_id = _validate_scope_input(
            organization_id=data.organization_id,
            project_id=data.project_id,
            as_of=data.as_of,
            after_id=data.after_id,
        )
        if data.kind not in {'exact', 'embedding'}:
            raise ValueError("kind must be 'exact' or 'embedding'")
        if data.batch_size < 1 or data.batch_size > 200:
            raise ValueError('batch_size must be between 1 and 200')
        memory_ids, next_after_id = _memory_page(
            organization_id=organization_id,
            project_id=project_id,
            as_of=data.as_of,
            after_id=after_id,
            size=data.batch_size,
        )
        changed = 0
        skipped = 0
        for memory_id in memory_ids:
            with transaction.atomic():
                memory = (
                    Memory.objects.select_for_update(of=('self',))
                    .filter(
                        id=memory_id,
                        organization_id=organization_id,
                        project_id=project_id,
                        created_at__lte=data.as_of,
                    )
                    .first()
                )
                if memory is None:
                    continue
                evaluation = _evaluate_memory(memory, lock_related=True)
                if data.kind == 'exact':
                    if (
                        evaluation.semantic_codes
                        or evaluation.document is None
                        or evaluation.expected_projection is None
                        or evaluation.exact_matches
                    ):
                        skipped += 1

                        continue
                    if not data.apply:
                        skipped += 1

                        continue
                    _apply_exact_projection(evaluation.document, evaluation.expected_projection)
                    changed += 1

                    continue
                if evaluation.embedding_code is None or evaluation.document is None:
                    continue
                if not data.apply:
                    skipped += 1

                    continue
                create_embedding_work_and_signal(document=evaluation.document)
                changed += 1

        return RebuildProjectionResult(
            organization_id=organization_id,
            project_id=project_id,
            as_of=data.as_of,
            kind=data.kind,
            apply=data.apply,
            scanned=len(memory_ids),
            changed=changed,
            skipped=skipped,
            next_after_id=next_after_id,
        )
