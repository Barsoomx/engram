from __future__ import annotations

import uuid

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.export import export_queryset, guard_export_stream, iter_export_memories_json
from engram.core.models import Organization, Project

_TRUE_VALUES = frozenset({'true', '1', 'yes', 'on'})


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False

    return raw.strip().lower() in _TRUE_VALUES


class MemoryExportView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        capability = 'memories:admin' if self._all_statuses() else 'memories:read'

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability(capability),
        ]

    def get(self, request: Request) -> Response | StreamingHttpResponse:
        organization = request.active_organization
        all_statuses = self._all_statuses()

        raw_project = request.query_params.get('project_id', '').strip()

        if not raw_project:
            return Response({'error': 'project_id is required'}, status=HTTP_400_BAD_REQUEST)

        try:
            project_id = uuid.UUID(raw_project)
        except ValueError:
            return Response({'error': 'project_id is not a valid uuid'}, status=HTTP_400_BAD_REQUEST)

        raw_team = request.query_params.get('team_id', '').strip()
        team_id: uuid.UUID | None = None

        if raw_team:
            try:
                team_id = uuid.UUID(raw_team)
            except ValueError:
                return Response({'error': 'team_id is not a valid uuid'}, status=HTTP_400_BAD_REQUEST)

        project = Project.objects.filter(organization=organization, id=project_id).first()

        if project is None:
            return Response(status=HTTP_404_NOT_FOUND)

        memory_count = export_queryset(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team_id,
            all_statuses=all_statuses,
        ).count()

        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='MemoryExported',
            target_type='project',
            target_id=str(project.id),
            metadata={
                'project_id': str(project.id),
                'team_id': str(team_id) if team_id is not None else None,
                'all_statuses': all_statuses,
                'memory_count': memory_count,
            },
        )

        stream = guard_export_stream(
            iter_export_memories_json(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team_id,
                all_statuses=all_statuses,
            ),
        )

        response = StreamingHttpResponse(stream, content_type='application/json')
        response['Content-Disposition'] = f'attachment; filename="{self._filename(organization, project)}"'

        return response

    def _all_statuses(self) -> bool:
        return _parse_bool(self.request.query_params.get('all_statuses'))

    def _filename(self, organization: Organization, project: Project) -> str:
        date = timezone.now().date().isoformat()

        return f'engram-memories-{organization.slug}-{project.slug}-{date}.json'
