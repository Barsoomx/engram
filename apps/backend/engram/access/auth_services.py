from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth.models import User
from django.db import transaction
from rest_framework.authtoken.models import Token

from engram.access.models import (
    Identity,
    IdentityType,
    MembershipStatus,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
    TeamMembership,
)
from engram.access.services import EffectiveScope
from engram.core.models import Organization, Project, Team

DEFAULT_ORGANIZATION_SLUG = 'default'
DEFAULT_ORGANIZATION_NAME = 'Default organization'
DEFAULT_ADMIN_ROLE_CODE = 'organization_admin'
DEFAULT_OWNER_ROLE_CODE = 'organization_owner'
USER_IDENTITY_EXTERNAL_ID_PREFIX = 'django-user:'

PROJECT_ADMIN_CAPABILITIES = {'projects:*', 'policy:admin'}
TEAM_ADMIN_CAPABILITIES = {'teams:*'}


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)

        self.code = code


@dataclass(frozen=True)
class LoginInput:
    raw_username: str
    raw_password: str


@dataclass(frozen=True)
class LoginResult:
    token: str
    user: User
    identity: Identity
    scope: EffectiveScope

    def to_response(self) -> dict[str, Any]:
        return {
            'token': self.token,
            'user_id': self.user.id,
            'username': self.user.get_username(),
            'identity_id': str(self.identity.id),
            'organization_id': str(self.scope.organization_id),
            'capabilities': list(self.scope.capabilities),
        }


@dataclass(frozen=True)
class CurrentUserResult:
    user: User
    identity: Identity
    scope: EffectiveScope

    def to_response(self) -> dict[str, Any]:
        return {
            'user_id': self.user.id,
            'username': self.user.get_username(),
            'identity_id': str(self.identity.id),
            'organization_id': str(self.scope.organization_id),
            'capabilities': list(self.scope.capabilities),
        }


def external_id_for_user(user: User) -> str:
    return f'{USER_IDENTITY_EXTERNAL_ID_PREFIX}{user.id}'


class LoginUser:
    def __init__(self, login_input: LoginInput) -> None:
        self._login_input = login_input

    @transaction.atomic
    def execute(self) -> LoginResult:
        user = django_authenticate(
            username=self._login_input.raw_username,
            password=self._login_input.raw_password,
        )
        if user is None:
            raise AuthError('invalid_credentials', 'Invalid username or password')

        if not user.is_active:
            raise AuthError('inactive_user', 'User account is disabled')

        identity = self._ensure_identity(user)
        token, _created = Token.objects.get_or_create(user=user)
        scope = resolve_user_scope(user)

        return LoginResult(token=token.key, user=user, identity=identity, scope=scope)

    def _ensure_identity(self, user: User) -> Identity:
        external_id = external_id_for_user(user)
        organization = self._ensure_organization()
        identity, created = Identity.objects.get_or_create(
            organization=organization,
            identity_type=IdentityType.USER,
            external_id=external_id,
            defaults={
                'display_name': user.get_username(),
                'email': getattr(user, 'email', '') or '',
            },
        )
        if created:
            self._ensure_membership(organization, identity)

        return identity

    def _ensure_organization(self) -> Organization:
        organization = Organization.objects.filter(slug=DEFAULT_ORGANIZATION_SLUG).first()
        if organization is not None:
            return organization

        return Organization.objects.create(
            name=DEFAULT_ORGANIZATION_NAME,
            slug=DEFAULT_ORGANIZATION_SLUG,
        )

    def _ensure_membership(self, organization: Organization, identity: Identity) -> None:
        if OrganizationMembership.objects.filter(organization=organization, identity=identity).exists():
            return

        role = Role.objects.filter(code=DEFAULT_ADMIN_ROLE_CODE).first()
        if role is None:
            role = Role.objects.filter(code=DEFAULT_OWNER_ROLE_CODE).first()
        if role is None:
            raise AuthError('default_role_missing', 'Default access role is not seeded')

        OrganizationMembership.objects.create(
            organization=organization,
            identity=identity,
            role=role,
        )


