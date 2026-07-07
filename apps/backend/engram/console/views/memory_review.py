from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Q
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
)

from engram.console.filters import MemoryReviewCandidateFilterSet, MemoryReviewMemoryFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.memory_review import (
    BulkArchiveResultSerializer,
    BulkArchiveSerializer,
    MemoryReviewActionSerializer,
    queue_item_payload,
    version_slice,
)
from engram.console.services import (
    MemoryReviewError,
    bulk_archive_memories,
    get_review_candidate_or_404,
    get_review_memory_or_404,
)
from engram.console.usecases.review_action import ReviewActionInput, ReviewActionUseCase
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
)

REVIEW_MEMORY_STATUSES = (
    MemoryStatus.CONFLICT,
    MemoryStatus.REFUTED,
)

REVIEW_MEMORY_CONFIDENCE_THRESHOLD = '0.300'

PAGE_SIZE = 50


class MemoryReviewViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    pagination_class: Any = None

    def get_permissions(self) -> list[BasePermission]:
        if self.action in {'review_action', 'bulk_archive'}:
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
        items = list(self._reviewable_items(request))

        page = self._paginate(request, items)

        results = [queue_item_payload(item) for item in page['items']]

        return Response(
            {
                'count': page['count'],
                'next': page['next'],
                'previous': page['previous'],
                'results': results,
            },
        )

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        item_id = self._uuid_kwarg(kwargs)

        try:
            item = get_review_candidate_or_404(organization, item_id)

        except MemoryReviewError:
            item = get_review_memory_or_404(organization, item_id)

        return Response(queue_item_payload(item), status=HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='diff')
    def diff(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        memory_id = self._uuid_kwarg(kwargs)

        from_version = self._int_query(request, 'from_version')

        to_version = self._int_query(request, 'to_version')

        if from_version is None or to_version is None:
            return Response(
                {'detail': 'from_version and to_version are required'},
                status=HTTP_400_BAD_REQUEST,
            )

        memory = Memory.objects.filter(
            organization=organization,
            id=memory_id,
        ).first()

        if memory is None:
            return Response(status=HTTP_404_NOT_FOUND)

        from_slice = self._version_or_404(memory, from_version)

        to_slice = self._version_or_404(memory, to_version)

        return Response(
            {
                'from': version_slice(from_slice),
                'to': version_slice(to_slice),
            },
        )

    @action(detail=True, methods=['post'], url_path='action')
    def review_action(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        actor_identity = request.user_identity

        item_id = self._uuid_kwarg(kwargs)

        serializer = MemoryReviewActionSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        output = ReviewActionUseCase(user=request.user, transaction=transaction.atomic()).execute(
            ReviewActionInput(
                organization=organization,
                actor_identity=actor_identity,
                item_id=item_id,
                action_name=data['action'],
                reason=data['reason'],
                body=data.get('body'),
                target_memory_id=data.get('target_memory_id'),
            ),
        )

        return Response(output.result, status=HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='bulk-archive')
    def bulk_archive(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        actor_identity = request.user_identity

        serializer = BulkArchiveSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        ids = [uuid.UUID(str(raw)) for raw in data['ids']] if 'ids' in data else None

        confidence_lte = str(data['confidence__lte']) if 'confidence__lte' in data else None

        archived_ids = bulk_archive_memories(
            organization=organization,
            actor_identity=actor_identity,
            reason=data['reason'],
            ids=ids,
            confidence_lte=confidence_lte,
        )

        payload = {
            'archived_count': len(archived_ids),
            'archived_ids': archived_ids,
        }

        result = BulkArchiveResultSerializer(payload)

        return Response(result.data, status=HTTP_200_OK)

    def _reviewable_items(self, request: Request) -> Any:
        organization = request.active_organization

        candidates_qs = self._filtered_candidates(request, organization)

        memories_qs = self._filtered_memories(request, organization)

        candidates = list(candidates_qs)

        memories = list(memories_qs.prefetch_related('links', 'versions__source_observation'))

        combined = sorted(
            candidates + memories,
            key=lambda item: item.created_at,
            reverse=True,
        )

        return combined

    def _filtered_candidates(self, request: Request, organization: Any) -> Any:
        queryset = MemoryCandidate.objects.filter(
            organization=organization,
            status=CandidateStatus.PROPOSED,
        ).select_related('source_observation', 'project', 'team')

        queryset = self._apply_common_filters(request, queryset, candidate=True)

        return queryset

    def _filtered_memories(self, request: Request, organization: Any) -> Any:
        queryset = (
            Memory.objects.filter(organization=organization)
            .filter(
                Q(status__in=REVIEW_MEMORY_STATUSES)
                | Q(status=MemoryStatus.APPROVED, confidence__lte=REVIEW_MEMORY_CONFIDENCE_THRESHOLD)
                | Q(status=MemoryStatus.APPROVED, refuted=True),
            )
            .select_related('project', 'team')
        )

        queryset = self._apply_common_filters(request, queryset, candidate=False)

        status_param = request.query_params.get('status')

        if status_param in REVIEW_MEMORY_STATUSES:
            queryset = queryset.filter(status=status_param)

        elif status_param == 'proposed':
            queryset = queryset.none()

        return queryset

    def _apply_common_filters(
        self,
        request: Request,
        queryset: Any,
        *,
        candidate: bool,
    ) -> Any:
        filterset_class = MemoryReviewCandidateFilterSet if candidate else MemoryReviewMemoryFilterSet

        filterset = filterset_class(data=request.query_params, queryset=queryset)

        if not filterset.is_valid():
            raise MemoryReviewError('invalid_filter', 'one or more filter parameters are invalid')

        queryset = filterset.qs

        if candidate:
            status_param = request.query_params.get('status')

            if status_param == 'proposed':
                queryset = queryset.filter(status=CandidateStatus.PROPOSED)

            elif status_param in REVIEW_MEMORY_STATUSES:
                queryset = queryset.none()

        return queryset

    def _paginate(self, request: Request, items: list) -> dict[str, Any]:
        total = len(items)

        try:
            page_number = max(int(request.query_params.get('page', '1')), 1)

        except ValueError:
            page_number = 1

        start = (page_number - 1) * PAGE_SIZE

        end = start + PAGE_SIZE

        page_items = items[start:end]

        base_url = request.build_absolute_uri(request.path)

        query = request.query_params.copy()

        next_url = None

        if end < total:
            query['page'] = str(page_number + 1)

            next_url = f'{base_url}?{query.urlencode()}'

        previous_url = None

        if page_number > 1:
            query['page'] = str(page_number - 1)

            previous_url = f'{base_url}?{query.urlencode()}'

        return {
            'count': total,
            'next': next_url,
            'previous': previous_url,
            'items': page_items,
        }

    def _version_or_404(self, memory: Memory, version_number: int) -> MemoryVersion:
        version = MemoryVersion.objects.filter(memory=memory, version=version_number).first()

        if version is None:
            raise MemoryReviewError(
                'version_not_found',
                'memory version not found',
                status=HTTP_404_NOT_FOUND,
            )

        return version

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

    def _int_query(self, request: Request, name: str) -> int | None:
        raw = request.query_params.get(name)

        if raw is None:
            return None

        try:
            return int(raw)

        except ValueError:
            return None
