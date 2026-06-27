from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db.models import Prefetch, Q, QuerySet

from engram.access.services import EffectiveScope, ResolveApiKeyScope
from engram.core.models import (
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryVersion,
    Project,
    RetrievalDocument,
)


class InspectionNotFoundError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InspectionScopeInput:
    raw_key: str
    required_capability: str
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    target_type: str
    target_id: str


@dataclass(frozen=True)
class InspectionScope:
    project: Project
    scope: EffectiveScope

    @property
    def team_filter(self) -> Q:
        return Q(team__isnull=True) | Q(team_id__in=self.scope.team_ids)


class ResolveInspectionScope:
    def execute(self, data: InspectionScopeInput) -> InspectionScope:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability=data.required_capability,
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            target_type=data.target_type,
            target_id=data.target_id,
        )
        project = Project.objects.get(organization_id=scope.organization_id, id=data.project_id)

        return InspectionScope(project=project, scope=scope)


class ListInspectionMemories:
    def execute(self, inspection_scope: InspectionScope) -> QuerySet[Memory]:
        return self._base_queryset(inspection_scope).order_by('created_at', 'id')

    def detail(self, inspection_scope: InspectionScope, memory_id: uuid.UUID) -> Memory:
        memory = self._base_queryset(inspection_scope).filter(id=memory_id).first()
        if memory is None:
            raise InspectionNotFoundError('memory_not_found', 'Memory was not found')

        return memory

    def _base_queryset(self, inspection_scope: InspectionScope) -> QuerySet[Memory]:
        return (
            Memory.objects.filter(
                organization_id=inspection_scope.scope.organization_id,
                project=inspection_scope.project,
            )
            .filter(inspection_scope.team_filter)
            .prefetch_related(
                Prefetch('versions', queryset=MemoryVersion.objects.order_by('version')),
                Prefetch('retrieval_documents', queryset=RetrievalDocument.objects.order_by('created_at', 'id')),
            )
        )


class ListInspectionContextBundles:
    def execute(self, inspection_scope: InspectionScope) -> QuerySet[ContextBundle]:
        return self._base_queryset(inspection_scope).order_by('created_at', 'id')

    def detail(self, inspection_scope: InspectionScope, bundle_id: uuid.UUID) -> ContextBundle:
        bundle = self._base_queryset(inspection_scope).filter(id=bundle_id).first()
        if bundle is None:
            raise InspectionNotFoundError('context_bundle_not_found', 'Context bundle was not found')

        return bundle

    def _base_queryset(self, inspection_scope: InspectionScope) -> QuerySet[ContextBundle]:
        return (
            ContextBundle.objects.filter(
                organization_id=inspection_scope.scope.organization_id,
                project=inspection_scope.project,
            )
            .filter(inspection_scope.team_filter)
            .select_related('agent', 'session')
            .prefetch_related(
                Prefetch(
                    'items',
                    queryset=ContextBundleItem.objects.select_related(
                        'memory',
                        'retrieval_document',
                        'retrieval_document__memory_version',
                    ).order_by('rank', 'id'),
                ),
            )
        )


class ListInspectionAuditEvents:
    def execute(self, inspection_scope: InspectionScope) -> QuerySet[AuditEvent]:
        return (
            AuditEvent.objects.filter(
                organization_id=inspection_scope.scope.organization_id,
                project=inspection_scope.project,
            )
            .filter(inspection_scope.team_filter)
            .exclude(
                event_type='AccessScopeResolved',
                target_type='audit_event',
                capability='audit:read',
            )
            .order_by('created_at', 'id')
        )
