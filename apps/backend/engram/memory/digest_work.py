from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet

from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    LinkType,
    Memory,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.services import (
    MemoryWorkerError,
    render_frozen_daily_digest_provider_result,
    render_weekly_digest_body,
)
from engram.memory.tasks import dispatch_work_task
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    WorkflowWorkScopeError,
    canonical_json_bytes,
    create_work,
    resolve_work_no_input,
    resolve_work_succeeded,
    work_input_fingerprint,
)

_DIGEST_VISIBILITY_POLICY = 'digest_visibility/v1'
_DAILY_SCHEMA = 'daily_digest_input/v1'
_WEEKLY_SCHEMA = 'weekly_digest_input/v1'


@dataclass(frozen=True, slots=True)
class DigestSourceRef:
    render_position: int
    memory_id: UUID
    memory_version_id: UUID
    version: int
    server_body_digest: str
    visibility_scope: str
    team_id: UUID | None
    source_title: str


def _require_project(organization_id: UUID, project_id: UUID) -> Project:
    try:
        return Project.objects.get(id=project_id, organization_id=organization_id)
    except Project.DoesNotExist as error:
        raise WorkflowWorkScopeError('project is outside the declared organization scope') from error


def _to_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f'{label} must be a datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{label} must be timezone-aware')

    return value.astimezone(UTC)


def _canonical_timestamp(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    timespec = 'microseconds' if normalized.microsecond else 'seconds'

    return normalized.isoformat(timespec=timespec).replace('+00:00', 'Z')


def _server_body_digest(version: MemoryVersion) -> str:
    payload = [str(version.id), version.version, version.body]

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _input_digest(snapshot: dict[str, object]) -> str:
    payload = {key: value for key, value in snapshot.items() if key != 'input_digest'}

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _ref_to_dict(ref: DigestSourceRef) -> dict[str, object]:
    return {
        'render_position': ref.render_position,
        'memory_id': str(ref.memory_id),
        'memory_version_id': str(ref.memory_version_id),
        'version': ref.version,
        'server_body_digest': ref.server_body_digest,
        'visibility_scope': ref.visibility_scope,
        'team_id': str(ref.team_id) if ref.team_id is not None else None,
        'source_title': ref.source_title,
    }


def _pin_versions(memories: list[Memory]) -> dict[UUID, MemoryVersion]:
    if not memories:
        return {}

    wanted = {memory.id: memory.current_version for memory in memories}
    rows = MemoryVersion.objects.filter(memory_id__in=list(wanted)).only('id', 'memory_id', 'version', 'body')
    pinned: dict[UUID, MemoryVersion] = {}
    for row in rows:
        if wanted.get(row.memory_id) == row.version and row.memory_id not in pinned:
            pinned[row.memory_id] = row
    missing = [memory.id for memory in memories if memory.id not in pinned]
    if missing:
        raise ValueError('digest source is missing its current version row')

    return pinned


def _admitted(queryset: QuerySet[Memory], team_id: UUID | None) -> QuerySet[Memory]:
    admission = Q(visibility_scope=VisibilityScope.PROJECT)
    if team_id is not None:
        admission = admission | Q(visibility_scope=VisibilityScope.TEAM, team_id=team_id)

    return queryset.filter(admission).exclude(kind='digest')


def freeze_daily_digest_input(
    *,
    organization_id: UUID,
    project_id: UUID,
    window_start: datetime,
    window_end: datetime,
    schedule_key: str,
    max_sources: int,
) -> dict[str, object]:
    _require_project(organization_id, project_id)
    start = _to_utc(window_start, 'window_start')
    end = _to_utc(window_end, 'window_end')

    memories = list(
        _admitted(
            Memory.objects.filter(
                organization_id=organization_id,
                project_id=project_id,
                status=MemoryStatus.APPROVED,
                updated_at__gte=start,
                updated_at__lt=end,
            ),
            None,
        ).order_by('-updated_at', 'id')
    )
    eligible_count = len(memories)
    cap = max(0, max_sources)
    selected = memories[:cap]
    versions = _pin_versions(selected)

    ordered = sorted(selected, key=lambda memory: (memory.title, str(memory.id)))
    sources: list[dict[str, object]] = []
    for position, memory in enumerate(ordered):
        version = versions[memory.id]
        ref = DigestSourceRef(
            render_position=position,
            memory_id=memory.id,
            memory_version_id=version.id,
            version=version.version,
            server_body_digest=_server_body_digest(version),
            visibility_scope=memory.visibility_scope,
            team_id=memory.team_id,
            source_title=memory.title,
        )
        sources.append(_ref_to_dict(ref))

    snapshot: dict[str, object] = {
        'schema': _DAILY_SCHEMA,
        'project_id': str(project_id),
        'schedule_key': schedule_key,
        'window_start': _canonical_timestamp(start),
        'window_end': _canonical_timestamp(end),
        'visibility_policy': _DIGEST_VISIBILITY_POLICY,
        'allowed_team_ids': [],
        'output_visibility_scope': 'project',
        'output_team_id': None,
        'eligible_source_count': eligible_count,
        'max_sources': max_sources,
        'sources_truncated': eligible_count > len(sources),
        'sources': sources,
    }
    snapshot['input_digest'] = _input_digest(snapshot)

    return snapshot


def _scoped_memories(organization_id: UUID, project_id: UUID, team_id: UUID | None) -> QuerySet[Memory]:
    return _admitted(Memory.objects.filter(organization_id=organization_id, project_id=project_id), team_id)


def _window_links(
    organization_id: UUID, project_id: UUID, link_type: str, start: datetime, end: datetime
) -> list[MemoryLink]:
    return list(
        MemoryLink.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            link_type=link_type,
            created_at__gte=start,
            created_at__lt=end,
        )
    )


