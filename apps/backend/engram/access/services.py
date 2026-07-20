from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    OrganizationMembership,
    ProjectGrant,
    RoleCapability,
    TeamMembership,
)
from engram.access.organization_access import organization_access_blocked
from engram.core.domain.usecases.errors import DomainError
from engram.core.models import AuditEvent, AuditResult, Project, ProjectTeam, Team

API_KEY_PREFIX_LENGTH = 12


def api_key_prefix(raw_key: str) -> str:
    return raw_key[:API_KEY_PREFIX_LENGTH]


def hash_api_key(raw_key: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def api_key_fingerprint(raw_key: str) -> str:
    digest = hashlib.sha256(raw_key.encode()).hexdigest()

    return f'{api_key_prefix(raw_key)}...{digest[-12:]}'


@dataclass(frozen=True)
class EffectiveScope:
    organization_id: uuid.UUID
    identity_id: uuid.UUID
    api_key_id: uuid.UUID
    project_ids: tuple[uuid.UUID, ...]
    team_ids: tuple[uuid.UUID, ...]
    capabilities: tuple[str, ...]
    actor_type: str
    actor_id: str
    project_bound: bool
    team_bound: bool = False


ACCESS_STATUS = {
    'invalid_key': status.HTTP_401_UNAUTHORIZED,
    'inactive_key': status.HTTP_403_FORBIDDEN,
    'revoked_key': status.HTTP_403_FORBIDDEN,
    'expired_key': status.HTTP_403_FORBIDDEN,
    'inactive_owner': status.HTTP_403_FORBIDDEN,
    'missing_capability': status.HTTP_403_FORBIDDEN,
    'project_scope_denied': status.HTTP_403_FORBIDDEN,
    'team_scope_denied': status.HTTP_403_FORBIDDEN,
    'hook_identity_collision': status.HTTP_403_FORBIDDEN,
    'invalid_session': status.HTTP_401_UNAUTHORIZED,
    'organization_required': status.HTTP_400_BAD_REQUEST,
    'organization_not_found': status.HTTP_404_NOT_FOUND,
    'not_a_member': status.HTTP_403_FORBIDDEN,
    'organization_suspended': status.HTTP_403_FORBIDDEN,
    'missing_api_key': status.HTTP_401_UNAUTHORIZED,
}


class AccessDeniedError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            message,
            error_code=code,
            status_code=ACCESS_STATUS.get(code, status.HTTP_401_UNAUTHORIZED),
        )
        self.code = code


