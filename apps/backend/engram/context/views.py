from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.serializers import ContextRequestSerializer
from engram.context.services import BuildContextBundle, ContextBundleInput

ACCESS_STATUS = {
    'invalid_key': status.HTTP_401_UNAUTHORIZED,
    'inactive_key': status.HTTP_403_FORBIDDEN,
    'revoked_key': status.HTTP_403_FORBIDDEN,
    'expired_key': status.HTTP_403_FORBIDDEN,
    'inactive_owner': status.HTTP_403_FORBIDDEN,
    'missing_capability': status.HTTP_403_FORBIDDEN,
    'project_scope_denied': status.HTTP_403_FORBIDDEN,
    'team_scope_denied': status.HTTP_403_FORBIDDEN,
    'invalid_session': status.HTTP_401_UNAUTHORIZED,
    'organization_required': status.HTTP_400_BAD_REQUEST,
    'organization_not_found': status.HTTP_404_NOT_FOUND,
    'not_a_member': status.HTTP_403_FORBIDDEN,
    'organization_suspended': status.HTTP_403_FORBIDDEN,
}


def bearer_key(request: Request) -> str:
    header = request.META.get('HTTP_AUTHORIZATION', '')
    prefix = 'Bearer '
    if not header.startswith(prefix) or not header[len(prefix) :].strip():
        raise AccessDeniedError('missing_api_key', 'Missing bearer API key')

    return header[len(prefix) :].strip()


def access_error_response(error: AccessDeniedError) -> Response:
    response_status = ACCESS_STATUS.get(error.code, status.HTTP_401_UNAUTHORIZED)

    return Response({'code': error.code, 'detail': str(error)}, status=response_status)


class ContextView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []
    purpose = 'task'

    def post(self, request: Request) -> Response:
        serializer = ContextRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = BuildContextBundle().execute(self._input(request, data))
        except AccessDeniedError as error:
            return access_error_response(error)

        return Response(result.to_response())

    def _input(self, request: Request, data: dict[str, Any]) -> ContextBundleInput:
        return ContextBundleInput(
            raw_key=bearer_key(request),
            project_id=data['project_id'],
            team_id=data.get('team_id'),
            agent_runtime=data['agent_runtime'],
            agent_version=data.get('agent_version', ''),
            agent_external_id=data.get('agent_external_id', ''),
            session_id=data['session_id'],
            request_id=data['request_id'],
            correlation_id=data.get('correlation_id', ''),
            trace_id=data.get('trace_id', ''),
            repository_url=data.get('repository_url', ''),
            repository_root=data.get('repository_root', ''),
            branch=data.get('branch', ''),
            cwd=data.get('cwd', ''),
            query=data.get('query', ''),
            file_paths=tuple(data.get('file_paths', [])),
            symbols=tuple(data.get('symbols', [])),
            limit=data.get('limit', 5),
            token_budget=data.get('token_budget'),
            purpose=self.purpose,
        )


class SessionStartContextView(ContextView):
    purpose = 'session_start'


class TaskContextView(ContextView):
    purpose = 'task'
