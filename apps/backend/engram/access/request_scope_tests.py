from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.context.context_api_tests import (
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
)
from engram.core.models import Organization, Project, ProjectTeam, Team


def _make_admin_session(organization: Organization) -> tuple[User, str]:
    username = f'admin-{organization.slug}'
    user = User.objects.create_user(username=username, password='admin-pass-123')  # noqa: S106
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )
    role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role)
    token = Token.objects.get_or_create(user=user)[0]

    return user, token.key


def _make_developer_session(organization: Organization) -> tuple[User, str]:
    username = f'dev-{organization.slug}'
    user = User.objects.create_user(username=username, password='dev-pass-123')  # noqa: S106
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )
    role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role)
    token = Token.objects.get_or_create(user=user)[0]

    return user, token.key


def _session_headers(token: str, org_slug: str) -> dict[str, str]:
    return {
        'HTTP_AUTHORIZATION': f'Token {token}',
        'HTTP_X_ENGRAM_ORGANIZATION': org_slug,
    }


@pytest.mark.django_db
def test_session_admin_can_list_secrets_with_token_auth() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200
    assert response.json() == {'count': 0, 'items': []}


@pytest.mark.django_db
def test_session_admin_can_list_policies_with_token_auth() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/policies',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200
    assert response.json() == {'count': 0, 'items': []}


@pytest.mark.django_db
def test_session_admin_can_list_inspection_memories() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_approved_memory_document(organization, None, project)
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200
    assert response.json()['count'] == 1


@pytest.mark.django_db
def test_session_admin_can_list_audit_events() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200
    assert 'items' in response.json()


@pytest.mark.django_db
def test_session_user_without_capability_is_denied_secrets() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_developer_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_session_user_without_capability_is_denied_inspection() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_developer_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_session_user_not_member_of_org_is_denied() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_org = Organization.objects.create(name='Other Org', slug='other-org')
    _user, token = _make_admin_session(other_org)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'not_a_member'


@pytest.mark.django_db
def test_session_scoped_to_org_a_cannot_read_org_b_project() -> None:
    organization_a, team_a, project_a, _owner_a, _api_key_a = create_project_scope()
    organization_b = Organization.objects.create(name='Org B', slug='org-b')
    team_b = Team.objects.create(organization=organization_b, name='Team B', slug='team-b')
    project_b = Project.objects.create(
        organization=organization_b,
        name='Project B',
        slug='project-b',
    )
    ProjectTeam.objects.create(organization=organization_b, team=team_b, project=project_b)
    _user_a, token_a = _make_admin_session(organization_a)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project_b.id)},
        **_session_headers(token_a, organization_a.slug),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_bearer_api_key_path_still_works_after_session_support_added() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_bearer_api_key_cross_tenant_access_still_denied() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    other_org = Organization.objects.create(name='External Corp', slug='external-corp')
    other_project = Project.objects.create(
        organization=other_org,
        name='External Project',
        slug='external-project',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(other_project.id)},
        **auth_headers(RAW_KEY),
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_missing_org_header_with_token_auth_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION=f'Token {token}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'organization_required'


@pytest.mark.django_db
def test_unauthenticated_token_header_returns_401() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    client = APIClient()

    response = client.get(
        '/v1/model-policy/secrets',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION='Token invalid-token-value',
        HTTP_X_ENGRAM_ORGANIZATION=organization.slug,
    )

    assert response.status_code == 401
