from __future__ import annotations

import pytest
import structlog
from django.db import transaction

from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.console.exceptions import LastOwnerError
from engram.console.usecases.members import (
    RemoveMember,
    RemoveMemberInput,
    SetMemberRole,
    SetMemberRoleInput,
)
from engram.core.models import Organization


@pytest.fixture
def f_organization() -> Organization:
    return Organization.objects.create(name='Acme', slug='acme')


@pytest.fixture
def f_owner_role() -> Role:
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'organization_owner'})

    return role


@pytest.fixture
def f_developer_role() -> Role:
    role, _ = Role.objects.get_or_create(code='developer', defaults={'name': 'developer'})

    return role


def _make_membership(
    organization: Organization,
    role: Role,
    *,
    external_id: str,
) -> OrganizationMembership:
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id,
        display_name=external_id,
    )

    return OrganizationMembership.objects.create(
        organization=organization,
        identity=identity,
        role=role,
    )


@pytest.mark.django_db
def test_set_member_role_changes_role_and_logs(
    f_organization: Organization,
    f_developer_role: Role,
    f_owner_role: Role,
) -> None:
    owner = _make_membership(f_organization, f_owner_role, external_id='owner-1')
    _make_membership(f_organization, f_owner_role, external_id='owner-2')

    with structlog.testing.capture_logs() as captured_logs:
        output = SetMemberRole(user=None, transaction=transaction.atomic()).execute(
            SetMemberRoleInput(membership=owner, role=f_developer_role),
        )

    assert output.membership.role_id == f_developer_role.id

    owner.refresh_from_db()

    assert owner.role_id == f_developer_role.id

    events = [entry for entry in captured_logs if entry['event'] == 'member_role_changed']

    assert len(events) == 1

    assert events[0]['organization_id'] == str(f_organization.id)

    assert events[0]['identity_id'] == str(owner.identity_id)

    assert events[0]['role'] == 'developer'


@pytest.mark.django_db
def test_set_member_role_raises_last_owner_error_when_demoting_last_owner(
    f_organization: Organization,
    f_developer_role: Role,
    f_owner_role: Role,
) -> None:
    owner = _make_membership(f_organization, f_owner_role, external_id='owner-only')

    with pytest.raises(LastOwnerError) as error:
        SetMemberRole(user=None, transaction=transaction.atomic()).execute(
            SetMemberRoleInput(membership=owner, role=f_developer_role),
        )

    assert error.value.error_code == 'last_owner'

    owner.refresh_from_db()

    assert owner.role_id == f_owner_role.id


@pytest.mark.django_db
def test_remove_member_deactivates_and_logs(
    f_organization: Organization,
    f_owner_role: Role,
) -> None:
    owner = _make_membership(f_organization, f_owner_role, external_id='owner-1')
    _make_membership(f_organization, f_owner_role, external_id='owner-2')

    with structlog.testing.capture_logs() as captured_logs:
        output = RemoveMember(user=None, transaction=transaction.atomic()).execute(
            RemoveMemberInput(membership=owner),
        )

    assert output.membership.active is False

    owner.refresh_from_db()

    assert owner.active is False

    events = [entry for entry in captured_logs if entry['event'] == 'member_removed']

    assert len(events) == 1

    assert events[0]['organization_id'] == str(f_organization.id)

    assert events[0]['identity_id'] == str(owner.identity_id)


@pytest.mark.django_db
def test_remove_member_raises_last_owner_error_when_removing_last_owner(
    f_organization: Organization,
    f_owner_role: Role,
) -> None:
    owner = _make_membership(f_organization, f_owner_role, external_id='owner-only')

    with pytest.raises(LastOwnerError) as error:
        RemoveMember(user=None, transaction=transaction.atomic()).execute(
            RemoveMemberInput(membership=owner),
        )

    assert error.value.error_code == 'last_owner'

    owner.refresh_from_db()

    assert owner.active is True
