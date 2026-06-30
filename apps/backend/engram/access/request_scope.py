from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from rest_framework.request import Request

from engram.access.auth_services import AuthError, resolve_user_scope_for_organization
from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.core.models import Organization

_ORGANIZATION_HEADER = 'HTTP_X_ENGRAM_ORGANIZATION'


def resolve_request_scope(
    request: Request,
    *,
    required_capability: str,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None = None,
    target_type: str = '',
    target_id: str = '',
    request_id: str = '',
) -> EffectiveScope:
    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if auth_header.startswith('Token '):
        return _session_scope(
            request,
            required_capability=required_capability,
            project_id=project_id,
            team_id=team_id,
        )

    return _bearer_scope(
        auth_header,
        required_capability=required_capability,
        project_id=project_id,
        team_id=team_id,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id,
    )


def _session_scope(
    request: Request,
    *,
    required_capability: str,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> EffectiveScope:
    user: User = request.user
    if not user or not user.is_authenticated:
        raise AccessDeniedError('invalid_session', 'Session authentication required')

    raw_header = request.META.get(_ORGANIZATION_HEADER, '').strip()
    if not raw_header:
        raise AccessDeniedError('organization_required', 'X-Engram-Organization header is required')

    organization = _organization_by_header(raw_header)
    if organization is None:
        raise AccessDeniedError('organization_not_found', 'Organization not found')

    try:
        scope = resolve_user_scope_for_organization(user, organization)
    except AuthError:
        raise AccessDeniedError('not_a_member', 'User is not a member of this organization') from None

    _check_capability(scope, required_capability)

    if project_id is not None and project_id not in scope.project_ids:
        raise AccessDeniedError('project_scope_denied', 'User cannot access requested project')

    if team_id is not None and team_id not in scope.team_ids:
        raise AccessDeniedError('team_scope_denied', 'User cannot access requested team')

    return scope


def _check_capability(scope: EffectiveScope, required_capability: str) -> None:
    prefix = required_capability.split(':')[0]
    wildcard = f'{prefix}:*'
    if required_capability not in scope.capabilities and wildcard not in scope.capabilities:
        raise AccessDeniedError('missing_capability', 'User lacks the required capability')


def _organization_by_header(header: str) -> Organization | None:
    organization = Organization.objects.filter(slug=header).first()
    if organization is not None:
        return organization

    try:
        org_id = uuid.UUID(header)
    except ValueError:
        return None

    return Organization.objects.filter(id=org_id).first()


def _bearer_scope(
    auth_header: str,
    *,
    required_capability: str,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
    target_type: str,
    target_id: str,
    request_id: str,
) -> EffectiveScope:
    prefix = 'Bearer '
    if not auth_header.startswith(prefix) or not auth_header[len(prefix) :].strip():
        raise AccessDeniedError('missing_api_key', 'Missing bearer API key')

    raw_key = auth_header[len(prefix) :].strip()

    return ResolveApiKeyScope().execute(
        raw_key=raw_key,
        required_capability=required_capability,
        requested_project_id=project_id,
        requested_team_id=team_id,
        request_id=request_id,
        target_type=target_type,
        target_id=target_id,
    )
