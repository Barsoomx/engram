from __future__ import annotations

import uuid
from typing import Any

import structlog
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_202_ACCEPTED, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.exceptions import DailyDigestAlreadyRunningError
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import Project, WorkflowRun, WorkflowRunStatus, WorkflowRunType
from engram.memory.tasks import generate_daily_digest, recent_approved_memory_ids

logger = structlog.get_logger(__name__)

_ACTIVE_RUN_STATUSES = (WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING)


class ProjectDigestRunView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:admin'),
        ]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        project_id: uuid.UUID = kwargs['project_id']

        project = Project.objects.filter(
            organization=organization,
            id=project_id,
        ).first()

        if project is None:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        already_running = WorkflowRun.objects.filter(
            organization=organization,
            project=project,
            run_type=WorkflowRunType.DAILY_DIGEST,
            status__in=_ACTIVE_RUN_STATUSES,
        ).exists()

        if already_running:
            raise DailyDigestAlreadyRunningError(
                'a daily digest is already queued or running for this project',
            )

        memory_ids = recent_approved_memory_ids(project)

        if not memory_ids:
            return Response(
                {'enqueued': False, 'reason': 'no_recent_memories'},
                status=HTTP_200_OK,
            )

        request_id = f'daily-digest:{project.id}'

        generate_daily_digest.delay(
            str(organization.id),
            str(project.id),
            [str(value) for value in memory_ids],
        )

        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='DailyDigestRunRequested',
            target_type='project',
            target_id=str(project.id),
            metadata={
                'memory_count': len(memory_ids),
                'request_id': request_id,
            },
        )

        logger.info(
            'daily_digest_run_requested',
            organization_id=str(organization.id),
            project_id=str(project.id),
            memory_count=len(memory_ids),
        )

        return Response(
            {
                'enqueued': True,
                'workflow': {
                    'run_type': WorkflowRunType.DAILY_DIGEST.value,
                    'project_id': str(project.id),
                    'request_id': request_id,
                },
            },
            status=HTTP_202_ACCEPTED,
        )
