from __future__ import annotations

from typing import Any

from rest_framework import mixins, viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.imports import ImportJobSerializer
from engram.imports.models import ImportJob


class ImportJobViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ImportJobSerializer

    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get_queryset(self) -> Any:
        return (
            ImportJob.objects.filter(organization=self.request.active_organization)
            .select_related('project', 'team')
            .order_by('-created_at')
        )
