from __future__ import annotations

import uuid

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.context.views import bearer_key
from engram.search.serializers import SearchRequestSerializer
from engram.search.services import SearchInput, SearchMemories


class SearchView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = str(data.get('request_id') or f'search-{uuid.uuid4()}')
        result = SearchMemories().execute(
            SearchInput(
                raw_key=bearer_key(request),
                project_id=data.get('project_id'),
                team_id=data.get('team_id'),
                repository_url=str(data.get('repository_url') or ''),
                repository_root=str(data.get('repository_root') or ''),
                query=data.get('query', ''),
                file_paths=tuple(data.get('file_paths', [])),
                symbols=tuple(data.get('symbols', [])),
                limit=data.get('limit', 5),
                request_id=request_id,
                correlation_id=data.get('correlation_id', ''),
                kinds=tuple(data.get('kinds', [])),
            ),
        )

        response = result.to_response()
        response['request_id'] = request_id

        return Response(response)
