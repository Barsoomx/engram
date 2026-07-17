from __future__ import annotations

import uuid
from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_412_PRECONDITION_FAILED,
    HTTP_428_PRECONDITION_REQUIRED,
)

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.memory_review import (
    ConflictResolveSerializer,
    conflict_detail_payload,
    conflict_list_item,
)
from engram.console.services import (
    MemoryReviewError,
    conflict_set_etag,
    get_conflict_candidate_or_404,
    open_conflict_candidates,
    open_conflicts_for_candidates,
    resolve_candidate_conflicts,
)

PAGE_SIZE = 50


class MemoryReviewViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    pagination_class: Any = None

    def get_permissions(self) -> list[BasePermission]:
        if self.action == 'resolve':
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('memories:admin'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:review'),
        ]

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        filters = self._list_filters(request)

        ordering = self._ordering(request)

        base = open_conflict_candidates(organization, request.effective_scope, **filters)

        candidates = list(base.order_by(*ordering)[:PAGE_SIZE])

        conflicts_by_candidate = open_conflicts_for_candidates(
            organization,
            [candidate.id for candidate in candidates],
        )

        results = [conflict_list_item(candidate, conflicts_by_candidate[candidate.id]) for candidate in candidates]

        return Response(
            {
                'count': base.count(),
                'next': None,
                'previous': None,
                'results': results,
            },
        )

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        candidate_id = self._uuid_kwarg(kwargs)

        candidate = get_conflict_candidate_or_404(organization, candidate_id, request.effective_scope)

        conflicts = open_conflicts_for_candidates(organization, [candidate.id])[candidate.id]

        etag = conflict_set_etag(candidate)

        response = Response(conflict_detail_payload(candidate, conflicts, etag), status=HTTP_200_OK)

        response['ETag'] = etag

        return response

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        actor_identity = request.user_identity

        candidate_id = self._uuid_kwarg(kwargs)

        candidate = get_conflict_candidate_or_404(organization, candidate_id, request.effective_scope)

        if_match = request.META.get('HTTP_IF_MATCH')

        if not if_match:
            return Response(
                {'code': 'precondition_required', 'detail': 'If-Match header is required'},
                status=HTTP_428_PRECONDITION_REQUIRED,
            )

        if if_match != conflict_set_etag(candidate):
            return Response(
                {'code': 'precondition_failed', 'detail': 'conflict set has changed'},
                status=HTTP_412_PRECONDITION_FAILED,
            )

        serializer = ConflictResolveSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        payload = resolve_candidate_conflicts(
            organization=organization,
            actor_identity=actor_identity,
            candidate=candidate,
            action=data['action'],
            reason=data['reason'],
            target_memory_id=data.get('target_memory_id'),
            merged_title=data.get('merged_title'),
            merged_body=data.get('merged_body'),
        )

        return Response(payload, status=HTTP_200_OK)

    def _list_filters(self, request: Request) -> dict[str, Any]:
        return {
            'project_id': self._uuid_query(request, 'project_id'),
            'team_id': self._uuid_query(request, 'team_id'),
            'opened_at__gte': self._datetime_query(request, 'opened_at__gte'),
            'search': request.query_params.get('search') or None,
        }

    def _ordering(self, request: Request) -> tuple[str, ...]:
        ordering = request.query_params.get('ordering')

        if ordering == 'opened_at':
            return ('opened_at', 'id')

        return ('-opened_at', '-id')

    def _uuid_query(self, request: Request, name: str) -> uuid.UUID | None:
        raw = request.query_params.get(name)

        if raw is None:
            return None

        try:
            return uuid.UUID(str(raw))

        except ValueError as error:
            raise MemoryReviewError(
                'invalid_filter',
                f'{name} is not a valid uuid',
                status=HTTP_400_BAD_REQUEST,
            ) from error

    def _datetime_query(self, request: Request, name: str) -> Any:
        raw = request.query_params.get(name)

        if raw is None:
            return None

        parsed = parse_datetime(raw)

        if parsed is None:
            raise MemoryReviewError(
                'invalid_filter',
                f'{name} is not a valid datetime',
                status=HTTP_400_BAD_REQUEST,
            )

        return parsed

    def _uuid_kwarg(self, kwargs: dict[str, Any]) -> uuid.UUID:
        raw = kwargs.get('pk')

        if raw is None:
            raise MemoryReviewError('id_required', 'id is required', status=HTTP_400_BAD_REQUEST)

        try:
            return uuid.UUID(str(raw))

        except ValueError as error:
            raise MemoryReviewError(
                'invalid_id',
                'id is not a valid uuid',
                status=HTTP_400_BAD_REQUEST,
            ) from error
