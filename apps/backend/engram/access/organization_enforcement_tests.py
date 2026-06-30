from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.request_scope_tests import _make_admin_session, _session_headers
from engram.context.context_api_tests import RAW_KEY, auth_headers, create_project_scope
from engram.core.models import OrganizationStatus


@pytest.mark.django_db
def test_bearer_denied_when_organization_suspended() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    organization.status = OrganizationStatus.SUSPENDED
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'organization_suspended'


@pytest.mark.django_db
def test_session_denied_when_organization_suspended() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    organization.status = OrganizationStatus.SUSPENDED
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'organization_suspended'


@pytest.mark.django_db
def test_console_denied_when_organization_suspended() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    organization.status = OrganizationStatus.SUSPENDED
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get('/v1/admin/projects/', **_session_headers(token, organization.slug))

    assert response.status_code == 403


@pytest.mark.django_db
def test_pending_delete_also_denied() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    organization.status = OrganizationStatus.PENDING_DELETE
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'organization_suspended'


@pytest.mark.django_db
def test_active_organization_is_not_blocked() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_past_due_is_grace_not_blocked() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    _user, token = _make_admin_session(organization)
    organization.status = OrganizationStatus.PAST_DUE
    organization.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **_session_headers(token, organization.slug),
    )

    assert response.status_code == 200
