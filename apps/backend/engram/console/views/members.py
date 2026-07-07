from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Q
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.status import HTTP_204_NO_CONTENT

from engram.access.models import OrganizationMembership
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.members import (
    MemberReadSerializer,
    MemberWriteSerializer,
)
from engram.console.services import (
    activate_member,
    audit_admin_action,
    invite_member,
    reactivate_member,
)
from engram.console.usecases.members import (
    RemoveMember,
    RemoveMemberInput,
    SetMemberRole,
    SetMemberRoleInput,
)


class MemberViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self) -> list[BasePermission]:
        if self.action in {'list', 'retrieve'}:
            return [
                IsAuthenticated(),
                ActiveOrganizationPermission(),
                RequireCapability('members:read'),
            ]

        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('members:admin'),
        ]

    def get_queryset(self) -> Any:
        queryset = (
            OrganizationMembership.objects.filter(
                organization=self.request.active_organization,
            )
            .select_related('identity', 'role')
            .order_by('created_at')
        )

        if self.action == 'reactivate':
            return queryset

        if self.action == 'list':
            return self._filter_list(queryset)

        return queryset.filter(active=True)

    def _filter_list(self, queryset: Any) -> Any:
        queryset = queryset.filter(active=self._active_param())

        role = self.request.query_params.get('role')

        if role:
            queryset = queryset.filter(role__code=role)

        search = self.request.query_params.get('search')

        if search:
            queryset = queryset.filter(
                Q(identity__display_name__icontains=search)
                | Q(identity__email__icontains=search)
                | Q(identity__external_id__icontains=search),
            )

        return queryset

    def _active_param(self) -> bool:
        raw = self.request.query_params.get('active')

        if raw is None:
            return True

        return raw.strip().lower() not in {'false', '0', 'no'}

    def get_serializer_context(self) -> dict:
        context = super().get_serializer_context()

        context['organization'] = self.request.active_organization

        return context

    def get_serializer_class(self) -> type:
        if self.action in {'create', 'partial_update', 'update'}:
            return MemberWriteSerializer

        return MemberReadSerializer

    def perform_create(self, serializer: BaseSerializer) -> None:
        membership = invite_member(
            organization=self.request.active_organization,
            external_id=serializer.validated_data['external_id'],
            display_name=serializer.validated_data['display_name'],
            email=serializer.validated_data.get('email', ''),
            role=serializer.validated_data['role'],
        )

        serializer.instance = membership

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='MemberInvited',
            target_type='member',
            target_id=str(membership.id),
            metadata={
                'external_id': membership.identity.external_id,
                'role': membership.role.code,
            },
        )

    def perform_update(self, serializer: BaseSerializer) -> None:
        membership = serializer.instance

        new_role = serializer.validated_data['role']

        previous_role = membership.role.code

        SetMemberRole(user=self.request.user, transaction=transaction.atomic()).execute(
            SetMemberRoleInput(membership=membership, role=new_role),
        )

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='MemberRoleChanged',
            target_type='member',
            target_id=str(membership.id),
            metadata={
                'previous_role': previous_role,
                'new_role': new_role.code,
            },
        )

    @action(detail=True, methods=['post'])
    def activate(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        membership = self.get_object()

        membership = activate_member(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            membership_id=membership.id,
        )

        serializer = self.get_serializer(membership)

        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def reactivate(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        membership = self.get_object()

        with transaction.atomic():
            membership = reactivate_member(
                organization=self.request.active_organization,
                actor_identity=self.request.user_identity,
                membership_id=membership.id,
            )

        serializer = self.get_serializer(membership)

        return Response(serializer.data)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        membership = self.get_object()

        RemoveMember(user=self.request.user, transaction=transaction.atomic()).execute(
            RemoveMemberInput(membership=membership),
        )

        audit_admin_action(
            organization=self.request.active_organization,
            actor_identity=self.request.user_identity,
            event_type='MemberRemoved',
            target_type='member',
            target_id=str(membership.id),
        )

        return Response(status=HTTP_204_NO_CONTENT)
