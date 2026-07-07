from __future__ import annotations

import uuid

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.imports.batch_services import CancelImportJob, CancelImportJobInput, get_import_job
from engram.imports.views.support import authorize_job_project, resolve_import_organization


class CancelImportView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request, import_id: uuid.UUID) -> Response:
        scope = resolve_request_scope(request, required_capability='memories:admin', project_id=None)
        organization = resolve_import_organization(scope)
        job = get_import_job(organization, import_id)
        authorize_job_project(scope, job)
        job = CancelImportJob().execute(
            CancelImportJobInput(
                organization=organization,
                import_id=job.id,
                actor_id=scope.api_key_id,
            ),
        )

        return Response({'status': job.status, 'failure_reason': job.failure_reason})
