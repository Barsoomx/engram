from __future__ import annotations

import pytest

from engram.access.organization_access import organization_access_blocked
from engram.core.models import Organization, OrganizationStatus


@pytest.mark.django_db
def test_default_status_is_active() -> None:
    organization = Organization.objects.create(name='Acme', slug='acme')

    assert organization.status == OrganizationStatus.ACTIVE
    assert organization_access_blocked(organization) is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('status', 'blocked'),
    [
        (OrganizationStatus.ACTIVE, False),
        (OrganizationStatus.TRIALING, False),
        (OrganizationStatus.PAST_DUE, False),
        (OrganizationStatus.SUSPENDED, True),
        (OrganizationStatus.PENDING_DELETE, True),
    ],
)
def test_organization_access_blocked_matrix(status: str, blocked: bool) -> None:
    organization = Organization.objects.create(name='Acme', slug='acme', status=status)

    assert organization_access_blocked(organization) is blocked
