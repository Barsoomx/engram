from __future__ import annotations

from typing import Any

import structlog
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_400_BAD_REQUEST

from engram.access.models import ApiKey, Capability
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.api_keys import (
    ApiKeyIssueInputSerializer,
    ApiKeyIssueResultSerializer,
    ApiKeyReadSerializer,
)
from engram.console.services import (
    CapabilityWideningError,
    _issuer_can_grant,
    audit_admin_action,
    issue_api_key,
    revoke_api_key,
)
from engram.core.models import AuditResult

logger = structlog.get_logger(__name__)


class ApiKeyViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        if self.action == 'create':
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('api_keys:issue'),
            ]

        if self.action == 'revoke':
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('api_keys:revoke'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('api_keys:read'),
        ]

    def get_queryset(self) -> Any:
        queryset = (
            ApiKey.objects.filter(
                organization=self.request.active_organization,
            )
            .select_related('owner_identity')
            .order_by('created_at')
        )

        if self.action != 'list':
            return queryset

        queryset = self._filter_status(queryset)

        search = self.request.query_params.get('search')

        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(key_prefix__icontains=search))

        return queryset

    def _filter_status(self, queryset: Any) -> Any:
        status_param = self.request.query_params.get('status')

        if status_param == 'revoked':
            return queryset.filter(revoked_at__isnull=False)

        now = timezone.now()

        if status_param == 'expired':
            return queryset.filter(revoked_at__isnull=True, expires_at__isnull=False, expires_at__lte=now)

        if status_param == 'active':
            return queryset.filter(revoked_at__isnull=True).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now),
            )

        return queryset

    def get_serializer_class(self) -> type:
        if self.action == 'create':
            return ApiKeyIssueInputSerializer

        return ApiKeyReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        requested_capabilities = list(serializer.validated_data['capabilities'])

        known_codes = set(
            Capability.objects.filter(code__in=requested_capabilities).values_list(
                'code',
                flat=True,
            ),
        )

        unknown_codes = sorted(set(requested_capabilities) - known_codes)

        if unknown_codes:
            return Response(
                {
                    'detail': f'unknown capabilities: {unknown_codes}',
                    'code': 'unknown_capability',
                },
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            _issuer_can_grant(
                requested_capabilities,
                request.effective_scope.capabilities,
            )
        except CapabilityWideningError:
            audit_admin_action(
                organization=request.active_organization,
                actor_identity=request.user_identity,
                event_type='ApiKeyIssueDenied',
                target_type='api_key',
                target_id='',
                metadata={'requested_capabilities': requested_capabilities},
                result=AuditResult.DENIED,
            )

            logger.warning(
                'api_key_issue_denied',
                organization_id=str(request.active_organization.id),
                requested_capabilities=requested_capabilities,
            )

            raise

        api_key, plaintext = issue_api_key(
            organization=request.active_organization,
            owner_identity=request.user_identity,
            name=serializer.validated_data['name'],
            capabilities=requested_capabilities,
            expires_at=serializer.validated_data.get('expires_at'),
        )

        audit_admin_action(
            organization=request.active_organization,
            actor_identity=request.user_identity,
            event_type='ApiKeyIssued',
            target_type='api_key',
            target_id=str(api_key.id),
            metadata={
                'name': api_key.name,
                'capabilities': requested_capabilities,
            },
        )

        result = ApiKeyIssueResultSerializer(api_key, context={'plaintext': plaintext})

        return Response(result.data, status=201)

    @action(detail=True, methods=['post'])
    def revoke(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        api_key = self.get_object()

        revoke_api_key(api_key)

        audit_admin_action(
            organization=request.active_organization,
            actor_identity=request.user_identity,
            event_type='ApiKeyRevoked',
            target_type='api_key',
            target_id=str(api_key.id),
            metadata={
                'name': api_key.name,
                'key_prefix': api_key.key_prefix,
            },
        )

        return Response(status=HTTP_200_OK)
