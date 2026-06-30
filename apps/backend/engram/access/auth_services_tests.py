from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token

from engram.access.auth_services import (
    AuthError,
    GetCurrentUser,
    external_id_for_user,
    resolve_user_scope,
    resolve_user_scope_for_organization,
)
from engram.access.models import (
    Identity,
    IdentityType,
    MembershipStatus,
    OrganizationMembership,
    Role,
    TeamMembership,
)
from engram.core.models import Organization, Team


def _org_with_two_teams() -> tuple[Organization, Team, Team]:
    organization = Organization.objects.create(name='Acme', slug='acme')
    team_a = Team.objects.create(organization=organization, name='Team A', slug='team-a')
    team_b = Team.objects.create(organization=organization, name='Team B', slug='team-b')

    return organization, team_a, team_b


def _member(
    organization: Organization,
    *,
    role_code: str,
    external_id: str,
    identity_active: bool = True,
    membership_status: str = MembershipStatus.ACTIVE,
) -> tuple[User, Identity]:
    user = User.objects.create_user(username=f'{external_id}-{organization.slug}', password='pass-12345')  # noqa: S106
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        display_name=user.get_username(),
        active=identity_active,
    )
    role = Role.objects.get(code=role_code)
    OrganizationMembership.objects.create(
        organization=organization,
        identity=identity,
        role=role,
        status=membership_status,
    )

    return user, identity


@pytest.mark.django_db
def test_org_admin_session_scope_includes_all_org_teams_without_team_membership() -> None:
    organization, team_a, team_b = _org_with_two_teams()
    user, _identity = _member(organization, role_code='organization_admin', external_id='admin')

    scope = resolve_user_scope_for_organization(user, organization)

    assert set(scope.team_ids) == {team_a.id, team_b.id}


@pytest.mark.django_db
def test_developer_session_scope_limited_to_member_teams() -> None:
    organization, team_a, team_b = _org_with_two_teams()
    user, identity = _member(organization, role_code='developer', external_id='dev')
    role = Role.objects.get(code='developer')
    TeamMembership.objects.create(organization=organization, identity=identity, team=team_a, role=role)

    scope = resolve_user_scope_for_organization(user, organization)

    assert set(scope.team_ids) == {team_a.id}
    assert team_b.id not in scope.team_ids


@pytest.mark.django_db
def test_invited_membership_denies_org_scope() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(
        organization,
        role_code='organization_admin',
        external_id='invitee',
        membership_status=MembershipStatus.INVITED,
    )

    with pytest.raises(AuthError) as error_info:
        resolve_user_scope_for_organization(user, organization)

    assert error_info.value.code == 'membership_missing'


@pytest.mark.django_db
def test_suspended_membership_denies_org_scope() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(
        organization,
        role_code='organization_admin',
        external_id='suspendee',
        membership_status=MembershipStatus.SUSPENDED,
    )

    with pytest.raises(AuthError) as error_info:
        resolve_user_scope_for_organization(user, organization)

    assert error_info.value.code == 'membership_missing'


@pytest.mark.django_db
def test_invited_membership_denies_user_scope() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(
        organization,
        role_code='organization_admin',
        external_id='invitee-user-scope',
        membership_status=MembershipStatus.INVITED,
    )

    with pytest.raises(AuthError) as error_info:
        resolve_user_scope(user)

    assert error_info.value.code == 'membership_missing'


@pytest.mark.django_db
def test_invited_membership_has_no_capabilities_even_with_other_grants() -> None:
    organization, team_a, _team_b = _org_with_two_teams()
    user, identity = _member(
        organization,
        role_code='organization_admin',
        external_id='invitee-with-team',
        membership_status=MembershipStatus.INVITED,
    )
    role = Role.objects.get(code='developer')
    TeamMembership.objects.create(organization=organization, identity=identity, team=team_a, role=role)

    with pytest.raises(AuthError) as error_info:
        resolve_user_scope_for_organization(user, organization)

    assert error_info.value.code == 'membership_missing'


@pytest.mark.django_db
def test_active_membership_grants_org_scope() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(organization, role_code='organization_admin', external_id='active-member')

    scope = resolve_user_scope_for_organization(user, organization)

    assert 'projects:*' in scope.capabilities


@pytest.mark.django_db
def test_inactive_identity_denies_user_scope() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(
        organization,
        role_code='organization_admin',
        external_id='inactive-identity',
        identity_active=False,
    )

    with pytest.raises(AuthError) as error_info:
        resolve_user_scope(user)

    assert error_info.value.code == 'identity_missing'


@pytest.mark.django_db
def test_inactive_identity_denies_current_user_lookup() -> None:
    organization, _team_a, _team_b = _org_with_two_teams()
    user, _identity = _member(
        organization,
        role_code='organization_admin',
        external_id='inactive-identity-token',
        identity_active=False,
    )
    token = Token.objects.create(user=user)

    with pytest.raises(AuthError) as error_info:
        GetCurrentUser(token.key).execute()

    assert error_info.value.code == 'identity_missing'
