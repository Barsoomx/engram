from __future__ import annotations

from django.core.exceptions import FieldDoesNotExist
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox, CeleryOutboxDeadLetter
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.core.models import Organization, RetrievalDocument, WorkflowRun, WorkflowRunStatus


class OpsOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:admin'),
        ]

    def get(self, request: Request) -> Response:
        outbox_pending = CeleryOutbox.objects.filter(updated_at__isnull=True)
        outbox_count = outbox_pending.count()
        oldest_outbox = outbox_pending.order_by('created_at').first()
        outbox_oldest_age_seconds = None
        if oldest_outbox is not None:
            outbox_oldest_age_seconds = int((timezone.now() - oldest_outbox.created_at).total_seconds())

        dead_letter_count = CeleryOutboxDeadLetter.objects.count()

        failed_workflow_runs = WorkflowRun.objects.filter(
            status=WorkflowRunStatus.FAILED,
            organization=request.active_organization,
        ).count()

        pending_embedding_count = _pending_embedding_count(request.active_organization)

        return Response(
            {
                'outbox_backlog_count': outbox_count,
                'outbox_oldest_age_seconds': outbox_oldest_age_seconds,
                'dead_letter_count': dead_letter_count,
                'failed_workflow_runs': failed_workflow_runs,
                'pending_embedding_count': pending_embedding_count,
            }
        )


def _pending_embedding_count(organization: Organization) -> int:
    try:
        RetrievalDocument._meta.get_field('embedding_pgvector')
    except FieldDoesNotExist:
        return 0

    return RetrievalDocument.objects.filter(
        embedding_pgvector__isnull=True,
        organization=organization,
    ).count()
