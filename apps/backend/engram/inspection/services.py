from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Prefetch, Q, QuerySet

from engram.access.services import EffectiveScope
from engram.core.models import (
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Project,
    RetrievalDocument,
)
from engram.inspection.filters import (
    InspectionAuditEventFilterSet,
    InspectionContextBundleFilterSet,
    InspectionMemoryFilterSet,
)


class InspectionNotFoundError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InspectionScope:
    project: Project
    scope: EffectiveScope
    limit: int = 50
    offset: int = 0
    status: str | None = None
    kind: str | None = None
    search: str | None = None
    ordering: str | None = None
    session_id: str | None = None
    event_type: str | None = None
    correlation_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    @property
    def team_filter(self) -> Q:
        return Q(team__isnull=True) | Q(team_id__in=self.scope.team_ids)


MEMORY_ORDERING_FIELDS = ('created_at', '-created_at')
DEFAULT_MEMORY_ORDERING = '-created_at'


class ListInspectionMemories:
    def execute(self, inspection_scope: InspectionScope) -> QuerySet[Memory]:
        ordering = self._ordering(inspection_scope.ordering)
        qs = self._base_queryset(inspection_scope).order_by(ordering, 'id')
        filter_data = {
            'status': inspection_scope.status or MemoryStatus.APPROVED,
            'kind': inspection_scope.kind,
            'search': inspection_scope.search,
        }

        return InspectionMemoryFilterSet(data=filter_data, queryset=qs).qs

    def _ordering(self, ordering: str | None) -> str:
        if ordering in MEMORY_ORDERING_FIELDS:
            return ordering

        return DEFAULT_MEMORY_ORDERING

    def detail(self, inspection_scope: InspectionScope, memory_id: uuid.UUID) -> Memory:
        memory = self._base_queryset(inspection_scope).filter(id=memory_id).first()
        if memory is None:
            raise InspectionNotFoundError('memory_not_found', 'Memory was not found')

        return memory

    def count(self, inspection_scope: InspectionScope) -> int:
        qs = Memory.objects.filter(
            organization_id=inspection_scope.scope.organization_id,
            project=inspection_scope.project,
        ).filter(inspection_scope.team_filter)
        qs = qs.filter(status=inspection_scope.status or MemoryStatus.APPROVED)
        if inspection_scope.kind:
            qs = qs.filter(kind=inspection_scope.kind)

        return qs.count()

    def related_memories(
        self,
        inspection_scope: InspectionScope,
        memory_id: uuid.UUID,
    ) -> list[tuple[Memory, str | None]]:
        org_id = inspection_scope.scope.organization_id
        project = inspection_scope.project
        team_filter = inspection_scope.team_filter

        outgoing_links = list(
            MemoryLink.objects.filter(
                organization_id=org_id,
                project=project,
                memory_id=memory_id,
            )
        )
        incoming_links = list(
            MemoryLink.objects.filter(
                organization_id=org_id,
                project=project,
                target=str(memory_id),
            ).exclude(memory_id=memory_id)
        )

        related_id_to_link_type: dict[uuid.UUID, str | None] = {}
        for link in outgoing_links:
            try:
                target_id = uuid.UUID(str(link.target))
                if target_id != memory_id:
                    related_id_to_link_type[target_id] = link.link_type
            except ValueError:
                pass

        for link in incoming_links:
            mid = link.memory_id
            if mid not in related_id_to_link_type:
                related_id_to_link_type[mid] = link.link_type

        result: list[tuple[Memory, str | None]] = []
        if related_id_to_link_type:
            linked = list(
                Memory.objects.filter(
                    organization_id=org_id,
                    project=project,
                    id__in=related_id_to_link_type.keys(),
                )
                .filter(team_filter)
                .only('id', 'title')[:10]
            )
            result = [(m, related_id_to_link_type.get(m.id)) for m in linked]

        if len(result) < 10:
            existing_ids = {m.id for m, _ in result} | {memory_id}
            siblings = list(
                Memory.objects.filter(
                    organization_id=org_id,
                    project=project,
                )
                .filter(team_filter)
                .exclude(id__in=existing_ids)
                .only('id', 'title')[: 10 - len(result)]
            )
            result.extend((m, None) for m in siblings)

        return result

    def _base_queryset(self, inspection_scope: InspectionScope) -> QuerySet[Memory]:
        return (
            Memory.objects.filter(
                organization_id=inspection_scope.scope.organization_id,
                project=inspection_scope.project,
            )
            .filter(inspection_scope.team_filter)
            .select_related('project')
            .prefetch_related(
                Prefetch('versions', queryset=MemoryVersion.objects.order_by('version')),
                Prefetch('retrieval_documents', queryset=RetrievalDocument.objects.order_by('created_at', 'id')),
            )
        )


class ListInspectionContextBundles:
    def execute(self, inspection_scope: InspectionScope) -> QuerySet[ContextBundle]:
        qs = self._base_queryset(inspection_scope).order_by('created_at', 'id')
        filter_data = {
            'since': inspection_scope.since,
            'until': inspection_scope.until,
            'status': inspection_scope.status,
            'session_id': inspection_scope.session_id,
        }

        return InspectionContextBundleFilterSet(data=filter_data, queryset=qs).qs

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
        qs = (
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
        filter_data = {
            'event_type': inspection_scope.event_type,
            'correlation_id': inspection_scope.correlation_id,
            'since': inspection_scope.since,
            'until': inspection_scope.until,
        }

        return InspectionAuditEventFilterSet(data=filter_data, queryset=qs).qs

    def detail(self, inspection_scope: InspectionScope, audit_event_id: uuid.UUID) -> AuditEvent:
        ae = (
            AuditEvent.objects.filter(
                organization_id=inspection_scope.scope.organization_id,
                project=inspection_scope.project,
                id=audit_event_id,
            )
            .filter(inspection_scope.team_filter)
            .first()
        )
        if ae is None:
            raise InspectionNotFoundError('audit_event_not_found', 'Audit event was not found')

        return ae
