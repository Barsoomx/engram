from __future__ import annotations

import uuid

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.access.services import AccessDeniedError
from engram.context.views import access_error_response
from engram.observations.serializers import ObservationDetailQuerySerializer, ObservationListQuerySerializer
from engram.observations.services import (
    GetObservation,
    ListObservations,
    ObservationDetailInput,
    ObservationListInput,
    ObservationNotFoundError,
    observation_response,
)


class ObservationListView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        serializer = ObservationListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = str(data.get('request_id') or f'observations-{uuid.uuid4()}')
        try:
            scope = resolve_request_scope(
                request,
                required_capability='observations:read',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='observation_list',
                target_id='list',
                request_id=request_id,
            )
            result = ListObservations().execute(
                ObservationListInput(
                    scope=scope,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    limit=data.get('limit', 20),
                    offset=data.get('offset', 0),
                    observation_type=data.get('observation_type') or None,
                    session_id=data.get('session_id'),
                    since=data.get('since'),
                    until=data.get('until'),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        response = result.to_response()
        response['request_id'] = request_id

        return Response(response)


class ObservationDetailView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = []

    def get(self, request: Request, observation_id: uuid.UUID) -> Response:
        serializer = ObservationDetailQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = f'observations-detail-{uuid.uuid4()}'
        try:
            scope = resolve_request_scope(
                request,
                required_capability='observations:read',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='observation',
                target_id=str(observation_id),
                request_id=request_id,
            )
            observation = GetObservation().execute(
                ObservationDetailInput(
                    scope=scope,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    observation_id=observation_id,
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ObservationNotFoundError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=status.HTTP_404_NOT_FOUND,
            )

        response_body = observation_response(observation)
        response_body['request_id'] = request_id

        return Response(response_body)
