from __future__ import annotations

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.core.repository import resolve_project_for_scope
from engram.imports.batch_services import CreateImportJob, CreateImportJobInput
from engram.imports.serializers import CreateImportJobSerializer
from engram.imports.views.support import resolve_import_organization, resolve_team_for_scope


class CreateImportView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = CreateImportJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        project_id = data['project_id']
        scope = resolve_request_scope(
            request,
            required_capability='memories:admin',
            project_id=project_id,
        )
        project = resolve_project_for_scope(scope=scope, project_id=project_id, repository_url='')
        organization = resolve_import_organization(scope)
        team = resolve_team_for_scope(scope, organization)
        job = CreateImportJob().execute(
            CreateImportJobInput(
                organization=organization,
                project=project,
                team=team,
                source_store_id=data['source_store_id'],
                manifest=data['manifest'],
                api_key_id=scope.api_key_id,
                identity_id=scope.identity_id,
            ),
        )

        return Response({'import_id': str(job.id), 'status': job.status}, status=status.HTTP_201_CREATED)
