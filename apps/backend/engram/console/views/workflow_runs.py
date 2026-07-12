from __future__ import annotations

import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from engram.console.exceptions import DailyDigestAlreadyRunningError, InvalidRerunSnapshotError
from engram.console.filters import WorkflowRunFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.workflow_runs import (
    WorkflowRunDetailSerializer,
    WorkflowRunListSerializer,
)
from engram.console.services import audit_admin_action
from engram.core.models import WorkflowRun, WorkflowRunStatus, WorkflowRunType
from engram.memory.services import DAILY_DIGEST_WINDOW_DAYS, WEEKLY_DIGEST_WINDOW_DAYS
from engram.memory.tasks import distill_session, generate_daily_digest, generate_weekly_digest


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

            window_days = int(input_snapshot.get('window_days', DAILY_DIGEST_WINDOW_DAYS))

            new_run = self._create_queued_daily_digest_run(run, memory_ids, window_days, request_id)

            generate_daily_digest.delay(
                str(run.organization_id),
                str(run.project_id),
                [str(value) for value in memory_ids],
                workflow_run_id=str(new_run.id),
            )
        elif run.run_type == WorkflowRunType.WEEKLY_DIGEST:
            window_days = int(input_snapshot.get('window_days', WEEKLY_DIGEST_WINDOW_DAYS))

            new_run = WorkflowRun.objects.create(
                organization=run.organization,
                project=run.project,
                team=run.team,
                run_type=WorkflowRunType.WEEKLY_DIGEST,
                status=WorkflowRunStatus.QUEUED,
                input_snapshot={'window_days': window_days},
                request_id=request_id,
                correlation_id=request_id,
                rerun_of=run,
            )

            generate_weekly_digest.delay(
                str(run.organization_id),
                str(run.project_id),
                workflow_run_id=str(new_run.id),
            )
        elif run.run_type == WorkflowRunType.SESSION_DISTILLATION:
            if run.work_id is not None:
                raise InvalidRerunSnapshotError('work-linked session distillation runs cannot be rerun')

            raw_session_id = input_snapshot.get('session_id')

            try:
                session_id = uuid.UUID(str(raw_session_id))
            except (AttributeError, TypeError, ValueError) as error:
                raise InvalidRerunSnapshotError('invalid session_id in input_snapshot') from error

            new_run = WorkflowRun.objects.create(
                organization=run.organization,
                project=run.project,
                team=run.team,
                run_type=WorkflowRunType.SESSION_DISTILLATION,
                status=WorkflowRunStatus.QUEUED,
                input_snapshot={'session_id': str(session_id)},
                request_id=request_id,
                correlation_id=request_id,
                rerun_of=run,
            )

            distill_session.delay(
                str(session_id),
                workflow_run_id=str(new_run.id),
            )
        else:
            return Response(
                {'detail': f'rerun is not supported for run_type {run.run_type}'},
                status=400,
            )

        audit_admin_action(
            organization=run.organization,
            actor_identity=request.user_identity,
            event_type='WorkflowRunReran',
            target_type='workflow_run',
            target_id=str(run.id),
            metadata={'new_run_id': str(new_run.id)},
        )

        return Response(
            {
                'run_id': str(new_run.id),
                'status': new_run.status,
            },
            status=202,
        )

    def _create_queued_daily_digest_run(
        self,
        run: WorkflowRun,
        memory_ids: tuple[uuid.UUID, ...],
        window_days: int,
        request_id: str,
    ) -> WorkflowRun:
        try:
            with transaction.atomic():
                return WorkflowRun.objects.create(
                    organization=run.organization,
                    project=run.project,
                    team=run.team,
                    run_type=WorkflowRunType.DAILY_DIGEST,
                    status=WorkflowRunStatus.QUEUED,
                    input_snapshot={
                        'memory_ids': [str(value) for value in memory_ids],
                        'window_days': window_days,
                    },
                    request_id=request_id,
                    correlation_id=request_id,
                    rerun_of=run,
                )
        except IntegrityError as error:
            raise DailyDigestAlreadyRunningError(
                'a daily digest is already queued or running for this project',
            ) from error