def _first_transitions(links: list[MemoryLink], allowed_ids: set[UUID]) -> dict[UUID, tuple[datetime, str]]:
    transitions: dict[UUID, tuple[datetime, str]] = {}
    for link in links:
        if link.memory_id in allowed_ids and link.memory_id not in transitions:
            transitions[link.memory_id] = (link.created_at, str(link.id))

    return transitions


def _item(bucket: str, memory: Memory, occurrence_at: datetime, transition_ref: str) -> dict[str, object]:
    return {'bucket': bucket, 'memory': memory, 'occurrence_at': occurrence_at, 'transition_ref': transition_ref}


def _transition_items(
    bucket: str,
    transitions: dict[UUID, tuple[datetime, str]],
    link_memories: dict[UUID, Memory],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for memory_id, (occurrence_at, transition_ref) in transitions.items():
        memory = link_memories.get(memory_id)
        if memory is not None:
            items.append(_item(bucket, memory, occurrence_at, transition_ref))

    return items


def _weekly_link_changes(
    *,
    organization_id: UUID,
    project_id: UUID,
    team_id: UUID | None,
    start: datetime,
    end: datetime,
    blocked_ids: set[UUID],
) -> tuple[list[dict[str, object]], set[UUID]]:
    superseded_links = _window_links(organization_id, project_id, LinkType.SUPERSEDED_BY, start, end)
    merged_links = _window_links(organization_id, project_id, LinkType.NARROWED_BY, start, end)
    superseded_ids = {link.memory_id for link in superseded_links} - blocked_ids
    merged_ids = {link.memory_id for link in merged_links} - blocked_ids - superseded_ids

    link_ids = superseded_ids | merged_ids
    link_memories: dict[UUID, Memory] = {}
    if link_ids:
        link_memories = {
            memory.id: memory
            for memory in _scoped_memories(organization_id, project_id, team_id).filter(id__in=link_ids)
        }

    items = _transition_items('superseded', _first_transitions(superseded_links, superseded_ids), link_memories)
    items += _transition_items('merged', _first_transitions(merged_links, merged_ids), link_memories)

    return items, link_ids


def _classify_weekly_changes(
    *,
    organization_id: UUID,
    project_id: UUID,
    team_id: UUID | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    base = _scoped_memories(organization_id, project_id, team_id)

    refuted = list(
        base.filter(updated_at__gte=start, updated_at__lt=end).filter(Q(status=MemoryStatus.REFUTED) | Q(refuted=True))
    )
    refuted_ids = {memory.id for memory in refuted}

    retired = list(
        base.filter(status=MemoryStatus.ARCHIVED, updated_at__gte=start, updated_at__lt=end).exclude(id__in=refuted_ids)
    )
    retired_ids = {memory.id for memory in retired}

    link_items, link_ids = _weekly_link_changes(
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        start=start,
        end=end,
        blocked_ids=refuted_ids | retired_ids,
    )

    excluded = refuted_ids | retired_ids | link_ids
    added = list(base.filter(created_at__gte=start, created_at__lt=end).exclude(id__in=excluded))

    items = [_item('refuted', memory, memory.updated_at, str(memory.id)) for memory in refuted]
    items += [_item('retired', memory, memory.updated_at, str(memory.id)) for memory in retired]
    items += link_items
    items += [_item('added', memory, memory.created_at, str(memory.id)) for memory in added]

    return items


def freeze_weekly_digest_input(
    *,
    organization_id: UUID,
    project_id: UUID,
    team_id: UUID | None,
    window_start: datetime,
    window_end: datetime,
    schedule_key: str,
) -> dict[str, object]:
    _require_project(organization_id, project_id)
    if (
        team_id is not None
        and not ProjectTeam.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            team_id=team_id,
        ).exists()
    ):
        raise WorkflowWorkScopeError('team is not linked to the declared project')

    start = _to_utc(window_start, 'window_start')
    end = _to_utc(window_end, 'window_end')

    classified = _classify_weekly_changes(
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        start=start,
        end=end,
    )
    classified.sort(
        key=lambda item: (item['bucket'], item['occurrence_at'], str(item['memory'].id), item['transition_ref']),
    )
    versions = _pin_versions([item['memory'] for item in classified])

    changes: list[dict[str, object]] = []
    for position, item in enumerate(classified):
        memory = item['memory']
        version = versions[memory.id]
        ref = DigestSourceRef(
            render_position=position,
            memory_id=memory.id,
            memory_version_id=version.id,
            version=version.version,
            server_body_digest=_server_body_digest(version),
            visibility_scope=memory.visibility_scope,
            team_id=memory.team_id,
            source_title=memory.title,
        )
        change = _ref_to_dict(ref)
        change['bucket'] = item['bucket']
        change['occurrence_at'] = _canonical_timestamp(item['occurrence_at'])
        change['transition_ref'] = item['transition_ref']
        changes.append(change)

    allowed_team_ids = [str(team_id)] if team_id is not None else []
    snapshot: dict[str, object] = {
        'schema': _WEEKLY_SCHEMA,
        'project_id': str(project_id),
        'team_id': str(team_id) if team_id is not None else None,
        'schedule_key': schedule_key,
        'window_start': _canonical_timestamp(start),
        'window_end': _canonical_timestamp(end),
        'visibility_policy': _DIGEST_VISIBILITY_POLICY,
        'allowed_team_ids': allowed_team_ids,
        'output_visibility_scope': 'team' if team_id is not None else 'project',
        'output_team_id': str(team_id) if team_id is not None else None,
        'changes': changes,
    }
    snapshot['input_digest'] = _input_digest(snapshot)

    return snapshot


def _digest_source_refs(data: CreateWorkflowWorkInput) -> list[dict[str, object]]:
    if data.work_type == WorkflowWorkType.DAILY_DIGEST:
        refs = data.input_snapshot.get('sources')
    else:
        refs = data.input_snapshot.get('changes')

    return list(refs) if isinstance(refs, list) else []


def _validate_digest_sources(data: CreateWorkflowWorkInput) -> None:
    refs = _digest_source_refs(data)
    if not refs:
        return

    version_ids: list[UUID] = []
    for ref in refs:
        try:
            version_ids.append(uuid.UUID(str(ref['memory_version_id'])))
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowWorkScopeError('digest source reference is malformed') from error

    versions = {
        version.id: version
        for version in MemoryVersion.objects.filter(
            id__in=version_ids,
            organization_id=data.organization_id,
            project_id=data.project_id,
        ).only('id', 'memory_id', 'version', 'body')
    }
    for ref in refs:
        version = versions.get(uuid.UUID(str(ref['memory_version_id'])))
        if version is None:
            raise WorkflowWorkScopeError('digest source is outside the declared project scope')
        if str(version.memory_id) != ref.get('memory_id'):
            raise WorkflowWorkScopeError('digest source memory does not match its version')
        if version.version != ref.get('version'):
            raise WorkflowWorkScopeError('digest source version does not match its persisted version')
        if _server_body_digest(version) != ref.get('server_body_digest'):
            raise WorkflowWorkScopeError('digest source body digest does not match persisted content')


def _authorized_input_is_empty(data: CreateWorkflowWorkInput) -> bool:
    return not _digest_source_refs(data)


def create_digest_work_and_signal(
    *,
    data: CreateWorkflowWorkInput,
    signal_task: object,
    workflow_run: WorkflowRun | None = None,
) -> tuple[WorkflowWork, bool]:
    _validate_digest_sources(data)

    work, created = create_work(data)
    if not created:
        return work, created

    if _authorized_input_is_empty(data):
        resolved = resolve_work_no_input(
            work.id,
            organization_id=data.organization_id,
            project_id=data.project_id,
        )

        return resolved, created

    if work.disposition == WorkflowWorkDisposition.REQUIRED:
        dispatch_work_task(
            signal_task,
            work.id,
            workflow_run.id if workflow_run is not None else None,
        )

    return work, created


@dataclass(frozen=True, slots=True)
class _FrozenSource:
    title: str
    body: str


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def digest_output_identity(work: WorkflowWork) -> str:
    snapshot = work.input_snapshot
    payload = {
        'workflow_work_id': str(work.id),
        'input_digest': snapshot['input_digest'],
        'output_visibility_scope': snapshot['output_visibility_scope'],
        'output_team_id': snapshot['output_team_id'],
        'allowed_team_ids': list(snapshot['allowed_team_ids']),
    }

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _digest_visibility_metadata(work: WorkflowWork) -> dict[str, object]:
    snapshot = work.input_snapshot

    return {
        'schema': _DIGEST_VISIBILITY_POLICY,
        'workflow_work_id': str(work.id),
        'input_digest': snapshot['input_digest'],
        'output_identity': digest_output_identity(work),
        'allowed_team_ids': list(snapshot['allowed_team_ids']),
        'output_visibility_scope': snapshot['output_visibility_scope'],
        'output_team_id': snapshot['output_team_id'],
    }


def _verify_digest_fingerprint(work: WorkflowWork) -> None:
    try:
        fingerprint = work_input_fingerprint(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=work.input_snapshot,
        )
    except ValueError as error:
        raise MemoryWorkerError('workflow work fingerprint is invalid') from error

    if fingerprint != work.input_fingerprint:
        raise MemoryWorkerError('workflow work fingerprint does not match frozen input')


def _work_source_refs(work: WorkflowWork) -> list[dict[str, object]]:
    if work.work_type == WorkflowWorkType.DAILY_DIGEST:
        refs = work.input_snapshot.get('sources')
    else:
        refs = work.input_snapshot.get('changes')

    return list(refs) if isinstance(refs, list) else []


def _load_output_team(work: WorkflowWork) -> Team | None:
    if work.team_id is None:
        return None

    try:
        return Team.objects.get(id=work.team_id, organization_id=work.organization_id)
    except Team.DoesNotExist as error:
        raise MemoryWorkerError('digest output team is outside work scope') from error


def _existing_output(work: WorkflowWork) -> UUID | None:
    memory = (
        Memory.objects.filter(
            organization_id=work.organization_id,
            project_id=work.project_id,
            kind='digest',
            metadata__digest_visibility__workflow_work_id=str(work.id),
        )
        .order_by('created_at')
        .first()
    )

    return memory.id if memory is not None else None


def _lock_and_revalidate(
    work: WorkflowWork,
    refs: list[dict[str, object]],
    team: Team | None,
) -> dict[UUID, MemoryVersion]:
    memory_ids = sorted({UUID(str(ref['memory_id'])) for ref in refs})
    version_ids = sorted({UUID(str(ref['memory_version_id'])) for ref in refs})

    list(
        Memory.objects.select_for_update()
        .filter(id__in=memory_ids, organization_id=work.organization_id, project_id=work.project_id)
        .order_by('id')
    )
    locked_versions = list(
        MemoryVersion.objects.select_for_update()
        .filter(id__in=version_ids, organization_id=work.organization_id, project_id=work.project_id)
        .order_by('id')
    )
    if team is not None:
        linked = list(
            ProjectTeam.objects.select_for_update()
            .filter(organization_id=work.organization_id, project_id=work.project_id, team_id=team.id)
            .order_by('id')
        )
        if not linked:
            raise MemoryWorkerError('digest output team is no longer linked to its project')

    versions = {version.id: version for version in locked_versions}
    for ref in refs:
        version = versions.get(UUID(str(ref['memory_version_id'])))
        if version is None:
            raise MemoryWorkerError('digest source is missing its frozen version')
        if _server_body_digest(version) != ref.get('server_body_digest'):
            raise MemoryWorkerError('digest source body digest does not match frozen input')

    return versions


def _render_daily(
    work: WorkflowWork,
    refs: list[dict[str, object]],
    versions: dict[UUID, MemoryVersion],
) -> object:
    ordered = sorted(refs, key=lambda ref: ref['render_position'])
    sources = tuple(
        _FrozenSource(
            title=str(ref['source_title']),
            body=versions[UUID(str(ref['memory_version_id']))].body,
        )
        for ref in ordered
    )
    trace_id = f'digest-work:{work.id}'

    return render_frozen_daily_digest_provider_result(
        project=work.project,
        sources=sources,
        request_id=trace_id,
        trace_id=trace_id,
    )


def _weekly_title_body(snapshot: dict[str, object], refs: list[dict[str, object]]) -> tuple[str, str]:
    memory_changes: dict[str, list[dict[str, object]]] = {}
    for ref in sorted(refs, key=lambda item: item['render_position']):
        bucket = str(ref.get('bucket', 'added'))
        memory_changes.setdefault(bucket, []).append(
            {
                'id': str(ref['memory_id']),
                'title': str(ref['source_title']),
                'at': str(ref.get('occurrence_at', '')),
            }
        )
    window_end = _parse_iso(str(snapshot['window_end']))
    body = render_weekly_digest_body(memory_changes, window_end, 7)
    title = f'Weekly Structured Digest {str(snapshot["window_start"])[:10]} to {str(snapshot["window_end"])[:10]}'

    return title, body


def _publish(
    work: WorkflowWork,
    refs: list[dict[str, object]],
    team: Team | None,
    provider_result: object,
) -> UUID:
    snapshot = work.input_snapshot
    if work.work_type == WorkflowWorkType.DAILY_DIGEST:
        title = f'Digest {provider_result.generated_title}'
        body = provider_result.generated_body
        digest_kind = 'daily_structured'
    else:
        title, body = _weekly_title_body(snapshot, refs)
        digest_kind = 'weekly_structured'

    visibility = VisibilityScope.TEAM if team is not None else VisibilityScope.PROJECT
    metadata: dict[str, object] = {
        'kind': 'digest',
        'digest_kind': digest_kind,
        'source_memory_ids': [str(ref['memory_id']) for ref in refs],
        'digest_visibility': _digest_visibility_metadata(work),
    }
    if provider_result is not None:
        metadata['provider_call_id'] = str(provider_result.call_record_id)
        metadata['provider'] = provider_result.provider
        metadata['model'] = provider_result.model

    memory = Memory.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility,
        metadata=metadata,
    )
    version = MemoryVersion.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        memory=memory,
        version=1,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        source_metadata={'kind': 'digest'},
    )
    IndexMemoryVersion().execute(
        IndexMemoryVersionInput(memory_version_id=version.id, defer_embedding=True),
    )
    resolve_work_succeeded(
        work.id,
        organization_id=work.organization_id,
        project_id=work.project_id,
    )

    return memory.id


def execute_frozen_digest_work(work: WorkflowWork, workflow_run: WorkflowRun | None) -> UUID | None:
    _verify_digest_fingerprint(work)
    if work.disposition == WorkflowWorkDisposition.NO_OP:
        return None

    existing = _existing_output(work)
    if existing is not None:
        return existing

    refs = _work_source_refs(work)
    team = _load_output_team(work)

    with transaction.atomic():
        versions = _lock_and_revalidate(work, refs, team)

    provider_result = _render_daily(work, refs, versions) if work.work_type == WorkflowWorkType.DAILY_DIGEST else None

    with transaction.atomic():
        _lock_and_revalidate(work, refs, team)

        return _publish(work, refs, team, provider_result)
