from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from engram.access.models import Identity
from engram.core.models import AuditEvent, AuditResult, Organization, Team


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

