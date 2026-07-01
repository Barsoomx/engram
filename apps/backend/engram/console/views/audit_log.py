from __future__ import annotations

import uuid
from typing import Any

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from engram.access.models import ApiKey, Identity
from engram.console.filters import AuditEventFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.audit_log import AuditEventSerializer
from engram.core.models import AuditEvent, Memory, Project, Team
from engram.core.redaction import redact_value


def _redacted_text(value: str) -> str:
    return str(redact_value(value).value)


class AuditEventViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = AuditEventSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = AuditEventFilterSet

    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('audit:read'),
        ]

    def get_queryset(self) -> Any:
        return (
            AuditEvent.objects.filter(
                organization=self.request.active_organization,
            )
            .select_related('project', 'team')
            .order_by('-created_at')
        )

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)

        events = list(page) if page is not None else list(queryset)

        org_id = request.active_organization.id

        context = {
            **self.get_serializer_context(),
            'actor_name_map': _batch_resolve_actor_names(events, org_id),
            'target_name_map': _batch_resolve_target_names(events, org_id),
        }

        serializer = AuditEventSerializer(events, many=True, context=context)

        if page is not None:
            return self.get_paginated_response(serializer.data)

        return Response(serializer.data)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        instance = self.get_object()

        org_id = request.active_organization.id

        context = {
            **self.get_serializer_context(),
            'actor_name_map': _batch_resolve_actor_names([instance], org_id),
            'target_name_map': _batch_resolve_target_names([instance], org_id),
        }

        serializer = AuditEventSerializer(instance, context=context)

        return Response(serializer.data)


def _batch_resolve_actor_names(
    events: list[AuditEvent],
    organization_id: uuid.UUID,
) -> dict[str, str | None]:
    name_map: dict[str, str | None] = {}

    api_key_ids = _valid_uuids({e.actor_id for e in events if e.actor_type == 'api_key'})

    if api_key_ids:
        for key in ApiKey.objects.filter(
            organization_id=organization_id,
            id__in=api_key_ids,
        ).select_related('owner_identity'):
            name_map[str(key.id)] = key.owner_identity.display_name

    identity_ids = _valid_uuids({e.actor_id for e in events if e.actor_type == 'identity'})

    if identity_ids:
        for ident in Identity.objects.filter(
            organization_id=organization_id,
            id__in=identity_ids,
        ):
            name_map[str(ident.id)] = ident.display_name

    return name_map


def _batch_resolve_target_names(
    events: list[AuditEvent],
    organization_id: uuid.UUID,
) -> dict[tuple[str, str], str | None]:
    by_type: dict[str, set[str]] = {}

    for e in events:
        if e.target_type and e.target_id:
            by_type.setdefault(e.target_type, set()).add(e.target_id)

    name_map: dict[tuple[str, str], str | None] = {}

    _resolve_memory_targets(by_type, organization_id, name_map)
    _resolve_project_targets(by_type, organization_id, name_map)
    _resolve_team_targets(by_type, organization_id, name_map)
    _resolve_identity_targets(by_type, organization_id, name_map)

    return name_map


def _resolve_memory_targets(
    by_type: dict[str, set[str]],
    organization_id: uuid.UUID,
    name_map: dict[tuple[str, str], str | None],
) -> None:
    ids = _valid_uuids(by_type.get('memory', set()))

    if not ids:
        return

    for m in Memory.objects.filter(organization_id=organization_id, id__in=ids).only('id', 'title'):
        name_map[('memory', str(m.id))] = _redacted_text(m.title)


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
