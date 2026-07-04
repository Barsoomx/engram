from __future__ import annotations

import uuid
from typing import Any

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.search_debug_service import ReplaySearchDebug
from engram.console.serializers.search_debug import SearchDebugRequestSerializer
from engram.core.models import Project


class SearchDebugView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = SearchDebugRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        organization = request.active_organization
        project_id: uuid.UUID = data['project_id']

        project = Project.objects.filter(organization=organization, id=project_id).first()
        if project is None:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        scope = request.effective_scope
        team_id: uuid.UUID | None = data.get('team_id')
        file_paths: tuple[str, ...] = tuple(data.get('file_paths', []))
        symbols: tuple[str, ...] = tuple(data.get('symbols', []))

        result = ReplaySearchDebug().execute(
            organization=organization,
            project=project,
            scope=scope,
            query=data['query'],
            team_id=team_id,
            file_paths=file_paths,
            symbols=symbols,
        )

        return Response(
            {
                'scope_filters': result.scope_filters,
                'candidate_universe_count': result.candidate_universe_count,
                'exact_matches': [
                    {
                        'memory_id': str(m.memory_id),
                        'title': m.title,
                        'score': m.score,
                        'matched_on': m.matched_on,
                        'kind': m.kind,
                        'confidence': m.confidence,
                    }
                    for m in result.exact_matches
                ],
                'semantic_enabled': result.semantic_enabled,
                'semantic_candidates': [
                    {
                        'memory_id': str(c.memory_id),
                        'title': c.title,
                        'score': c.score,
                        'kind': c.kind,
                        'confidence': c.confidence,
                    }
                    for c in result.semantic_candidates
                ],
                'lexical_enabled': result.lexical_enabled,
                'lexical_candidates': [
                    {
                        'memory_id': str(m.memory_id),
                        'title': m.title,
                        'score': m.score,
                        'matched_on': m.matched_on,
                        'kind': m.kind,
                        'confidence': m.confidence,
                    }
                    for m in result.lexical_candidates
                ],
                'packed_context': [
                    {
                        'memory_id': str(p.memory_id),
                        'title': p.title,
                        'kind': p.kind,
                        'confidence': p.confidence,
                    }
                    for p in result.packed_context
                ],
                'excluded': [
                    {
                        'memory_id': str(e.memory_id),
                        'title': e.title,
                        'reason': e.reason,
                    }
                    for e in result.excluded
                ],
            },
            status=HTTP_200_OK,
        )
