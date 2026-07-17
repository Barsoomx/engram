from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from engram.console.exceptions import (
    DailyDigestAlreadyRunningError,
    InvalidRerunSnapshotError,
    LegacyWorkUnlinkedError,
    WorkflowRunNotTerminalError,
)
from engram.console.filters import WorkflowRunFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.workflow_runs import (
    WorkflowRunDetailSerializer,
    WorkflowRunListSerializer,
)
from engram.console.services import audit_admin_action
from engram.core.models import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
)
from engram.memory.tasks import (
    dispatch_work_task,
    distill_session,  # noqa: F401
    generate_daily_digest_work_v1,
    generate_weekly_digest_work_v1,
)

_DIGEST_RUN_TYPES = (WorkflowRunType.DAILY_DIGEST, WorkflowRunType.WEEKLY_DIGEST)
_TERMINAL_RUN_STATUSES = (WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED)
_DIGEST_WORK_TASKS = {
    WorkflowRunType.DAILY_DIGEST: generate_daily_digest_work_v1,
    WorkflowRunType.WEEKLY_DIGEST: generate_weekly_digest_work_v1,
}


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
        scope = self.request.effective_scope

        return (
            WorkflowRun.objects.filter(organization=self.request.active_organization)
            .select_related('project', 'team', 'result_memory')
            .filter(project_id__in=scope.project_ids)
            .filter(Q(team_id__isnull=True) | Q(team_id__in=scope.team_ids))
        )

    def get_serializer_class(self) -> type:
        if self.action == 'retrieve':
            return WorkflowRunDetailSerializer

        return WorkflowRunListSerializer

    @action(detail=True, methods=['post'])
    def rerun(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        run = self.get_object()

        if run.run_type in _DIGEST_RUN_TYPES:
            return self._rerun_digest(request, run)

        if run.run_type == WorkflowRunType.SESSION_DISTILLATION:
            if run.work_id is not None:
                raise InvalidRerunSnapshotError('work-linked session distillation runs cannot be rerun')

            raise LegacyWorkUnlinkedError('legacy run is not linked to workflow work')

        return Response(
            {'detail': f'rerun is not supported for run_type {run.run_type}'},
            status=400,
        )

    def _rerun_digest(self, request: Request, run: WorkflowRun) -> Response:
        if run.work_id is None:
            raise LegacyWorkUnlinkedError('legacy run is not linked to workflow work')

        if run.status not in _TERMINAL_RUN_STATUSES:
            raise WorkflowRunNotTerminalError('workflow run must reach a terminal status before rerun')

        request_id = f'workflow-rerun:{run.id}'
        signal_task = _DIGEST_WORK_TASKS[run.run_type]

        try:
            with transaction.atomic():
                work = WorkflowWork.objects.select_for_update().get(
                    id=run.work_id,
                    organization=run.organization,
                    project=run.project,
                )

                new_run = WorkflowRun.objects.create(
                    organization=run.organization,
                    project=run.project,
                    team=run.team,
                    work=work,
                    run_type=run.run_type,
                    status=WorkflowRunStatus.QUEUED,
                    input_snapshot=work.input_snapshot,
                    request_id=request_id,
                    correlation_id=request_id,
                    rerun_of=run,
                )

                dispatch_work_task(signal_task, work.id, new_run.id)

                audit_admin_action(
                    organization=run.organization,
                    actor_identity=request.user_identity,
                    event_type='WorkflowRunReran',
                    target_type='workflow_run',
                    target_id=str(run.id),
                    metadata={'new_run_id': str(new_run.id)},
                )
        except IntegrityError as error:
            if run.run_type == WorkflowRunType.DAILY_DIGEST:
                raise DailyDigestAlreadyRunningError(
                    'a daily digest is already queued or running for this project',
                ) from error

            raise

        return Response(
            {
                'run_id': str(new_run.id),
                'status': new_run.status,
            },
            status=202,
        )
