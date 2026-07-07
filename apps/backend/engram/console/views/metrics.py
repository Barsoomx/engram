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
from engram.console.serializers.metrics import MetricsScopeQuerySerializer


def _metrics_permissions() -> list[BasePermission]:
    return [
        IsAuthenticated(),
        ActiveOrganizationPermission(),
        RequireCapability('memories:read'),
    ]


def _scope_params(request: Request) -> dict[str, Any]:
    serializer = MetricsScopeQuerySerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)

    return {
        'project_id': serializer.validated_data.get('project_id'),
        'team_id': serializer.validated_data.get('team_id'),
    }


class MetricsOverviewView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return _metrics_permissions()

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        params = _scope_params(request)

        data = get_overview_metrics(
            organization=request.active_organization,
            scope=request.effective_scope,
            project_id=params['project_id'],
            team_id=params['team_id'],
        )

        return Response(data)


class MetricsMemoryIngestView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return _metrics_permissions()

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        params = _scope_params(request)

        data = get_memory_ingest_daily(
            organization=request.active_organization,
            scope=request.effective_scope,
            project_id=params['project_id'],
            team_id=params['team_id'],
        )

        return Response(data)


class MetricsSessionsView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return _metrics_permissions()

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        params = _scope_params(request)

        data = get_sessions(
            organization=request.active_organization,
            scope=request.effective_scope,
            project_id=params['project_id'],
            team_id=params['team_id'],
        )

        return Response(data)


class MetricsActivityView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return _metrics_permissions()

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        params = _scope_params(request)

        data = get_activity(
            organization=request.active_organization,
            scope=request.effective_scope,
            project_id=params['project_id'],
            team_id=params['team_id'],
        )

        return Response(data)
