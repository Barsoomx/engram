from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.access.services import (
    api_key_fingerprint,
    api_key_prefix,
    hash_api_key,
)
from engram.console.exceptions import LastOwnerError
from engram.core.models import AuditEvent, AuditResult, Organization, Project, Team

OWNER_ROLE_CODE = 'organization_owner'

API_KEY_TOKEN_PREFIX = 'egk_'

WILDCARD_ADMIN_CAPABILITY = 'policy:admin'


def _active_owner_count(organization: Organization) -> int:
    return OrganizationMembership.objects.filter(
        organization=organization,
        role__code=OWNER_ROLE_CODE,
        active=True,
    ).count()


def audit_admin_action(
    *,
    organization: Organization,
    actor_identity: Identity,
    event_type: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any] | None = None,
    result: str = AuditResult.RECORDED,
) -> AuditEvent:
    return AuditEvent.objects.create(
        organization=organization,
        event_type=event_type,
        actor_type='user',
        actor_id=str(actor_identity.id),
        target_type=target_type,
        target_id=target_id,
        capability='',
        result=result,
        metadata=metadata or {},
    )


@transaction.atomic
def create_team(
    *,
    organization: Organization,
    name: str,
    slug: str,
) -> Team:
    return Team.objects.create(organization=organization, name=name, slug=slug)


@transaction.atomic
def archive_team(team: Team) -> Team:
    team.archived_at = timezone.now()

    team.save(update_fields=['archived_at', 'updated_at'])

    return team


@transaction.atomic
def create_project(
    *,
    organization: Organization,
    name: str,
    slug: str,
    repository_url: str = '',
    default_branch: str = '',
) -> Project:
    return Project.objects.create(
        organization=organization,
        name=name,
        slug=slug,
        repository_url=repository_url,
        default_branch=default_branch,
    )


@transaction.atomic
def archive_project(project: Project) -> Project:
    project.archived_at = timezone.now()

    project.save(update_fields=['archived_at', 'updated_at'])

    return project


@transaction.atomic
def invite_member(
    *,
    organization: Organization,
    external_id: str,
    display_name: str,
    email: str,
    role: Role,
) -> OrganizationMembership:
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id,
        display_name=display_name,
        email=email,
    )

    return OrganizationMembership.objects.create(
        organization=organization,
        identity=identity,
        role=role,
    )


@transaction.atomic
def set_member_role(membership: OrganizationMembership, role: Role) -> OrganizationMembership:
    is_current_owner = membership.role.code == OWNER_ROLE_CODE and membership.active

    if is_current_owner and role.code != OWNER_ROLE_CODE:
        if _active_owner_count(membership.organization) <= 1:
            raise LastOwnerError(
                'cannot demote the last active organization owner',
            )

    membership.role = role

    membership.save(update_fields=['role', 'updated_at'])

    return membership


@transaction.atomic
def remove_member(membership: OrganizationMembership) -> OrganizationMembership:
    is_current_owner = membership.role.code == OWNER_ROLE_CODE and membership.active

    if is_current_owner and _active_owner_count(membership.organization) <= 1:
        raise LastOwnerError(
            'cannot remove the last active organization owner',
        )

    membership.active = False

    membership.save(update_fields=['active', 'updated_at'])

    return membership


class CapabilityWideningError(Exception):
    pass


def _issuer_can_grant(
    requested_capabilities: Iterable[str],
    issuer_capabilities: Iterable[str],
) -> set[str]:
    issuer = set(issuer_capabilities)

    granted: set[str] = set()

    for capability in requested_capabilities:
        if capability in issuer:
            granted.add(capability)

            continue

        group = capability.split(':')[0]

        if f'{group}:*' in issuer or WILDCARD_ADMIN_CAPABILITY in issuer:
            granted.add(capability)

            continue

        raise CapabilityWideningError(
            f'issuer cannot grant capability {capability!r}',
        )

    return granted


def generate_api_key_plaintext() -> str:
    return f'{API_KEY_TOKEN_PREFIX}{secrets.token_urlsafe(32)}'


@transaction.atomic
def issue_api_key(
    *,
    organization: Organization,
    owner_identity: Identity,
    name: str,
    capabilities: list[str],
    team: Team | None = None,
    project: Project | None = None,
    expires_at: Any = None,
) -> tuple[ApiKey, str]:
    capability_objs = list(Capability.objects.filter(code__in=capabilities))

    found_codes = {capability.code for capability in capability_objs}

    missing_codes = set(capabilities) - found_codes

    if missing_codes:
        raise CapabilityWideningError(
            f'unknown capabilities: {sorted(missing_codes)}',
        )

    plaintext = generate_api_key_plaintext()

    api_key = ApiKey(
        organization=organization,
        owner_identity=owner_identity,
        name=name,
        key_prefix=api_key_prefix(plaintext),
        key_hash=hash_api_key(plaintext),
        key_fingerprint=api_key_fingerprint(plaintext),
        team=team,
        project=project,
        active=True,
        expires_at=expires_at,
    )

    api_key.full_clean()

    api_key.save()

    for capability in capability_objs:
        ApiKeyCapability.objects.get_or_create(api_key=api_key, capability=capability)

    return api_key, plaintext


@transaction.atomic
def revoke_api_key(api_key: ApiKey) -> ApiKey:
    api_key.revoked_at = timezone.now()

    api_key.save(update_fields=['revoked_at', 'updated_at'])

    return api_key
