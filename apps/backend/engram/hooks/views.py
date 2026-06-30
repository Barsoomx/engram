from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.services import AccessDeniedError
from engram.hooks.serializers import HookDryRunSerializer, HookEventSerializer
from engram.hooks.services import HookDryRunInput, HookEventInput, IngestHookEvent, VerifyHookDryRun

ACCESS_STATUS = {
    'invalid_key': status.HTTP_401_UNAUTHORIZED,
    'inactive_key': status.HTTP_403_FORBIDDEN,
    'revoked_key': status.HTTP_403_FORBIDDEN,
    'expired_key': status.HTTP_403_FORBIDDEN,
    'inactive_owner': status.HTTP_403_FORBIDDEN,
    'missing_capability': status.HTTP_403_FORBIDDEN,
    'project_scope_denied': status.HTTP_403_FORBIDDEN,
    'team_scope_denied': status.HTTP_403_FORBIDDEN,
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


class HookDryRunView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = HookDryRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = VerifyHookDryRun().execute(
                HookDryRunInput(
                    raw_key=bearer_key(request),
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    agent_runtime=data['agent_runtime'],
                    agent_version=data.get('agent_version', ''),
                    request_id=data.get('request_id', ''),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        scope = result.scope

        return Response(
            {
                'status': 'ok',
                'request_id': result.request_id,
                'resolved_actor': {'type': scope.actor_type, 'id': scope.actor_id},
                'scope': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                    'capabilities': list(scope.capabilities),
                },
                'server': {'health': 'ok'},
            },
        )


class HookIngestView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []
    expected_event_type = ''

    def post(self, request: Request) -> Response:
        serializer = HookEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if self.expected_event_type and data['event_type'] != self.expected_event_type:
            return Response(
                {'event_type': [f'Expected {self.expected_event_type}.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = IngestHookEvent().execute(self._input(request, data))
        except AccessDeniedError as error:
            return access_error_response(error)

        return Response(
            {
                'status': 'accepted',
                'duplicate': result.duplicate,
                'request_id': result.request_id,
                'raw_event_id': str(result.raw_event.id),
                'observation_id': str(result.observation.id),
                'agent_session_id': str(result.session.id),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def _input(self, request: Request, data: dict[str, Any]) -> HookEventInput:
        return HookEventInput(
            raw_key=bearer_key(request),
            project_id=data['project_id'],
            team_id=data.get('team_id'),
            agent_runtime=data['agent_runtime'],
            agent_version=data.get('agent_version', ''),
            agent_external_id=data.get('agent_external_id', ''),
            session_id=data['session_id'],
            event_id=data['event_id'],
            idempotency_key=data['idempotency_key'],
            event_type=data['event_type'],
            payload_schema_version=data['payload_schema_version'],
            sequence_number=data.get('sequence_number'),
            occurred_at=data.get('occurred_at'),
            content_hash=data['content_hash'],
            request_id=data.get('request_id', ''),
            correlation_id=data.get('correlation_id', ''),
            trace_id=data.get('trace_id', ''),
            repository_url=data.get('repository_url', ''),
            repository_root=data.get('repository_root', ''),
            branch=data.get('branch', ''),
            cwd=data.get('cwd', ''),
            payload=data['payload'],
            observation=data.get('observation', {}),
        )


class PostToolUseView(HookIngestView):
    expected_event_type = 'post_tool_use'


class SessionStartHookView(HookIngestView):
    expected_event_type = 'session_start'


class ErrorHookView(HookIngestView):
    expected_event_type = 'error'


class DecisionHookView(HookIngestView):
    expected_event_type = 'decision'


class SessionEndView(HookIngestView):
    expected_event_type = 'session_end'
