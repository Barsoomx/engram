from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_202_ACCEPTED, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.exceptions import DailyDigestAlreadyRunningError
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import (
    Organization,
    Project,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.digest_work import freeze_daily_digest_input
from engram.memory.services import DAILY_DIGEST_WINDOW_DAYS
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    create_work,
    resolve_work_no_input,
)

logger = structlog.get_logger(__name__)

_ACTIVE_RUN_STATUSES = (WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING)


def _daily_max_sources() -> int:
    return int(os.environ.get('ENGRAM_DIGEST_MAX_SOURCES', '200'))


class ProjectDigestRunView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:admin'),
        ]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization
        scope = request.effective_scope

        project_id: uuid.UUID = kwargs['project_id']

        if project_id not in scope.project_ids:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        project = Project.objects.filter(organization=organization, id=project_id).first()

        if project is None:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        now = datetime.now(UTC)
        day = now.date()
        window_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
        window_start = window_end - timedelta(days=DAILY_DIGEST_WINDOW_DAYS)
        schedule_key = f'daily:{project.id}:{day.isoformat()}'

        with transaction.atomic():
            if _has_active_daily_digest_run(organization, project):
                raise DailyDigestAlreadyRunningError(
                    'a daily digest is already queued or running for this project',
                )

            snapshot = freeze_daily_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
                max_sources=_daily_max_sources(),
            )

            return self._enqueue(request, organization, project, schedule_key, snapshot)

    def _enqueue(
        self,
        request: Request,
        organization: Organization,
        project: Project,
        schedule_key: str,
        snapshot: dict[str, object],
    ) -> Response:
        data = CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type=WorkflowWorkType.DAILY_DIGEST,
            subject_type=WorkflowSubjectType.PROJECT,
            subject_id=project.id,
            input_snapshot=snapshot,
            occurrence_key=schedule_key,
        )

        work, _created = create_work(data)

        sources = snapshot.get('sources') or []

        if not sources:
            if work.disposition == WorkflowWorkDisposition.REQUIRED:
                resolve_work_no_input(
                    work.id,
                    organization_id=organization.id,
                    project_id=project.id,
                )

            return Response(
                {'enqueued': False, 'reason': 'no_recent_memories'},
                status=HTTP_200_OK,
            )

        if work.disposition != WorkflowWorkDisposition.REQUIRED:
            reason = 'already_built' if work.disposition == WorkflowWorkDisposition.COMPLETE else 'no_recent_memories'

            return Response(
                {'enqueued': False, 'reason': reason},
                status=HTTP_200_OK,
            )

        request_id = f'daily-digest:{project.id}:{uuid.uuid4().hex[:8]}'

        try:
            run = queue_work_attempt(
                work_id=work.id,
                now=datetime.now(UTC),
                origin=WorkflowRunOrigin.MANUAL,
                request_id=request_id,
                correlation_id=request_id,
            )
        except IntegrityError as error:
            raise DailyDigestAlreadyRunningError(
                'a daily digest is already queued or running for this project',
            ) from error

        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='DailyDigestRunRequested',
            target_type='project',
            target_id=str(project.id),
            metadata={
                'memory_count': len(sources),
                'request_id': request_id,
                'workflow_work_id': str(work.id),
                'workflow_run_id': str(run.id),
            },
        )

        logger.info(
            'daily_digest_run_requested',
            organization_id=str(organization.id),
            project_id=str(project.id),
            workflow_work_id=str(work.id),
            workflow_run_id=str(run.id),
            memory_count=len(sources),
        )

        return Response(
            {
                'enqueued': True,
                'workflow': {
                    'run_type': WorkflowRunType.DAILY_DIGEST.value,
                    'project_id': str(project.id),
                    'work_id': str(work.id),
                    'run_id': str(run.id),
                    'request_id': request_id,
                },
            },
            status=HTTP_202_ACCEPTED,
        )


def _has_active_daily_digest_run(organization: Organization, project: Project) -> bool:
    return WorkflowRun.objects.filter(
        organization=organization,
        project=project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status__in=_ACTIVE_RUN_STATUSES,
    ).exists()
