from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
    RoleCapability,
)
from engram.core.models import Organization, Project, WorkflowRun, WorkflowRunStatus, WorkflowRunType


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
    for cap_code in capability_codes:
        RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(cap_code))

    return role


def _client_for_org(username: str, org: Organization, capabilities: tuple[str, ...]) -> APIClient:
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(f'role_{username}', capabilities)
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.fixture
def f_admin_client() -> APIClient:
    org = Organization.objects.create(name='OpsOrg', slug='ops-org')

    return _client_for_org('ops-admin', org, ('memories:admin',))


@pytest.fixture
def f_reader_client() -> APIClient:
    org = Organization.objects.create(name='OpsReader', slug='ops-reader')

    return _client_for_org('ops-reader', org, ('memories:read',))


@pytest.mark.django_db
def test_ops_overview_returns_expected_shape(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert 'outbox_backlog_count' in body
    assert 'outbox_oldest_age_seconds' in body
    assert 'dead_letter_count' in body
    assert 'failed_workflow_runs' in body
    assert 'pending_embedding_count' in body
    assert isinstance(body['outbox_backlog_count'], int)
    assert isinstance(body['dead_letter_count'], int)
    assert isinstance(body['failed_workflow_runs'], int)
    assert isinstance(body['pending_embedding_count'], int)


@pytest.mark.django_db
def test_ops_overview_counts_failed_workflow_runs(f_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='ops-org')
    project = Project.objects.create(organization=org, name='Proj', slug='proj-ops')
    WorkflowRun.objects.create(
        organization=org,
        project=project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.FAILED,
        failure_reason='test failure',
    )
    WorkflowRun.objects.create(
        organization=org,
        project=project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.SUCCEEDED,
    )

    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    assert response.json()['failed_workflow_runs'] >= 1


@pytest.mark.django_db
def test_ops_overview_scopes_counts_to_active_organization(f_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='ops-org')

    with (
        patch('engram.console.views.ops.WorkflowRun') as m_workflow_run,
        patch('engram.console.views.ops.RetrievalDocument') as m_retrieval_document,
    ):
        m_workflow_run.objects.filter.return_value.count.return_value = 2
        m_retrieval_document.objects.filter.return_value.count.return_value = 5

        response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    m_workflow_run.objects.filter.assert_called_once_with(status=WorkflowRunStatus.FAILED, organization=org)
    m_retrieval_document.objects.filter.assert_called_once_with(embedding_pgvector__isnull=True, organization=org)
    assert response.json()['failed_workflow_runs'] == 2
    assert response.json()['pending_embedding_count'] == 5


@pytest.mark.django_db
def test_ops_overview_does_not_count_other_organizations_failed_workflow_runs(f_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='ops-org')
    project = Project.objects.create(organization=org, name='Proj', slug='proj-ops')
    other_org = Organization.objects.create(name='OtherOrg', slug='other-org')
    other_project = Project.objects.create(organization=other_org, name='OtherProj', slug='proj-other')
    WorkflowRun.objects.create(
        organization=other_org,
        project=other_project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.FAILED,
        failure_reason='other org failure',
    )

    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    assert response.json()['failed_workflow_runs'] == 0

    WorkflowRun.objects.create(
        organization=org,
        project=project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.FAILED,
        failure_reason='own org failure',
    )

    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    assert response.json()['failed_workflow_runs'] == 1


@pytest.mark.django_db
def test_ops_overview_requires_memories_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/ops/overview')

    assert response.status_code == 403


@pytest.mark.django_db
def test_ops_overview_requires_authentication() -> None:
    client = APIClient()

    response = client.get('/v1/admin/ops/overview')

    assert response.status_code == 401
