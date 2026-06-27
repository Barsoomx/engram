from __future__ import annotations

from typing import Any

from rest_framework import mixins, viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.serializers import BaseSerializer

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.organizations import (
    OrganizationReadSerializer,
    OrganizationWriteSerializer,
)
from engram.console.services import audit_admin_action
from engram.core.models import Organization


class OrganizationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        if self.action == 'list':
            return [IsAuthenticated()]

        if self.action == 'retrieve':
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('organizations:read'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('organizations:admin'),
        ]

    def get_queryset(self) -> Any:
        if self.action == 'list':
            identity_ids = Identity.objects.filter(
                identity_type=IdentityType.USER,
                external_id=external_id_for_user(self.request.user),
            ).values('id')

            return Organization.objects.filter(
                organization_memberships__identity_id__in=identity_ids,
                organization_memberships__active=True,
            ).distinct()

        return Organization.objects.filter(
            organization_memberships__identity=self.request.user_identity,
            organization_memberships__active=True,
        ).distinct()

    def get_serializer_class(self) -> type:
        if self.action in {'partial_update', 'update'}:
            return OrganizationWriteSerializer

        return OrganizationReadSerializer

    def perform_update(self, serializer: BaseSerializer) -> None:
        instance = serializer.instance

        changed_fields = sorted(set(serializer.validated_data.keys()))

        serializer.save()

        audit_admin_action(
            organization=instance,
            actor_identity=self.request.user_identity,
            event_type='OrganizationUpdated',
            target_type='organization',
            target_id=str(instance.id),
            metadata={'fields': changed_fields},
        )
