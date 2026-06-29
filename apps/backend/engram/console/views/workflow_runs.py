from __future__ import annotations

import uuid
from typing import Any

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.workflow_runs import (
    WorkflowRunDetailSerializer,
    WorkflowRunListSerializer,
)
from engram.console.services import audit_admin_action
from engram.core.models import WorkflowRun, WorkflowRunStatus, WorkflowRunType
from engram.memory.services import DAILY_DIGEST_WINDOW_DAYS, run_daily_digest_with_tracking


class WorkflowRunViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        if self.action == 'rerun':
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('memories:admin'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get_queryset(self) -> Any:
        queryset = WorkflowRun.objects.filter(
            organization=self.request.active_organization,
        ).select_related('project', 'team', 'result_memory')

        if self.action != 'list':
            return queryset

        query = self.request.query_params

        run_type = query.get('run_type')

        if run_type:
            queryset = queryset.filter(run_type=run_type)

        status_value = query.get('status')

        if status_value:
            queryset = queryset.filter(status=status_value)

        project_id = query.get('project_id')

        if project_id:
            queryset = queryset.filter(project_id=project_id)

        team_id = query.get('team_id')

        if team_id:
            queryset = queryset.filter(team_id=team_id)

        escalation = query.get('escalation')

        if escalation is not None and escalation != '':
            queryset = queryset.filter(escalation=str(escalation).lower() in {'true', '1'})

        created_at_gte = query.get('created_at__gte')

        if created_at_gte:
            queryset = queryset.filter(created_at__gte=created_at_gte)

        created_at_lte = query.get('created_at__lte')

        if created_at_lte:
            queryset = queryset.filter(created_at__lte=created_at_lte)

        return queryset

    def get_serializer_class(self) -> type:
        if self.action == 'retrieve':
            return WorkflowRunDetailSerializer

        return WorkflowRunListSerializer

    @action(detail=True, methods=['post'])
    def rerun(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        run = self.get_object()

        input_snapshot = run.input_snapshot or {}

        raw_memory_ids = input_snapshot.get('memory_ids') or []

        try:
            memory_ids = tuple(uuid.UUID(str(value)) for value in raw_memory_ids)
        except (AttributeError, TypeError, ValueError):
            return Response(
                {'detail': 'invalid memory_ids in input_snapshot'},
                status=400,
            )

        window_days = input_snapshot.get('window_days', DAILY_DIGEST_WINDOW_DAYS)

        request_id = f'workflow-rerun:{run.id}'

        result = run_daily_digest_with_tracking(
            organization_id=run.organization_id,
            project_id=run.project_id,
            memory_ids=memory_ids,
            window_days=int(window_days),
            request_id=request_id,
        )

        new_run = WorkflowRun.objects.filter(
            organization_id=run.organization_id,
            request_id=request_id,
        ).order_by('-created_at').first()

        if new_run is not None:
            new_run.rerun_of = run

            new_run.save(update_fields=['rerun_of', 'updated_at'])

        audit_admin_action(
            organization=run.organization,
            actor_identity=request.user_identity,
            event_type='WorkflowRunReran',
            target_type='workflow_run',
            target_id=str(run.id),
            metadata={
                'new_run_id': str(new_run.id) if new_run else None,
                'result_memory_id': str(result.memory.id),
            },
        )

        return Response(
            {
                'run_id': str(new_run.id) if new_run else None,
                'result_memory_id': str(result.memory.id),
            },
            status=200,
        )
