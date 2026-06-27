from __future__ import annotations

import uuid
from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError, ResolveApiKeyScope
from engram.context.views import access_error_response, bearer_key
from engram.core.models import MemoryLink
from engram.core.redaction import redact_value
from engram.memory.serializers import (
    MemoryFeedbackSerializer,
    MemoryLinkQuerySerializer,
    MemoryLinkSerializer,
    MemoryVersionSerializer,
)
from engram.memory.services import (
    MemoryFeedbackError,
    MemoryFeedbackInput,
    MemoryLinkInput,
    MemoryVersionError,
    RecordMemoryFeedback,
    RecordMemoryLink,
    UpdateMemoryBody,
    UpdateMemoryBodyInput,
)

MEMORY_FEEDBACK_STATUS = {
    'memory_not_found': status.HTTP_404_NOT_FOUND,
}
MEMORY_VERSION_STATUS = {
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


class MemoryVersionView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request, memory_id: uuid.UUID) -> Response:
        serializer = MemoryVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = UpdateMemoryBody().execute(
                UpdateMemoryBodyInput(
                    raw_key=bearer_key(request),
                    memory_id=memory_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    body=data['body'],
                    reason=data.get('reason', ''),
                    request_id=data['request_id'],
                    correlation_id=data.get('correlation_id', ''),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except MemoryVersionError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=MEMORY_VERSION_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
            )

        return Response(result.to_response())


class MemoryLinksView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request, memory_id: uuid.UUID) -> Response:
        serializer = MemoryLinkQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = ResolveApiKeyScope().execute(
                raw_key=bearer_key(request),
                required_capability='memories:read',
                requested_project_id=data['project_id'],
                requested_team_id=data.get('team_id'),
                request_id=f'links-{uuid.uuid4()}',
                target_type='memory_link',
                target_id=str(memory_id),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        links = MemoryLink.objects.filter(
            organization_id=scope.organization_id,
            project_id=data['project_id'],
            memory_id=memory_id,
        ).order_by('link_type', 'target')

        return Response({'items': [self._link_response(link) for link in links]})

    def post(self, request: Request, memory_id: uuid.UUID) -> Response:
        serializer = MemoryLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = RecordMemoryLink().execute(
                MemoryLinkInput(
                    raw_key=bearer_key(request),
                    memory_id=memory_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    link_type=data['link_type'],
                    target=data['target'],
                    label=data.get('label', ''),
                    request_id=data['request_id'],
                    correlation_id=data.get('correlation_id', ''),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except MemoryVersionError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=status.HTTP_404_NOT_FOUND if error.code == 'memory_not_found' else status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            result.to_response(),
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    def _link_response(self, link: MemoryLink) -> dict[str, object]:
        return {
            'link_id': str(link.id),
            'link_type': link.link_type,
            'target': str(redact_value(link.target).value),
            'label': str(redact_value(link.label).value),
            'created_at': link.created_at.isoformat() if link.created_at else None,
        }
