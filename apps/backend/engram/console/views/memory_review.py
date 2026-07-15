from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import F
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

from engram.access.models import Identity
from engram.console.filters import MemoryReviewCandidateFilterSet, MemoryReviewMemoryFilterSet
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.memory_review import (
    BulkArchiveResultSerializer,
    BulkArchiveSerializer,
    BulkReviewActionResultSerializer,
    BulkReviewActionSerializer,
    MemoryReviewActionSerializer,
    queue_item_payload,
    version_slice,
)
from engram.console.services import (
    REVIEW_MEMORY_STATUSES,
    MemoryReviewError,
    bulk_archive_memories,
    get_review_candidate_or_404,
    get_review_memory_or_404,
    reviewable_memory_filter,
)
from engram.console.usecases.review_action import ReviewActionInput, ReviewActionUseCase
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryVersion,
)

PAGE_SIZE = 50

REVIEW_ORDERING_FIELDS = ('confidence', '-confidence', 'created_at', '-created_at')
DEFAULT_REVIEW_ORDERING = '-created_at'


class MemoryReviewViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    pagination_class: Any = None

    def get_permissions(self) -> list[BasePermission]:
        if self.action in {'review_action', 'bulk_archive', 'bulk_action'}:
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
        page = self._reviewable_page(request)

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
            project_id=data.get('project_id'),
            team_id=data.get('team_id'),
        )

        payload = {
            'archived_count': len(archived_ids),
            'archived_ids': archived_ids,
        }

        result = BulkArchiveResultSerializer(payload)

        return Response(result.data, status=HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='bulk-action')
    def bulk_action(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        actor_identity = request.user_identity

        serializer = BulkReviewActionSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        results = [
            self._apply_bulk_review_action(
                request,
                organization,
                actor_identity,
                item_id,
                data['action'],
                data['reason'],
            )
            for item_id in data['ids']
        ]

        done_count = sum(1 for result in results if result['outcome'] == 'done')

        payload = {
            'results': results,
            'done_count': done_count,
            'skipped_count': len(results) - done_count,
        }

        result = BulkReviewActionResultSerializer(payload)

        return Response(result.data, status=HTTP_200_OK)

    def _apply_bulk_review_action(
        self,
        request: Request,
        organization: Any,
        actor_identity: Identity,
        item_id: uuid.UUID,
        action_name: str,
        reason: str,
    ) -> dict[str, Any]:
        try:
            ReviewActionUseCase(user=request.user, transaction=transaction.atomic()).execute(
                ReviewActionInput(
                    organization=organization,
                    actor_identity=actor_identity,
                    item_id=item_id,
                    action_name=action_name,
                    reason=reason,
                ),
            )

        except MemoryReviewError as error:
            outcome = 'not_found' if error.code == 'not_found' else 'invalid_state'

            return {'id': item_id, 'outcome': outcome}

        return {'id': item_id, 'outcome': 'done'}

    def _reviewable_page(self, request: Request) -> dict[str, Any]:
        organization = request.active_organization

        ordering = self._ordering(request)

        candidates_qs = self._order_queryset(
            self._filtered_candidates(request, organization),
            ordering,
        )

        memories_qs = self._order_queryset(
            self._filtered_memories(request, organization),
            ordering,
        )

        page_number = self._page_number(request)

        start = (page_number - 1) * PAGE_SIZE

        end = start + PAGE_SIZE

        candidates = list(candidates_qs[:end])

        memories = list(memories_qs.prefetch_related('links', 'versions__source_observation')[:end])

        combined = self._sort_items(candidates + memories, ordering)

        page_items = combined[start:end]

        total = candidates_qs.count() + memories_qs.count()

        return {
            'count': total,
            'next': self._page_url(request, page_number + 1) if end < total else None,
            'previous': self._page_url(request, page_number - 1) if page_number > 1 else None,
            'items': page_items,
        }

    def _order_queryset(self, queryset: Any, ordering: str) -> Any:
        field = ordering.lstrip('-')

        if field == 'confidence':
            if ordering.startswith('-'):
                return queryset.order_by(F('confidence').desc(nulls_last=True), '-created_at')

            return queryset.order_by(F('confidence').asc(nulls_first=True), '-created_at')

        return queryset.order_by(ordering)

    def _ordering(self, request: Request) -> str:
        ordering = request.query_params.get('ordering')

        if ordering in REVIEW_ORDERING_FIELDS:
            return ordering

        return DEFAULT_REVIEW_ORDERING

    def _sort_items(self, items: list, ordering: str) -> list:
        reverse = ordering.startswith('-')

        field = ordering.lstrip('-')

        if field == 'confidence':
            return sorted(items, key=self._confidence_key, reverse=reverse)

        return sorted(items, key=lambda item: item.created_at, reverse=reverse)

    def _confidence_key(self, item: Any) -> Decimal:
        if item.confidence is None:
            return Decimal(0)

        return item.confidence

    def _filtered_candidates(self, request: Request, organization: Any) -> Any:
        queryset = MemoryCandidate.objects.filter(
            organization=organization,
            status=CandidateStatus.PROPOSED,
            decision_work_contract_version=1,
        ).select_related('source_observation', 'project', 'team')

        queryset = self._apply_common_filters(request, queryset, candidate=True)

        return queryset

    def _filtered_memories(self, request: Request, organization: Any) -> Any:
        queryset = (
            Memory.objects.filter(organization=organization)
            .filter(transition_contract_version=1, current_transition__isnull=False)
            .filter(reviewable_memory_filter())
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

    def _page_number(self, request: Request) -> int:
        try:
            return max(int(request.query_params.get('page', '1')), 1)

        except ValueError:
            return 1

    def _page_url(self, request: Request, page_number: int) -> str:
        base_url = request.build_absolute_uri(request.path)

        query = request.query_params.copy()

        query['page'] = str(page_number)

        return f'{base_url}?{query.urlencode()}'

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
