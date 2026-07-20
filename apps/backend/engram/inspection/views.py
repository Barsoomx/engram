from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.models import ApiKey, Identity
from engram.access.request_scope import resolve_request_scope
from engram.core.models import (
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.core.redaction import redact_value
from engram.inspection.serializers import InspectionQuerySerializer
from engram.inspection.services import (
    InspectionNotFoundError,
    InspectionScope,
    ListInspectionAuditEvents,
    ListInspectionContextBundles,
    ListInspectionMemories,
)
from engram.memory.digest_visibility import unproven_digest_memory_ids

NOT_FOUND_STATUS = {
    'memory_not_found': status.HTTP_404_NOT_FOUND,
    'context_bundle_not_found': status.HTTP_404_NOT_FOUND,
    'audit_event_not_found': status.HTTP_404_NOT_FOUND,
}


class InspectionBaseView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = []
    required_capability = ''
    target_type = ''

    def _inspection_scope(self, request: Request, target_id: str) -> InspectionScope:
        serializer = InspectionQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        scope = resolve_request_scope(
            request,
            required_capability=self.required_capability,
            project_id=data['project_id'],
            team_id=data.get('team_id'),
            target_type=self.target_type,
            target_id=target_id,
        )
        project = Project.objects.get(organization_id=scope.organization_id, id=data['project_id'])

        return InspectionScope(
            project=project,
            scope=scope,
            limit=data.get('limit', 50),
            offset=data.get('offset', 0),
            status=data.get('status') or None,
            kind=data.get('kind') or None,
            search=data.get('search') or None,
            ordering=data.get('ordering') or None,
            session_id=data.get('session_id') or None,
            event_type=data.get('event_type') or None,
            correlation_id=data.get('correlation_id') or None,
            target_id=data.get('target_id') or None,
            target_type=data.get('target_type') or None,
            since=data.get('since'),
            until=data.get('until'),
        )


class MemoryInspectionListView(InspectionBaseView):
    required_capability = 'memories:read'
    target_type = 'memory'

    def get(self, request: Request) -> Response:
        inspection_scope = self._inspection_scope(request, 'list')
        qs = ListInspectionMemories().execute(inspection_scope)

        total = qs.count()
        limit = inspection_scope.limit
        offset = inspection_scope.offset
        items = [memory_response(memory, include_detail=False) for memory in qs[offset : offset + limit]]

        return Response({'count': total, 'items': items})


class MemoryInspectionCountView(InspectionBaseView):
    required_capability = 'memories:read'
    target_type = 'memory'

    def get(self, request: Request) -> Response:
        inspection_scope = self._inspection_scope(request, 'count')
        count = ListInspectionMemories().count(inspection_scope)

        return Response({'count': count})


class MemoryInspectionDetailView(InspectionBaseView):
    required_capability = 'memories:read'
    target_type = 'memory'

    def get(self, request: Request, memory_id: uuid.UUID) -> Response:
        try:
            inspection_scope = self._inspection_scope(request, str(memory_id))
            memory = ListInspectionMemories().detail(inspection_scope, memory_id)
        except InspectionNotFoundError as error:
            return not_found_response(error)

        return Response(memory_response(memory, include_detail=True, inspection_scope=inspection_scope))


class ContextBundleInspectionListView(InspectionBaseView):
    required_capability = 'context:read'
    target_type = 'context_bundle'

    def get(self, request: Request) -> Response:
        inspection_scope = self._inspection_scope(request, 'list')
        qs = ListInspectionContextBundles().execute(inspection_scope)

        total = qs.count()
        limit = inspection_scope.limit
        offset = inspection_scope.offset
        items = [context_bundle_response(bundle, include_detail=False) for bundle in qs[offset : offset + limit]]

        return Response({'count': total, 'items': items})


class ContextBundleInspectionDetailView(InspectionBaseView):
    required_capability = 'context:read'
    target_type = 'context_bundle'

    def get(self, request: Request, bundle_id: uuid.UUID) -> Response:
        try:
            bundle = ListInspectionContextBundles().detail(self._inspection_scope(request, str(bundle_id)), bundle_id)
        except InspectionNotFoundError as error:
            return not_found_response(error)

        return Response(context_bundle_response(bundle, include_detail=True))


class AuditEventInspectionListView(InspectionBaseView):
    required_capability = 'audit:read'
    target_type = 'audit_event'

    def get(self, request: Request) -> Response:
        inspection_scope = self._inspection_scope(request, 'list')
        qs = ListInspectionAuditEvents().execute(inspection_scope)

        total = qs.count()
        limit = inspection_scope.limit
        offset = inspection_scope.offset
        audit_events = list(qs[offset : offset + limit])
        org_id = inspection_scope.scope.organization_id
        actor_name_map = _batch_resolve_actor_names(audit_events, org_id)
        target_name_map = _batch_resolve_target_names(audit_events, org_id, inspection_scope)

        items = [
            audit_event_response(ae, actor_name_map=actor_name_map, target_name_map=target_name_map)
            for ae in audit_events
        ]

        return Response({'count': total, 'items': items})


class AuditEventInspectionDetailView(InspectionBaseView):
    required_capability = 'audit:read'
    target_type = 'audit_event'

    def get(self, request: Request, audit_event_id: uuid.UUID) -> Response:
        try:
            inspection_scope = self._inspection_scope(request, str(audit_event_id))
            ae = ListInspectionAuditEvents().detail(inspection_scope, audit_event_id)
        except InspectionNotFoundError as error:
            return not_found_response(error)

        org_id = inspection_scope.scope.organization_id
        actor_name_map = _batch_resolve_actor_names([ae], org_id)
        target_name_map = _batch_resolve_target_names([ae], org_id, inspection_scope)

        return Response(audit_event_response(ae, actor_name_map=actor_name_map, target_name_map=target_name_map))


def not_found_response(error: InspectionNotFoundError) -> Response:
    return Response(
        {'code': error.code, 'detail': str(error)},
        status=NOT_FOUND_STATUS.get(error.code, status.HTTP_404_NOT_FOUND),
    )


def redacted(value: object) -> object:
    return redact_value(value).value


def redacted_text(value: str) -> str:
    return str(redacted(value))


def timestamp(value: object) -> str | None:
    if value is None:
        return None

    return value.isoformat()


def _memory_source_provenance(memory: Memory) -> tuple[str | None, str | None]:
    for version in memory.versions.all():
        observation = version.source_observation
        if observation is None:
            continue

        session_id = str(observation.session_id) if observation.session_id else None
        correlation_id = None
        raw_event = observation.raw_event
        if raw_event is not None and raw_event.correlation_id:
            correlation_id = raw_event.correlation_id

        return session_id, correlation_id

    return None, None


def memory_response(
    memory: Memory,
    *,
    include_detail: bool,
    inspection_scope: InspectionScope | None = None,
) -> dict[str, object]:
    metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
    kind = metadata.get('kind') or None
    tags = metadata.get('tags', [])
    file_paths = redacted(metadata.get('file_paths', []))
    confidence = memory.confidence
    confidence_percent: float | None = round(float(confidence) * 100, 1) if confidence is not None else None
    authorized_for_injection = memory.status == MemoryStatus.APPROVED and not memory.stale and not memory.refuted

    response: dict[str, object] = {
        'id': str(memory.id),
        'project_id': str(memory.project_id),
        'project_name': memory.project.name,
        'project_slug': memory.project.slug,
        'team_id': str(memory.team_id) if memory.team_id else None,
        'title': redacted_text(memory.title),
        'body': redacted_text(memory.body),
        'status': memory.status,
        'visibility_scope': memory.visibility_scope,
        'current_version': memory.current_version,
        'confidence': str(memory.confidence) if memory.confidence is not None else None,
        'confidence_percent': confidence_percent,
        'stale': memory.stale,
        'refuted': memory.refuted,
        'authorized_for_injection': authorized_for_injection,
        'kind': kind,
        'tags': redacted(tags),
        'file_paths': file_paths,
        'captured_by': redacted(metadata.get('captured_by')),
        'metadata': redacted(memory.metadata),
        'created_at': timestamp(memory.created_at),
        'updated_at': timestamp(memory.updated_at),
    }
    if include_detail:
        source_session_id, source_correlation_id = _memory_source_provenance(memory)
        response['source_session_id'] = source_session_id
        response['source_correlation_id'] = source_correlation_id
        response['versions'] = [memory_version_response(version) for version in memory.versions.all()]
        response['retrieval_documents'] = [
            retrieval_document_response(document) for document in memory.retrieval_documents.all()
        ]
        if inspection_scope is not None:
            related_list = ListInspectionMemories().related_memories(inspection_scope, memory.id)
            response['related'] = [
                {
                    'id': str(m.id),
                    'title': redacted_text(m.title),
                    'link_type': lt,
                }
                for m, lt in related_list
            ]
        else:
            response['related'] = []

    return response


def memory_version_response(version: MemoryVersion) -> dict[str, object]:
    return {
        'id': str(version.id),
        'memory_id': str(version.memory_id),
        'version': version.version,
        'body': redacted_text(version.body),
        'content_hash': redacted_text(version.content_hash),
        'source_observation_id': str(version.source_observation_id) if version.source_observation_id else None,
        'source_metadata': redacted(version.source_metadata),
        'created_at': timestamp(version.created_at),
        'updated_at': timestamp(version.updated_at),
    }


def retrieval_document_response(document: RetrievalDocument) -> dict[str, object]:
    return {
        'id': str(document.id),
        'memory_id': str(document.memory_id),
        'memory_version_id': str(document.memory_version_id),
        'team_id': str(document.team_id) if document.team_id else None,
        'visibility_scope': document.visibility_scope,
        'source_observation_ids': redacted(document.source_observation_ids),
        'file_paths': redacted(document.file_paths),
        'symbols': redacted(document.symbols),
        'exact_terms': redacted(document.exact_terms),
        'full_text': redacted_text(document.full_text),
        'embedding_reference': redacted_text(document.embedding_reference),
        'stale': document.stale,
        'refuted': document.refuted,
        'metadata': redacted(document.metadata),
        'created_at': timestamp(document.created_at),
        'updated_at': timestamp(document.updated_at),
    }


def context_bundle_response(bundle: ContextBundle, *, include_detail: bool) -> dict[str, object]:
    response: dict[str, object] = {
        'id': str(bundle.id),
        'project_id': str(bundle.project_id),
        'team_id': str(bundle.team_id) if bundle.team_id else None,
        'agent_id': str(bundle.agent_id),
        'session_id': str(bundle.session_id),
        'request_id': redacted_text(bundle.request_id),
        'purpose': bundle.purpose,
        'query_text': redacted_text(bundle.query_text),
        'rendered_text': redacted_text(bundle.rendered_text),
        'authorization_scope': redacted(bundle.authorization_scope),
        'retrieval_latency_ms': bundle.retrieval_latency_ms,
        'token_budget': bundle.token_budget,
        'selected_count': bundle.selected_count,
        'status': bundle.status,
        'warnings': redacted(bundle.metadata.get('warnings', [])),
        'metadata': redacted(bundle.metadata),
        'created_at': timestamp(bundle.created_at),
        'updated_at': timestamp(bundle.updated_at),
    }
    if include_detail:
        response['items'] = [context_bundle_item_response(item) for item in bundle.items.all()]

    return response


def context_bundle_item_response(item: ContextBundleItem) -> dict[str, object]:
    return {
        'id': str(item.id),
        'bundle_id': str(item.bundle_id),
        'memory_id': str(item.memory_id),
        'retrieval_document_id': str(item.retrieval_document_id),
        'kind': item.memory.kind,
        'confidence': str(item.memory.confidence) if item.memory.confidence is not None else None,
        'rank': item.rank,
        'citation': item.citation,
        'inclusion_reason': redacted_text(item.inclusion_reason),
        'scope_evidence': redacted(item.scope_evidence),
        'metadata': redacted(item.metadata),
        'created_at': timestamp(item.created_at),
        'updated_at': timestamp(item.updated_at),
    }


def audit_event_response(
    audit_event: AuditEvent,
    *,
    actor_name_map: dict[str, str | None] | None = None,
    target_name_map: dict[tuple[str, str], str | None] | None = None,
) -> dict[str, Any]:
    actor_display: str | None = None
    if actor_name_map is not None:
        actor_display = actor_name_map.get(audit_event.actor_id)

    target_display: str | None = None
    if target_name_map is not None:
        target_display = target_name_map.get((audit_event.target_type, audit_event.target_id))

    return {
        'id': str(audit_event.id),
        'project_id': str(audit_event.project_id) if audit_event.project_id else None,
        'team_id': str(audit_event.team_id) if audit_event.team_id else None,
        'event_type': audit_event.event_type,
        'actor_type': audit_event.actor_type,
        'actor_id': redacted_text(audit_event.actor_id),
        'actor_display': actor_display,
        'target_type': audit_event.target_type,
        'target_id': redacted_text(audit_event.target_id),
        'target_display': target_display,
        'capability': audit_event.capability,
        'result': audit_event.result,
        'request_id': redacted_text(audit_event.request_id),
        'correlation_id': redacted_text(audit_event.correlation_id),
        'metadata': redacted(audit_event.metadata),
        'created_at': timestamp(audit_event.created_at),
        'updated_at': timestamp(audit_event.updated_at),
    }


def _batch_resolve_actor_names(
    events: list[AuditEvent],
    organization_id: uuid.UUID,
) -> dict[str, str | None]:
    name_map: dict[str, str | None] = {}

    api_key_ids = _valid_uuids({e.actor_id for e in events if e.actor_type == 'api_key'})
    if api_key_ids:
        keys = ApiKey.objects.filter(
            organization_id=organization_id,
            id__in=api_key_ids,
        ).select_related('owner_identity')
        for key in keys:
            name_map[str(key.id)] = key.owner_identity.display_name

    identity_ids = _valid_uuids({e.actor_id for e in events if e.actor_type == 'identity'})
    if identity_ids:
        identities = Identity.objects.filter(
            organization_id=organization_id,
            id__in=identity_ids,
        )
        for ident in identities:
            name_map[str(ident.id)] = ident.display_name

    return name_map


def _batch_resolve_target_names(
    events: list[AuditEvent],
    organization_id: uuid.UUID,
    inspection_scope: InspectionScope,
) -> dict[tuple[str, str], str | None]:
    by_type: dict[str, set[str]] = {}
    for e in events:
        if e.target_type and e.target_id:
            by_type.setdefault(e.target_type, set()).add(e.target_id)

    name_map: dict[tuple[str, str], str | None] = {}
    _resolve_memory_targets(by_type, organization_id, name_map, inspection_scope)
    _resolve_project_targets(by_type, organization_id, name_map)
    _resolve_team_targets(by_type, organization_id, name_map)
    _resolve_identity_targets(by_type, organization_id, name_map)

    return name_map


def _resolve_memory_targets(
    by_type: dict[str, set[str]],
    organization_id: uuid.UUID,
    name_map: dict[tuple[str, str], str | None],
    inspection_scope: InspectionScope,
) -> None:
    ids = _valid_uuids(by_type.get('memory', set()))
    if not ids:
        return

    scope = inspection_scope.scope
    project = inspection_scope.project
    visibility_whitelist = Q(visibility_scope=VisibilityScope.PROJECT) | Q(
        visibility_scope=VisibilityScope.TEAM,
        team_id__in=scope.team_ids,
    )
    memories = Memory.objects.filter(organization_id=organization_id, project=project, id__in=ids).filter(
        visibility_whitelist
    )

    digests = Memory.objects.filter(
        organization_id=organization_id,
        project=project,
        kind='digest',
        id__in=ids,
    ).filter(visibility_whitelist)
    unproven = unproven_digest_memory_ids(digests)
    if unproven:
        memories = memories.exclude(id__in=unproven)

    for m in memories.only('id', 'title'):
        name_map[('memory', str(m.id))] = redacted_text(m.title)


def _resolve_project_targets(
    by_type: dict[str, set[str]],
    organization_id: uuid.UUID,
    name_map: dict[tuple[str, str], str | None],
) -> None:
    ids = _valid_uuids(by_type.get('project', set()))
    if not ids:
        return

    for p in Project.objects.filter(organization_id=organization_id, id__in=ids).only('id', 'name'):
        name_map[('project', str(p.id))] = p.name


def _resolve_team_targets(
    by_type: dict[str, set[str]],
    organization_id: uuid.UUID,
    name_map: dict[tuple[str, str], str | None],
) -> None:
    ids = _valid_uuids(by_type.get('team', set()))
    if not ids:
        return

    for t in Team.objects.filter(organization_id=organization_id, id__in=ids).only('id', 'name'):
        name_map[('team', str(t.id))] = t.name


def _resolve_identity_targets(
    by_type: dict[str, set[str]],
    organization_id: uuid.UUID,
    name_map: dict[tuple[str, str], str | None],
) -> None:
    ids = _valid_uuids(by_type.get('identity', set()))
    if not ids:
        return

    for ident in Identity.objects.filter(
        organization_id=organization_id,
        id__in=ids,
    ).only('id', 'display_name'):
        name_map[('identity', str(ident.id))] = ident.display_name


def _valid_uuids(raw_ids: set[str]) -> list[uuid.UUID]:
    result = []
    for raw in raw_ids:
        try:
            result.append(uuid.UUID(str(raw)))
        except ValueError:
            pass

    return result
