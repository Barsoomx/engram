from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from engram.core.models import AuditEvent, Organization, OrganizationStatus


@pytest.mark.django_db
def test_command_sets_status_and_audits() -> None:
    organization = Organization.objects.create(name='Acme', slug='acme')

    call_command('engram_set_organization_status', 'acme', 'suspended')

    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.SUSPENDED
    event = AuditEvent.objects.filter(
        organization=organization,
        event_type='OrganizationStatusChanged',
    ).first()
    assert event is not None
    assert event.metadata['previous_status'] == OrganizationStatus.ACTIVE
    assert event.metadata['new_status'] == OrganizationStatus.SUSPENDED


@pytest.mark.django_db
def test_command_rejects_unknown_organization() -> None:
    with pytest.raises(CommandError):
        call_command('engram_set_organization_status', 'nope', 'suspended')


@pytest.mark.django_db
def test_command_rejects_invalid_status() -> None:
    Organization.objects.create(name='Acme', slug='acme')

    with pytest.raises(CommandError):
        call_command('engram_set_organization_status', 'acme', 'bogus')
