from __future__ import annotations

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.core.repository import resolve_project_for_scope
from engram.memory.memory_propose_service import (
    ProposeMemory,
    ProposeMemoryError,
    ProposeMemoryInput,
)
from engram.memory.serializers import MemoryProposeSerializer

PROPOSE_STATUS = {
    'empty_content': status.HTTP_422_UNPROCESSABLE_ENTITY,
    'content_too_long': status.HTTP_422_UNPROCESSABLE_ENTITY,
    'team_not_in_project': status.HTTP_422_UNPROCESSABLE_ENTITY,
}


class MemoryProposeView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = MemoryProposeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = resolve_request_scope(
                request,
                required_capability='memories:propose',
                project_id=data.get('project_id'),
                team_id=data.get('team_id'),
                target_type='memory_candidate',
                target_id='',
                request_id=data['request_id'],
            )
            project = resolve_project_for_scope(
                scope=scope,
                project_id=data.get('project_id'),
                repository_url=data.get('repository_url', ''),
                request_id=data['request_id'],
                correlation_id=data.get('correlation_id', ''),
            )
            result = ProposeMemory().execute(
                ProposeMemoryInput(
                    scope=scope,
                    project=project,
                    team_id=data.get('team_id'),
                    title=data['title'],
                    body=data['body'],
                    kind=data.get('kind', ''),
                    request_id=data['request_id'],
                    correlation_id=data.get('correlation_id', ''),
                )
            )
        except ProposeMemoryError as error:
            return Response(
                {'code': error.code, 'detail': error.detail or str(error)},
                status=PROPOSE_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
            )

        return Response(
            {
                'candidate_id': str(result.candidate_id),
                'status': result.status,
                'decision_work_queued': result.decision_work_queued,
                'request_id': data['request_id'],
            },
            status=status.HTTP_202_ACCEPTED,
        )
