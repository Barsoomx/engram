from __future__ import annotations

import uuid

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.imports.batch_services import CancelImportJob, CancelImportJobInput, get_import_job


class AdminImportCancelView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:admin'),
        ]

    def post(self, request: Request, import_id: uuid.UUID) -> Response:
        organization = request.active_organization
        job = get_import_job(organization, import_id)
        job = CancelImportJob().execute(
            CancelImportJobInput(
                organization=organization,
                import_id=job.id,
                actor_id=None,
            ),
        )
        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='ImportCanceled',
            target_type='import_job',
            target_id=str(job.id),
            metadata={'source_store_id': job.source_store_id},
        )

        return Response({'status': job.status, 'failure_reason': job.failure_reason})
