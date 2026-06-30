from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth.models import User
from rest_framework.permissions import BasePermission

from engram.access.auth_services import (
    AuthError,
    external_id_for_user,
    resolve_user_scope_for_organization,
)
from engram.access.models import Identity, IdentityType, OrganizationMembership
from engram.access.organization_access import organization_access_blocked
from engram.core.models import Organization

ORGANIZATION_HEADER = 'HTTP_X_ENGRAM_ORGANIZATION'


class OrganizationRequiredError(Exception):
    pass


class OrganizationNotMemberError(Exception):
    pass


def resolve_active_organization(request: Any) -> Organization:
    user: User = request.user
    header = request.META.get(ORGANIZATION_HEADER, '').strip()

    if header:
        organization = _organization_by_header(header)
        if organization is None:
            raise OrganizationNotMemberError('organization not found')

        _require_active_member(user, organization)

        return organization

    memberships = list(
        _active_memberships_for_user(user).select_related('organization'),
    )

    if len(memberships) == 1:
        return memberships[0].organization

    raise OrganizationRequiredError('X-Engram-Organization header required')


def _organization_by_header(header: str) -> Organization | None:
    organization = Organization.objects.filter(slug=header).first()

    if organization is not None:
        return organization

    try:
        value = uuid.UUID(header)
    except ValueError:
        return None

    return Organization.objects.filter(id=value).first()


def _require_active_member(user: User, organization: Organization) -> None:
    identity = _user_identity_in_organization(user, organization)

    if identity is None:
        raise OrganizationNotMemberError('not a member of organization')

    is_member = OrganizationMembership.objects.filter(
        organization=organization,
        identity=identity,
        active=True,
    ).exists()

    if not is_member:
        raise OrganizationNotMemberError('not a member of organization')


def _user_identity_in_organization(user: User, organization: Organization) -> Identity | None:
    return Identity.objects.filter(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
    ).first()


def _active_memberships_for_user(user: User) -> Any:
    identity_ids = Identity.objects.filter(
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
    ).values('id')

    return OrganizationMembership.objects.filter(
        identity_id__in=identity_ids,
        active=True,
    )


class ActiveOrganizationPermission(BasePermission):
    def has_permission(self, request: Any, view: Any) -> bool:
        try:
            organization = resolve_active_organization(request)
        except (OrganizationRequiredError, OrganizationNotMemberError):
            return False

        if organization_access_blocked(organization):
            return False

        identity = _user_identity_in_organization(request.user, organization)

        if identity is None:
            return False

        request.active_organization = organization

        request.user_identity = identity

        try:
            request.effective_scope = resolve_user_scope_for_organization(
                request.user,
                organization,
            )
        except AuthError:
            return False

        return True