class ResolveApiKeyScope:
    def execute(
        self,
        *,
        raw_key: str,
        required_capability: str,
        requested_project_id: uuid.UUID | None = None,
        requested_team_id: uuid.UUID | None = None,
        request_id: str = '',
        correlation_id: str = '',
        target_type: str = '',
        target_id: str = '',
    ) -> EffectiveScope:
        key = self._find_key(raw_key)
        if key is None:
            raise AccessDeniedError('invalid_key', 'API key is invalid')

        if organization_access_blocked(key.organization):
            raise AccessDeniedError('organization_suspended', 'Organization is suspended')

        state_error = self._state_error(key)
        if state_error:
            self._audit(
                key,
                result=AuditResult.DENIED,
                required_capability=required_capability,
                request_id=request_id,
                correlation_id=correlation_id,
                target_type=target_type,
                target_id=target_id,
                reason=state_error,
                requested_project_id=requested_project_id,
                requested_team_id=requested_team_id,
            )
            raise AccessDeniedError(state_error, 'API key cannot be used')

        owner_capabilities = self._owner_capability_codes(
            key,
            requested_project_id=requested_project_id,
            requested_team_id=requested_team_id,
        )
        key_capabilities = self._key_capability_codes(key)
        effective_capabilities = tuple(sorted(owner_capabilities & key_capabilities))
        project_ids = self._project_ids(
            key,
            owner_capabilities,
            set(effective_capabilities),
            requested_project_id,
        )
        team_ids = self._team_ids(key, requested_team_id, project_ids, set(effective_capabilities))

        if project_ids is None:
            self._audit(
                key,
                result=AuditResult.DENIED,
                required_capability=required_capability,
                request_id=request_id,
                correlation_id=correlation_id,
                target_type=target_type,
                target_id=target_id,
                reason='project_scope_denied',
                requested_project_id=requested_project_id,
                requested_team_id=requested_team_id,
                effective_capabilities=effective_capabilities,
            )
            raise AccessDeniedError('project_scope_denied', 'API key cannot access requested project')

        if team_ids is None:
            self._audit(
                key,
                result=AuditResult.DENIED,
                required_capability=required_capability,
                request_id=request_id,
                correlation_id=correlation_id,
                target_type=target_type,
                target_id=target_id,
                reason='team_scope_denied',
                requested_project_id=requested_project_id,
                requested_team_id=requested_team_id,
                effective_capabilities=effective_capabilities,
                resolved_project_ids=project_ids or (),
            )
            raise AccessDeniedError('team_scope_denied', 'API key cannot access requested team')

        if required_capability not in effective_capabilities:
            self._audit(
                key,
                result=AuditResult.DENIED,
                required_capability=required_capability,
                request_id=request_id,
                correlation_id=correlation_id,
                target_type=target_type,
                target_id=target_id,
                reason='missing_capability',
                requested_project_id=requested_project_id,
                requested_team_id=requested_team_id,
                missing_capability=required_capability,
                effective_capabilities=effective_capabilities,
                resolved_project_ids=project_ids,
                resolved_team_ids=team_ids,
            )
            raise AccessDeniedError('missing_capability', 'API key lacks required capability')

        with transaction.atomic():
            key.last_used_at = timezone.now()
            key.save(update_fields=['last_used_at', 'updated_at'])
            self._audit(
                key,
                result=AuditResult.ALLOWED,
                required_capability=required_capability,
                request_id=request_id,
                correlation_id=correlation_id,
                target_type=target_type,
                target_id=target_id,
                reason='allowed',
                requested_project_id=requested_project_id,
                requested_team_id=requested_team_id,
                effective_capabilities=effective_capabilities,
                resolved_project_ids=project_ids,
                resolved_team_ids=team_ids,
            )

            return EffectiveScope(
                organization_id=key.organization_id,
                identity_id=key.owner_identity_id,
                api_key_id=key.id,
                project_ids=project_ids,
                team_ids=team_ids,
                capabilities=effective_capabilities,
                actor_type='api_key',
                actor_id=str(key.id),
                project_bound=bool(key.project_id),
                team_bound=bool(key.team_id),
            )

    def _find_key(self, raw_key: str) -> ApiKey | None:
        key_hash = hash_api_key(raw_key)
        keys = ApiKey.objects.select_related('organization', 'owner_identity', 'project', 'team').filter(
            key_prefix=api_key_prefix(raw_key),
        )
        for key in keys:
            if hmac.compare_digest(key.key_hash, key_hash):
                return key

        return None

    def _state_error(self, key: ApiKey) -> str:
        now = timezone.now()
        if not key.active:
            return 'inactive_key'
        if key.revoked_at is not None:
            return 'revoked_key'
        if key.expires_at is not None and key.expires_at <= now:
            return 'expired_key'
        if not key.owner_identity.active:
            return 'inactive_owner'

        return ''

    def _owner_capability_codes(
        self,
        key: ApiKey,
        *,
        requested_project_id: uuid.UUID | None,
        requested_team_id: uuid.UUID | None,
    ) -> set[str]:
        role_ids = list(
            OrganizationMembership.objects.filter(
                organization=key.organization,
                identity=key.owner_identity,
                active=True,
            ).values_list('role_id', flat=True),
        )
        role_ids.extend(
            ProjectGrant.objects.filter(
                organization=key.organization,
                identity=key.owner_identity,
                active=True,
                project_id=requested_project_id or key.project_id,
            ).values_list('role_id', flat=True),
        )
        role_ids.extend(
            TeamMembership.objects.filter(
                organization=key.organization,
                identity=key.owner_identity,
                active=True,
                team_id=requested_team_id or key.team_id,
            ).values_list('role_id', flat=True),
        )

        return set(RoleCapability.objects.filter(role_id__in=role_ids).values_list('capability__code', flat=True))

    def _key_capability_codes(self, key: ApiKey) -> set[str]:
        return set(ApiKeyCapability.objects.filter(api_key=key).values_list('capability__code', flat=True))

    def _project_ids(
        self,
        key: ApiKey,
        owner_capabilities: set[str],
        effective_capabilities: set[str],
        requested_project_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID, ...] | None:
        if key.project_id:
            if requested_project_id is not None and requested_project_id != key.project_id:
                return None
            if not self._owner_can_access_project(key, owner_capabilities, key.project_id):
                return None

            return (key.project_id,)

        if not (self._has_project_admin(effective_capabilities) or self._has_agent_scope(effective_capabilities)):
            return None

        if requested_project_id is None:
            return tuple(
                Project.objects.filter(organization=key.organization).values_list('id', flat=True),
            )

        project_exists = Project.objects.filter(organization=key.organization, id=requested_project_id).exists()
        if not project_exists:
            return None
        if self._has_agent_scope(effective_capabilities) or self._owner_can_access_project(
            key, owner_capabilities, requested_project_id
        ):
            return (requested_project_id,)

        return None

    def _has_project_admin(self, capabilities: set[str]) -> bool:
        return bool({'projects:*', 'policy:admin'} & capabilities)

    def _has_agent_scope(self, capabilities: set[str]) -> bool:
        return 'projects:agent' in capabilities

    def _has_team_admin(self, capabilities: set[str]) -> bool:
        return bool({'teams:*', 'policy:admin'} & capabilities)

    def _owner_can_access_project(
        self,
        key: ApiKey,
        owner_capabilities: set[str],
        project_id: uuid.UUID,
    ) -> bool:
        if {'projects:*', 'policy:admin'} & owner_capabilities:
            return True

        if ProjectGrant.objects.filter(
            organization=key.organization,
            project_id=project_id,
            identity=key.owner_identity,
            active=True,
        ).exists():
            return True

        return TeamMembership.objects.filter(
            organization=key.organization,
            team__project_links__project_id=project_id,
            identity=key.owner_identity,
            active=True,
        ).exists()

    def _team_ids(
        self,
        key: ApiKey,
        requested_team_id: uuid.UUID | None,
        project_ids: tuple[uuid.UUID, ...] | None,
        effective_capabilities: set[str],
    ) -> tuple[uuid.UUID, ...] | None:
        if key.team_id:
            if requested_team_id is not None and requested_team_id != key.team_id:
                return None

            return (key.team_id,)

        if requested_team_id is not None:
            if not self._has_team_admin(effective_capabilities):
                return None
            if not Team.objects.filter(organization=key.organization, id=requested_team_id).exists():
                return None
            if (
                project_ids
                and not ProjectTeam.objects.filter(
                    organization=key.organization,
                    team_id=requested_team_id,
                    project_id__in=project_ids,
                ).exists()
            ):
                return None

            return (requested_team_id,)

        return ()

    def _audit(
        self,
        key: ApiKey,
        *,
        result: str,
        required_capability: str,
        request_id: str,
        correlation_id: str,
        target_type: str,
        target_id: str,
        reason: str,
        requested_project_id: uuid.UUID | None,
        requested_team_id: uuid.UUID | None,
        missing_capability: str = '',
        effective_capabilities: tuple[str, ...] = (),
        resolved_project_ids: tuple[uuid.UUID, ...] = (),
        resolved_team_ids: tuple[uuid.UUID, ...] = (),
    ) -> None:
        if result == AuditResult.ALLOWED:
            return

        metadata = {
            'api_key_fingerprint': key.key_fingerprint,
            'owner_identity_id': str(key.owner_identity_id),
            'requested_project_id': str(requested_project_id) if requested_project_id else '',
            'requested_team_id': str(requested_team_id) if requested_team_id else '',
            'effective_capabilities': list(effective_capabilities),
            'reason': reason,
            'scope_filters': {
                'organization_id': str(key.organization_id),
                'project_ids': [str(project_id) for project_id in resolved_project_ids],
                'team_ids': [str(team_id) for team_id in resolved_team_ids],
            },
        }
        if missing_capability:
            metadata['missing_capability'] = missing_capability

        AuditEvent.objects.create(
            organization=key.organization,
            project_id=resolved_project_ids[0] if len(resolved_project_ids) == 1 else key.project_id,
            team_id=resolved_team_ids[0] if len(resolved_team_ids) == 1 else key.team_id,
            event_type='AccessScopeResolved',
            actor_type='api_key',
            actor_id=str(key.id),
            target_type=target_type,
            target_id=target_id,
            capability=required_capability,
            result=result,
            request_id=request_id,
            correlation_id=correlation_id,
            metadata=metadata,
        )
