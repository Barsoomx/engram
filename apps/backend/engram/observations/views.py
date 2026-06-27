from __future__ import annotations

import uuid

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response, bearer_key
from engram.observations.serializers import ObservationListQuerySerializer
from engram.observations.services import ListObservations, ObservationListInput


class ObservationListView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        serializer = ObservationListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = str(data.get('request_id') or f'observations-{uuid.uuid4()}')
        try:
            result = ListObservations().execute(
                ObservationListInput(
                    raw_key=bearer_key(request),
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    limit=data.get('limit', 20),
                    request_id=request_id,
                    correlation_id=data.get('correlation_id', ''),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        response = result.to_response()
        response['request_id'] = request_id

        return Response(response)
