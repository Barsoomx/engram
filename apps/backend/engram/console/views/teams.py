from __future__ import annotations

from typing import Any

from rest_framework import mixins, viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.status import HTTP_204_NO_CONTENT

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.teams import TeamReadSerializer, TeamWriteSerializer
from engram.console.services import archive_team, audit_admin_action, create_team
from engram.core.models import Team


class TeamViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        if self.action in {'list', 'retrieve'}:
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('teams:read'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('teams:admin'),
        ]

    def get_queryset(self) -> Any:
        return Team.objects.filter(
            organization=self.request.active_organization,
            archived_at__isnull=True,
        )

    def get_serializer_context(self) -> dict:
        context = super().get_serializer_context()

        context['organization'] = self.request.active_organization

        return context

    def get_serializer_class(self) -> type:
        if self.action in {'create', 'partial_update', 'update'}:
            return TeamWriteSerializer

        return TeamReadSerializer

    def perform_create(self, serializer: BaseSerializer) -> None:
        team = create_team(
            organization=self.request.active_organization,
            name=serializer.validated_data['name'],
            slug=serializer.validated_data['slug'],
        )

        serializer.instance = team

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='TeamCreated',
            target_type='team',
            target_id=str(team.id),
            metadata={'slug': team.slug, 'name': team.name},
        )

    def perform_update(self, serializer: BaseSerializer) -> None:
        instance = serializer.instance

        changed_fields = sorted(set(serializer.validated_data.keys()))

        serializer.save()

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='TeamUpdated',
            target_type='team',
            target_id=str(instance.id),
            metadata={'fields': changed_fields},
        )

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        team = self.get_object()

        archive_team(team)

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='TeamArchived',
            target_type='team',
            target_id=str(team.id),
        )

        return Response(status=HTTP_204_NO_CONTENT)
