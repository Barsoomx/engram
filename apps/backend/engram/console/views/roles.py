from __future__ import annotations

from typing import Any

from rest_framework import mixins, viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated

from engram.access.models import Role
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.roles import RoleReadSerializer


class RoleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Role.objects.all()
    serializer_class = RoleReadSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('roles:read'),
        ]

    def get_queryset(self) -> Any:
        return Role.objects.all()
