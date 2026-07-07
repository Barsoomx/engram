from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.authtoken.models import Token
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
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    Organization,
    Project,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='test-pass-123')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(
        code=code,
        defaults={'description': code},
    )

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for cap_code in capability_codes:
        capability = _ensure_capability(cap_code)
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_client(token: str, organization: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(organization.id),
    )

    return client


def _make_approved_memory(org: Organization, project: Project) -> Memory:
    return Memory.objects.create(
        organization=org,
        project=project,
        title='recent approved memory',
        body='body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )


def _endpoint(project_id: object) -> str:
    return f'/v1/admin/projects/{project_id}/digest/run'


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Digest Run Org', slug='digest-run-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_org,
        name='digest-run-project',
        slug='digest-run-project',
    )


@pytest.fixture
def f_other_org() -> Organization:
    return Organization.objects.create(name='Other Digest Run Org', slug='other-digest-run-org')


@pytest.fixture
def f_other_project(f_other_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_other_org,
        name='other-digest-run-project',
        slug='other-digest-run-project',
    )


@pytest.fixture
def f_admin_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-run-admin')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_run_admin_role', ('memories:admin',))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_no_cap_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-run-no-cap')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_run_no_cap_role', ('memories:read',))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.mark.django_db
def test_post_digest_run_enqueues_with_recent_memory_ids(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_approved_memory(f_org, f_project)

    with patch('engram.console.views.project_digest.generate_daily_digest') as m_task:
        response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 202

    assert response.data['enqueued'] is True

    m_task.delay.assert_called_once()

    args = m_task.delay.call_args[0]

    assert args[0] == str(f_org.id)

    assert args[1] == str(f_project.id)

    assert args[2] == [str(memory.id)]


@pytest.mark.django_db
def test_post_digest_run_returns_workflow_visibility_hint(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    with patch('engram.console.views.project_digest.generate_daily_digest'):
        response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 202

    workflow = response.data['workflow']

    assert workflow['run_type'] == WorkflowRunType.DAILY_DIGEST.value

    assert workflow['project_id'] == str(f_project.id)

    assert workflow['request_id'] == f'daily-digest:{f_project.id}'


@pytest.mark.django_db
def test_post_digest_run_writes_audit_event(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    with patch('engram.console.views.project_digest.generate_daily_digest'):
        f_admin_client.post(_endpoint(f_project.id))

    event = AuditEvent.objects.filter(
        organization=f_org,
        event_type='DailyDigestRunRequested',
        target_id=str(f_project.id),
    ).first()

    assert event is not None

    assert event.target_type == 'project'

    assert event.metadata['memory_count'] == 1


@pytest.mark.django_db
def test_post_digest_run_empty_window_does_not_enqueue(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    with patch('engram.console.views.project_digest.generate_daily_digest') as m_task:
        response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 200

    assert response.data['enqueued'] is False

    assert response.data['reason'] == 'no_recent_memories'

    m_task.delay.assert_not_called()


@pytest.mark.django_db
def test_post_digest_run_empty_window_writes_no_audit_event(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    with patch('engram.console.views.project_digest.generate_daily_digest'):
        f_admin_client.post(_endpoint(f_project.id))

    assert not AuditEvent.objects.filter(
        organization=f_org,
        event_type='DailyDigestRunRequested',
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('running_status', [WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING])
def test_post_digest_run_conflicts_when_run_in_flight(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    running_status: str,
) -> None:
    _make_approved_memory(f_org, f_project)

    WorkflowRun.objects.create(
        organization=f_org,
        project=f_project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=running_status,
    )

    with patch('engram.console.views.project_digest.generate_daily_digest') as m_task:
        response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 409

    assert response.data['code'] == 'daily_digest_already_running'

    m_task.delay.assert_not_called()


@pytest.mark.django_db
def test_post_digest_run_ignores_finished_runs_for_conflict(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    WorkflowRun.objects.create(
        organization=f_org,
        project=f_project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.SUCCEEDED,
        finished_at=timezone.now() - timedelta(hours=1),
    )

    _make_approved_memory(f_org, f_project)

    with patch('engram.console.views.project_digest.generate_daily_digest') as m_task:
        response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 202

    m_task.delay.assert_called_once()


@pytest.mark.django_db
def test_post_digest_run_requires_memories_admin_capability(
    f_no_cap_client: APIClient,
    f_project: Project,
) -> None:
    response = f_no_cap_client.post(_endpoint(f_project.id))

    assert response.status_code == 403


@pytest.mark.django_db
def test_post_digest_run_requires_authentication(
    f_project: Project,
) -> None:
    client = APIClient()

    response = client.post(_endpoint(f_project.id))

    assert response.status_code == 401


@pytest.mark.django_db
def test_post_digest_run_returns_404_for_unknown_project(
    f_admin_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_admin_client.post(_endpoint(uuid.uuid4()))

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_run_tenant_isolation(
    f_admin_client: APIClient,
    f_other_org: Organization,
    f_other_project: Project,
) -> None:
    _make_approved_memory(f_other_org, f_other_project)

    with patch('engram.console.views.project_digest.generate_daily_digest') as m_task:
        response = f_admin_client.post(_endpoint(f_other_project.id))

    assert response.status_code == 404

    m_task.delay.assert_not_called()
