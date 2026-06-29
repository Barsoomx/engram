from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.db.models import Q
from django.utils import timezone
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
    approve_memory_candidate,
    archive_memory,
    bulk_archive_memories,
    edit_memory_body,
    get_review_candidate_or_404,
    get_review_memory_or_404,
    narrow_memory,
    reject_review_item,
    supersede_memory,
)
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

        memory = (
            Memory.objects.filter(
                organization=organization,
                id=memory_id,
            ).first()
        )

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

        action_name = data['action']

        reason = data['reason']

        try:
            result = self._apply_action(
                organization=organization,
                actor_identity=actor_identity,
                item_id=item_id,
                action_name=action_name,
                data=data,
                reason=reason,
            )

        except MemoryReviewError as error:
            return Response(
                {'code': error.code, 'detail': str(error)},
                status=error.status,
            )

        return Response(result, status=HTTP_200_OK)

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
        queryset = Memory.objects.filter(organization=organization).filter(
            Q(status__in=REVIEW_MEMORY_STATUSES)
            | Q(status=MemoryStatus.APPROVED, confidence__lte=REVIEW_MEMORY_CONFIDENCE_THRESHOLD),
        ).select_related('project', 'team')

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
        team_id = request.query_params.get('team_id')

        if team_id:
            queryset = queryset.filter(team_id=team_id)

        project_id = request.query_params.get('project_id')

        if project_id:
            queryset = queryset.filter(project_id=project_id)

        visibility_scope = request.query_params.get('visibility_scope')

        if visibility_scope:
            queryset = queryset.filter(visibility_scope=visibility_scope)

        confidence_gte = request.query_params.get('confidence__gte')

        if confidence_gte:
            queryset = queryset.filter(confidence__gte=confidence_gte)

        confidence_lte = request.query_params.get('confidence__lte')

        if confidence_lte:
            queryset = queryset.filter(confidence__lte=confidence_lte)

        age_days = request.query_params.get('age_days__gte')

        if age_days:
            try:
                days = int(age_days)

            except ValueError:
                days = 0

            if days > 0:
                cutoff = timezone.now() - datetime.timedelta(days=days)

                queryset = queryset.filter(created_at__lte=cutoff)

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

    def _apply_action(
        self,
        organization: Any,
        actor_identity: Any,
        item_id: uuid.UUID,
        action_name: str,
        data: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        if action_name == 'approve':
            candidate = get_review_candidate_or_404(organization, item_id)

            memory = approve_memory_candidate(organization, actor_identity, candidate, reason)

            return {
                'action': 'approve',
                'candidate_id': str(candidate.id),
                'memory_id': str(memory.id),
            }

        if action_name == 'edit':
            memory = get_review_memory_or_404(organization, item_id)

            body = data.get('body')

            if not body:
                raise MemoryReviewError('body_required', 'body is required for edit action')

            version = edit_memory_body(organization, actor_identity, memory, body, reason)

            return {
                'action': 'edit',
                'memory_id': str(memory.id),
                'version': version.version,
            }

        if action_name == 'narrow':
            memory = get_review_memory_or_404(organization, item_id)

            target_id = data.get('target_memory_id')

            if target_id is None:
                raise MemoryReviewError(
                    'target_required',
                    'target_memory_id is required for narrow action',
                )

            link = narrow_memory(organization, actor_identity, memory, target_id, reason)

            return {
                'action': 'narrow',
                'memory_id': str(memory.id),
                'link_id': str(link.id),
            }

        if action_name == 'supersede':
            memory = get_review_memory_or_404(organization, item_id)

            target_id = data.get('target_memory_id')

            if target_id is None:
                raise MemoryReviewError(
                    'target_required',
                    'target_memory_id is required for supersede action',
                )

            link = supersede_memory(organization, actor_identity, memory, target_id, reason)

            return {
                'action': 'supersede',
                'memory_id': str(memory.id),
                'link_id': str(link.id),
            }

        if action_name == 'reject':
            candidate = MemoryCandidate.objects.filter(
                organization=organization,
                id=item_id,
            ).first()

            if candidate is not None:
                reject_review_item(organization, actor_identity, candidate, reason)

                return {
                    'action': 'reject',
                    'candidate_id': str(candidate.id),
                }

            memory = get_review_memory_or_404(organization, item_id)

            reject_review_item(organization, actor_identity, memory, reason)

            return {
                'action': 'reject',
                'memory_id': str(memory.id),
            }

        if action_name == 'archive':
            memory = get_review_memory_or_404(organization, item_id)

            archive_memory(organization, actor_identity, memory, reason)

            return {
                'action': 'archive',
                'memory_id': str(memory.id),
            }

        raise MemoryReviewError('unknown_action', f'unknown action {action_name!r}')

    def _version_or_404(self, memory: Memory, version_number: int) -> MemoryVersion:
        version = (
            MemoryVersion.objects.filter(memory=memory, version=version_number).first()
        )

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