def resolve_user_scope(user: User) -> EffectiveScope:
    identity = resolve_user_identity(user)
    if identity is None:
        raise AuthError('identity_missing', 'User identity is not linked')

    if not identity.active:
        raise AuthError('identity_missing', 'User identity is not linked')

    membership = (
        OrganizationMembership.objects.filter(
            identity=identity,
            active=True,
            status=MembershipStatus.ACTIVE,
        )
        .select_related('organization')
        .order_by('created_at')
        .first()
    )
    if membership is None:
        raise AuthError('membership_missing', 'User has no active organization membership')

    organization = membership.organization
    capabilities = _user_capability_codes(organization, identity)
    project_ids = _user_project_ids(organization, identity, capabilities)
    team_ids = _user_team_ids(organization, identity, capabilities)

    return EffectiveScope(
        organization_id=organization.id,
        identity_id=identity.id,
        api_key_id=uuid.UUID(int=0),
        project_ids=project_ids,
        team_ids=team_ids,
        capabilities=tuple(sorted(capabilities)),
        actor_type='user',
        actor_id=str(user.id),
    )


def resolve_user_identity(user: User) -> Identity | None:
    return (
        Identity.objects.filter(
            identity_type=IdentityType.USER,
            external_id=external_id_for_user(user),
        )
        .select_related('organization')
        .first()
    )


def resolve_user_identity_in_organization(
    user: User,
    organization: Organization,
) -> Identity | None:
    return Identity.objects.filter(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
    ).first()


def resolve_user_scope_for_organization(
    user: User,
    organization: Organization,
) -> EffectiveScope:
    identity = resolve_user_identity_in_organization(user, organization)

    if identity is None:
        raise AuthError('identity_missing', 'User identity is not linked')

    if not OrganizationMembership.objects.filter(
        organization=organization,
        identity=identity,
        active=True,
        status=MembershipStatus.ACTIVE,
    ).exists():
        raise AuthError('membership_missing', 'User has no active membership in organization')

    capabilities = _user_capability_codes(organization, identity)
    project_ids = _user_project_ids(organization, identity, capabilities)
    team_ids = _user_team_ids(organization, identity, capabilities)

    return EffectiveScope(
        organization_id=organization.id,
        identity_id=identity.id,
        api_key_id=uuid.UUID(int=0),
        project_ids=project_ids,
        team_ids=team_ids,
        capabilities=tuple(sorted(capabilities)),
        actor_type='user',
        actor_id=str(user.id),
    )


def _user_capability_codes(organization: Organization, identity: Identity) -> set[str]:
    role_ids = list(
        OrganizationMembership.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
            status=MembershipStatus.ACTIVE,
        ).values_list('role_id', flat=True),
    )
    role_ids.extend(
        ProjectGrant.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
        ).values_list('role_id', flat=True),
    )
    role_ids.extend(
        TeamMembership.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
        ).values_list('role_id', flat=True),
    )

    return set(RoleCapability.objects.filter(role_id__in=role_ids).values_list('capability__code', flat=True))


def _user_project_ids(
    organization: Organization,
    identity: Identity,
    capabilities: set[str],
) -> tuple[uuid.UUID, ...]:
    if PROJECT_ADMIN_CAPABILITIES & capabilities:
        return tuple(Project.objects.filter(organization=organization).values_list('id', flat=True))

    granted = set(
        ProjectGrant.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
        ).values_list('project_id', flat=True),
    )
    granted.update(
        TeamMembership.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
            team__project_links__project__organization=organization,
        ).values_list('team__project_links__project_id', flat=True),
    )

    return tuple(sorted(granted))


def _user_team_ids(
    organization: Organization,
    identity: Identity,
    capabilities: set[str],
) -> tuple[uuid.UUID, ...]:
    if TEAM_ADMIN_CAPABILITIES & capabilities:
        return tuple(Team.objects.filter(organization=organization).values_list('id', flat=True))

    return tuple(
        TeamMembership.objects.filter(
            organization=organization,
            identity=identity,
            active=True,
        ).values_list('team_id', flat=True),
    )


class GetCurrentUser:
    def __init__(self, token: str) -> None:
        self._token = token

    def execute(self) -> CurrentUserResult:
        token = Token.objects.select_related('user').filter(key=self._token).first()
        if token is None:
            raise AuthError('invalid_token', 'Authentication token is invalid')

        user = token.user
        if not user.is_active:
            raise AuthError('inactive_user', 'User account is disabled')

        identity = resolve_user_identity(user)
        if identity is None:
            raise AuthError('identity_missing', 'User identity is not linked')

        scope = resolve_user_scope(user)

        return CurrentUserResult(user=user, identity=identity, scope=scope)


class LogoutUser:
    def __init__(self, token: str) -> None:
        self._token = token

    def execute(self) -> None:
        deleted, _rows = Token.objects.filter(key=self._token).delete()
        if deleted == 0:
            raise AuthError('invalid_token', 'Authentication token is invalid')
