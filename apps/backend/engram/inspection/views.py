from __future__ import annotations

import uuid
from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response, bearer_key
from engram.core.models import AuditEvent, ContextBundle, ContextBundleItem, Memory, MemoryVersion, RetrievalDocument
from engram.core.redaction import redact_value
from engram.inspection.serializers import InspectionQuerySerializer
from engram.inspection.services import (
    InspectionNotFoundError,
    InspectionScope,
    InspectionScopeInput,
    ListInspectionAuditEvents,
    ListInspectionContextBundles,
    ListInspectionMemories,
    ResolveInspectionScope,
)

NOT_FOUND_STATUS = {
    'memory_not_found': status.HTTP_404_NOT_FOUND,
    'context_bundle_not_found': status.HTTP_404_NOT_FOUND,
    'audit_event_not_found': status.HTTP_404_NOT_FOUND,
}


class InspectionBaseView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []
    required_capability = ''
    target_type = ''

    def _inspection_scope(self, request: Request, target_id: str) -> InspectionScope:
        serializer = InspectionQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        return ResolveInspectionScope().execute(
            InspectionScopeInput(
                raw_key=bearer_key(request),
                required_capability=self.required_capability,
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type=self.target_type,
                target_id=target_id,
            ),
        )


class MemoryInspectionListView(InspectionBaseView):
    required_capability = 'memories:admin'
    target_type = 'memory'

    def get(self, request: Request) -> Response:
        try:
            memories = ListInspectionMemories().execute(self._inspection_scope(request, 'list'))
        except AccessDeniedError as error:
            return access_error_response(error)

        items = [memory_response(memory, include_detail=False) for memory in memories]

        return Response({'count': len(items), 'items': items})


class MemoryInspectionDetailView(InspectionBaseView):
    required_capability = 'memories:admin'
    target_type = 'memory'

    def get(self, request: Request, memory_id: uuid.UUID) -> Response:
        try:
            memory = ListInspectionMemories().detail(self._inspection_scope(request, str(memory_id)), memory_id)
        except AccessDeniedError as error:
            return access_error_response(error)
        except InspectionNotFoundError as error:
            return not_found_response(error)

        return Response(memory_response(memory, include_detail=True))


class ContextBundleInspectionListView(InspectionBaseView):
    required_capability = 'memories:admin'
    target_type = 'context_bundle'

    def get(self, request: Request) -> Response:
        try:
            bundles = ListInspectionContextBundles().execute(self._inspection_scope(request, 'list'))
        except AccessDeniedError as error:
            return access_error_response(error)

        items = [context_bundle_response(bundle, include_detail=False) for bundle in bundles]

        return Response({'count': len(items), 'items': items})


class ContextBundleInspectionDetailView(InspectionBaseView):
    required_capability = 'memories:admin'
    target_type = 'context_bundle'

    def get(self, request: Request, bundle_id: uuid.UUID) -> Response:
        try:
            bundle = ListInspectionContextBundles().detail(self._inspection_scope(request, str(bundle_id)), bundle_id)
        except AccessDeniedError as error:
            return access_error_response(error)
        except InspectionNotFoundError as error:
            return not_found_response(error)

        return Response(context_bundle_response(bundle, include_detail=True))


class AuditEventInspectionListView(InspectionBaseView):
    required_capability = 'audit:read'
    target_type = 'audit_event'

    def get(self, request: Request) -> Response:
        try:
            audit_events = ListInspectionAuditEvents().execute(self._inspection_scope(request, 'list'))
        except AccessDeniedError as error:
            return access_error_response(error)

        items = [audit_event_response(audit_event) for audit_event in audit_events]

        return Response({'count': len(items), 'items': items})


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


def memory_response(memory: Memory, *, include_detail: bool) -> dict[str, object]:
    response: dict[str, object] = {
        'id': str(memory.id),
        'project_id': str(memory.project_id),
        'team_id': str(memory.team_id) if memory.team_id else None,
        'title': redacted_text(memory.title),
        'body': redacted_text(memory.body),
        'status': memory.status,
        'visibility_scope': memory.visibility_scope,
        'current_version': memory.current_version,
        'confidence': str(memory.confidence) if memory.confidence is not None else None,
        'stale': memory.stale,
        'refuted': memory.refuted,
        'metadata': redacted(memory.metadata),
        'created_at': timestamp(memory.created_at),
        'updated_at': timestamp(memory.updated_at),
    }
    if include_detail:
        response['versions'] = [memory_version_response(version) for version in memory.versions.all()]
        response['retrieval_documents'] = [
            retrieval_document_response(document) for document in memory.retrieval_documents.all()
        ]

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
        'token_budget': bundle.token_budget,
        'selected_count': bundle.selected_count,
        'status': bundle.status,
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
        'rank': item.rank,
        'citation': item.citation,
        'inclusion_reason': redacted_text(item.inclusion_reason),
        'scope_evidence': redacted(item.scope_evidence),
        'metadata': redacted(item.metadata),
        'created_at': timestamp(item.created_at),
        'updated_at': timestamp(item.updated_at),
    }


def audit_event_response(audit_event: AuditEvent) -> dict[str, Any]:
    return {
        'id': str(audit_event.id),
        'project_id': str(audit_event.project_id) if audit_event.project_id else None,
        'team_id': str(audit_event.team_id) if audit_event.team_id else None,
        'event_type': audit_event.event_type,
        'actor_type': audit_event.actor_type,
        'actor_id': redacted_text(audit_event.actor_id),
        'target_type': audit_event.target_type,
        'target_id': redacted_text(audit_event.target_id),
        'capability': audit_event.capability,
        'result': audit_event.result,
        'request_id': redacted_text(audit_event.request_id),
        'correlation_id': redacted_text(audit_event.correlation_id),
        'metadata': redacted(audit_event.metadata),
        'created_at': timestamp(audit_event.created_at),
        'updated_at': timestamp(audit_event.updated_at),
    }
