from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.console.metrics_service import (
    get_activity,
    get_memory_ingest_daily,
    get_overview_metrics,
    get_sessions,
)
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability


class MetricsOverviewView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        data = get_overview_metrics(
            organization=request.active_organization,
            scope=request.effective_scope,
        )

        return Response(data)


class MetricsMemoryIngestView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        data = get_memory_ingest_daily(
            organization=request.active_organization,
            scope=request.effective_scope,
        )

        return Response(data)


class MetricsSessionsView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        data = get_sessions(
            organization=request.active_organization,
            scope=request.effective_scope,
        )

        return Response(data)


class MetricsActivityView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        data = get_activity(
            organization=request.active_organization,
            scope=request.effective_scope,
        )

        return Response(data)
