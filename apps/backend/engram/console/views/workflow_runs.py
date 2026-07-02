from __future__ import annotations

import uuid
from typing import Any

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from engram.console.exceptions import InvalidRerunSnapshotError
from engram.console.filters import WorkflowRunFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.workflow_runs import (
    WorkflowRunDetailSerializer,
    WorkflowRunListSerializer,
)
from engram.console.services import audit_admin_action
from engram.core.models import WorkflowRun, WorkflowRunType
from engram.memory.distillation import run_session_distillation_with_tracking
from engram.memory.services import (
    DAILY_DIGEST_WINDOW_DAYS,
    WEEKLY_DIGEST_WINDOW_DAYS,
    run_daily_digest_with_tracking,
    run_weekly_digest_with_tracking,
)


class WorkflowRunViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    filter_backends = (DjangoFilterBackend,)
    filterset_class = WorkflowRunFilterSet

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
        return WorkflowRun.objects.filter(
            organization=self.request.active_organization,
        ).select_related('project', 'team', 'result_memory')

    def get_serializer_class(self) -> type:
        if self.action == 'retrieve':
            return WorkflowRunDetailSerializer

        return WorkflowRunListSerializer

    @action(detail=True, methods=['post'])
    def rerun(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        run = self.get_object()

        input_snapshot = run.input_snapshot or {}

        request_id = f'workflow-rerun:{run.id}'

        if run.run_type == WorkflowRunType.DAILY_DIGEST:
            raw_memory_ids = input_snapshot.get('memory_ids') or []

            try:
                memory_ids = tuple(uuid.UUID(str(value)) for value in raw_memory_ids)
            except (AttributeError, TypeError, ValueError) as error:
                raise InvalidRerunSnapshotError('invalid memory_ids in input_snapshot') from error

            window_days = input_snapshot.get('window_days', DAILY_DIGEST_WINDOW_DAYS)

            run_daily_digest_with_tracking(
                organization_id=run.organization_id,
                project_id=run.project_id,
                memory_ids=memory_ids,
                window_days=int(window_days),
                request_id=request_id,
            )
        elif run.run_type == WorkflowRunType.WEEKLY_DIGEST:
            window_days = input_snapshot.get('window_days', WEEKLY_DIGEST_WINDOW_DAYS)

            run_weekly_digest_with_tracking(
                organization_id=run.organization_id,
                project_id=run.project_id,
                window_days=int(window_days),
                request_id=request_id,
            )
        elif run.run_type == WorkflowRunType.SESSION_DISTILLATION:
            raw_session_id = input_snapshot.get('session_id')

            try:
                session_id = uuid.UUID(str(raw_session_id))
            except (AttributeError, TypeError, ValueError) as error:
                raise InvalidRerunSnapshotError('invalid session_id in input_snapshot') from error

            run_session_distillation_with_tracking(
                session_id=session_id,
                request_id=request_id,
            )
        else:
            return Response(
                {'detail': f'rerun is not supported for run_type {run.run_type}'},
                status=400,
            )

        new_run = (
            WorkflowRun.objects.filter(
                organization_id=run.organization_id,
                request_id=request_id,
            )
            .order_by('-created_at')
            .first()
        )

        if new_run is not None:
            new_run.rerun_of = run

            new_run.save(update_fields=['rerun_of', 'updated_at'])

        result_memory_id = str(new_run.result_memory_id) if new_run and new_run.result_memory_id else None

        audit_admin_action(
            organization=run.organization,
            actor_identity=request.user_identity,
            event_type='WorkflowRunReran',
            target_type='workflow_run',
            target_id=str(run.id),
            metadata={
                'new_run_id': str(new_run.id) if new_run else None,
                'result_memory_id': result_memory_id,
            },
        )

        return Response(
            {
                'run_id': str(new_run.id) if new_run else None,
                'result_memory_id': result_memory_id,
            },
            status=200,
        )
