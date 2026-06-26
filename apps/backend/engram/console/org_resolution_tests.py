from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from engram.access.auth_services import external_id_for_user
from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.console.org_resolution import (
    OrganizationNotMemberError,
    OrganizationRequiredError,
    resolve_active_organization,
)
from engram.core.models import Organization


def _make_user(username: str = 'alice') -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_role(code: str = 'organization_owner') -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    return role


def _make_identity(user: User, organization: Organization, *, active: bool = True) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    identity.active = active

    identity.save(update_fields=['active'])

    return identity


def _make_membership(
    user: User,
    organization: Organization,
    *,
    role_code: str = 'organization_owner',
    active: bool = True,
) -> OrganizationMembership:
    identity = _make_identity(user, organization)

    membership, _ = OrganizationMembership.objects.get_or_create(
        organization=organization,
        identity=identity,
        defaults={'role': _make_role(role_code)},
    )

    membership.active = active

    membership.save(update_fields=['active'])

    return membership


def _request(user: User, header_value: str | None) -> object:
    meta: dict[str, str] = {}

    if header_value is not None:
        meta['HTTP_X_ENGRAM_ORGANIZATION'] = header_value

    return type('R', (), {'META': meta, 'user': user})()


@pytest.mark.django_db
def test_resolve_by_header_uuid_when_member() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    _make_membership(user, org)

    resolved = resolve_active_organization(_request(user, str(org.id)))

    assert resolved.id == org.id


@pytest.mark.django_db
def test_resolve_by_header_slug_when_member() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    _make_membership(user, org)

    resolved = resolve_active_organization(_request(user, org.slug))

    assert resolved.id == org.id


@pytest.mark.django_db
def test_resolve_raises_when_not_member() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    other_org = Organization.objects.create(name='Other', slug='other')

    _make_membership(user, other_org)

    with pytest.raises(OrganizationNotMemberError):
        resolve_active_organization(_request(user, str(org.id)))


@pytest.mark.django_db
def test_resolve_raises_when_org_not_found() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    _make_membership(user, org)

    with pytest.raises(OrganizationNotMemberError):
        resolve_active_organization(_request(user, 'no-such-org'))


@pytest.mark.django_db
def test_resolve_raises_when_membership_inactive() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    _make_membership(user, org, active=False)

    with pytest.raises(OrganizationNotMemberError):
        resolve_active_organization(_request(user, str(org.id)))


@pytest.mark.django_db
def test_resolve_falls_back_to_single_membership() -> None:
    user = _make_user()
    org = Organization.objects.create(name='Acme', slug='acme')

    _make_membership(user, org)

    resolved = resolve_active_organization(_request(user, None))

    assert resolved.id == org.id


@pytest.mark.django_db
def test_resolve_requires_header_when_multiple_memberships() -> None:
    user = _make_user()
    first = Organization.objects.create(name='Acme', slug='acme')
    second = Organization.objects.create(name='Globex', slug='globex')

    _make_membership(user, first)
    _make_membership(user, second)

    with pytest.raises(OrganizationRequiredError):
        resolve_active_organization(_request(user, None))


@pytest.mark.django_db
def test_resolve_ignores_inactive_memberships_for_fallback() -> None:
    user = _make_user()
    active_org = Organization.objects.create(name='Acme', slug='acme')

    inactive_org = Organization.objects.create(name='Globex', slug='globex')

    _make_membership(user, active_org, active=True)
    _make_membership(user, inactive_org, active=False)

    resolved = resolve_active_organization(_request(user, None))

    assert resolved.id == active_org.id
