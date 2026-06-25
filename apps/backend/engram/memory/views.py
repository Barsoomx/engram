from __future__ import annotations

import uuid
from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response, bearer_key
from engram.memory.serializers import MemoryFeedbackSerializer
from engram.memory.services import MemoryFeedbackError, MemoryFeedbackInput, RecordMemoryFeedback

MEMORY_FEEDBACK_STATUS = {
    'memory_not_found': status.HTTP_404_NOT_FOUND,
}


class MemoryFeedbackView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request, memory_id: uuid.UUID) -> Response:
        serializer = MemoryFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = RecordMemoryFeedback().execute(self._input(request, memory_id, data))
        except AccessDeniedError as error:
            return access_error_response(error)
        except MemoryFeedbackError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=MEMORY_FEEDBACK_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
            )

        return Response(result.to_response())

    def _input(self, request: Request, memory_id: uuid.UUID, data: dict[str, Any]) -> MemoryFeedbackInput:
        return MemoryFeedbackInput(
            raw_key=bearer_key(request),
            memory_id=memory_id,
            project_id=data['project_id'],
            team_id=data.get('team_id'),
            action=data['action'],
            reason=data['reason'],
            request_id=data['request_id'],
            correlation_id=data.get('correlation_id', ''),
        )
