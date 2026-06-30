from __future__ import annotations

from engram.core.models import Organization, OrganizationStatus

BLOCKED_STATUSES = frozenset(
    {
        OrganizationStatus.SUSPENDED,
        OrganizationStatus.PENDING_DELETE,
    },
)


def organization_access_blocked(organization: Organization) -> bool:
    return organization.status in BLOCKED_STATUSES
