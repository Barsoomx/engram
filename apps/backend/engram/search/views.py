from __future__ import annotations

import uuid

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response, bearer_key
from engram.search.serializers import SearchRequestSerializer
from engram.search.services import SearchInput, SearchMemories

ACCESS_STATUS = {
    'invalid_key': status.HTTP_401_UNAUTHORIZED,
    'inactive_key': status.HTTP_403_FORBIDDEN,
    'revoked_key': status.HTTP_403_FORBIDDEN,
    'expired_key': status.HTTP_403_FORBIDDEN,
    'inactive_owner': status.HTTP_403_FORBIDDEN,
    'missing_capability': status.HTTP_403_FORBIDDEN,
    'project_scope_denied': status.HTTP_403_FORBIDDEN,
    'team_scope_denied': status.HTTP_403_FORBIDDEN,
    'missing_api_key': status.HTTP_401_UNAUTHORIZED,
}


class SearchView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = str(data.get('request_id') or f'search-{uuid.uuid4()}')
        try:
            result = SearchMemories().execute(
                SearchInput(
                    raw_key=bearer_key(request),
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    query=data.get('query', ''),
                    file_paths=tuple(data.get('file_paths', [])),
                    symbols=tuple(data.get('symbols', [])),
                    limit=data.get('limit', 5),
                    request_id=request_id,
                    correlation_id=data.get('correlation_id', ''),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        response = result.to_response()
        response['request_id'] = request_id

        return Response(response)
