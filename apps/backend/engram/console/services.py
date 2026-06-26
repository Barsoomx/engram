from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from engram.access.models import (
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.console.exceptions import LastOwnerError
from engram.core.models import AuditEvent, AuditResult, Organization, Project, Team


OWNER_ROLE_CODE = 'organization_owner'


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

