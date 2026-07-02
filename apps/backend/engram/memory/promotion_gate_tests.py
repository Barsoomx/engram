from __future__ import annotations

from decimal import Decimal

import pytest

from engram.core.models import Organization, OrganizationSettings
from engram.memory.services import is_auto_promotable, resolve_auto_approve_threshold


def test_is_auto_promotable_true_at_or_above_threshold() -> None:
    assert is_auto_promotable(Decimal('0.900'), Decimal('0.800')) is True
    assert is_auto_promotable(Decimal('0.800'), Decimal('0.800')) is True


def test_is_auto_promotable_false_below_threshold_or_missing_confidence() -> None:
    assert is_auto_promotable(Decimal('0.400'), Decimal('0.800')) is False
    assert is_auto_promotable(None, Decimal('0.800')) is False


@pytest.mark.django_db
def test_resolve_auto_approve_threshold_prefers_explicit_override() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    OrganizationSettings.objects.create(
        organization=organization,
        distillation_auto_approve_threshold=Decimal('0.600'),
    )

    assert resolve_auto_approve_threshold(organization, Decimal('0.250')) == Decimal('0.250')


@pytest.mark.django_db
def test_resolve_auto_approve_threshold_uses_org_setting_without_override() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    OrganizationSettings.objects.create(
        organization=organization,
        distillation_auto_approve_threshold=Decimal('0.600'),
    )

    assert resolve_auto_approve_threshold(organization) == Decimal('0.600')


@pytest.mark.django_db
def test_resolve_auto_approve_threshold_falls_back_to_settings_default() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')

    assert resolve_auto_approve_threshold(organization) == Decimal('0.500')


@pytest.mark.django_db
def test_resolve_auto_approve_threshold_falls_back_when_org_setting_is_null() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    OrganizationSettings.objects.create(organization=organization)

    assert resolve_auto_approve_threshold(organization) == Decimal('0.500')
