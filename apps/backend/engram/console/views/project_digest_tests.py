from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.core.models import (
    AuditEvent,
    Memory,
    Organization,
    Project,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.transitions import PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request

_DAILY_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'


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
    candidate, _source, _session = provenanced_candidate_in_scope(
        org,
        project,
        None,
        suffix=f'project-digest-{project.id}-{uuid.uuid4().hex}',
        title='recent approved memory',
        body='body',
        visibility_scope=VisibilityScope.PROJECT,
    )
    memory = PromoteMemoryCandidate().execute(transition_request(candidate)).memory
    Memory.objects.filter(id=memory.id).update(updated_at=timezone.now() - timedelta(hours=1))

    return memory


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
def f_admin_client(f_org: Organization, f_project: Project) -> APIClient:
    user = _make_user('digest-run-admin')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_run_admin_role', ('memories:admin',))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    ProjectGrant.objects.create(organization=f_org, project=f_project, identity=identity, role=role)
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
def test_post_digest_run_creates_work_linked_run_and_composite_package(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 202

    assert response.data['enqueued'] is True

    work = WorkflowWork.objects.get(
        organization=f_org,
        project=f_project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
    )

    assert work.disposition == WorkflowWorkDisposition.REQUIRED

    assert work.occurrence_key != ''

    run = WorkflowRun.objects.get(work=work)

    assert run.status == WorkflowRunStatus.QUEUED

    assert run.run_type == WorkflowRunType.DAILY_DIGEST

    outbox = CeleryOutbox.objects.get(task_name=_DAILY_TASK_NAME)

    assert outbox.task_name == _DAILY_TASK_NAME

    assert outbox.args == [str(work.id), str(run.id)]

    assert outbox.kwargs == {}

    assert outbox.task_id == f'workflow-work:{work.id}:run:{run.id}'


@pytest.mark.django_db
def test_post_digest_run_writes_audit_event(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    f_admin_client.post(_endpoint(f_project.id))

    event = AuditEvent.objects.filter(
        organization=f_org,
        event_type='DailyDigestRunRequested',
        target_id=str(f_project.id),
    ).first()

    assert event is not None

    assert event.target_type == 'project'


@pytest.mark.django_db
def test_post_digest_run_reuses_work_across_manual_requests_with_distinct_runs(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    first = f_admin_client.post(_endpoint(f_project.id))

    assert first.status_code == 202

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)

    run_one = WorkflowRun.objects.get(work=work)

    WorkflowRun.objects.filter(id=run_one.id).update(
        status=WorkflowRunStatus.SUCCEEDED,
        fencing_token=1,
        lease_owner='manual-digest-test',
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )

    second = f_admin_client.post(_endpoint(f_project.id))

    assert second.status_code == 202

    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.DAILY_DIGEST).count() == 1

    runs = WorkflowRun.objects.filter(work=work)

    assert runs.count() == 2

    run_two = runs.exclude(id=run_one.id).get()

    assert run_two.id != run_one.id

    assert CeleryOutbox.objects.filter(task_name=_DAILY_TASK_NAME).count() == 2

    outbox_two = CeleryOutbox.objects.get(task_id=f'workflow-work:{work.id}:run:{run_two.id}')

    assert outbox_two.task_name == _DAILY_TASK_NAME

    assert outbox_two.args == [str(work.id), str(run_two.id)]


@pytest.mark.django_db
def test_post_digest_run_active_run_conflict_creates_no_rows(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_approved_memory(f_org, f_project)

    first = f_admin_client.post(_endpoint(f_project.id))

    assert first.status_code == 202

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)

    second = f_admin_client.post(_endpoint(f_project.id))

    assert second.status_code == 409

    assert second.data['code'] == 'daily_digest_already_running'

    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.DAILY_DIGEST).count() == 1

    assert WorkflowRun.objects.filter(work=work).count() == 1

    assert CeleryOutbox.objects.filter(task_name=_DAILY_TASK_NAME).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('running_status', [WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING])
def test_post_digest_run_conflicts_with_preexisting_active_run(
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

    response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 409

    assert response.data['code'] == 'daily_digest_already_running'

    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.DAILY_DIGEST).count() == 0

    assert CeleryOutbox.objects.filter(task_name=_DAILY_TASK_NAME).count() == 0


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

    response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 202

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)

    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 1


@pytest.mark.django_db
def test_post_digest_run_empty_input_creates_terminal_no_input_work_without_package(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 200

    assert response.data['enqueued'] is False

    assert response.data['reason'] == 'no_recent_memories'

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)

    assert work.disposition == WorkflowWorkDisposition.NO_OP

    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 0

    assert CeleryOutbox.objects.filter(task_name=_DAILY_TASK_NAME).count() == 0

    assert not AuditEvent.objects.filter(
        organization=f_org,
        event_type='DailyDigestRunRequested',
    ).exists()


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

    response = f_admin_client.post(_endpoint(f_other_project.id))

    assert response.status_code == 404

    assert WorkflowWork.objects.filter(project=f_other_project, work_type=WorkflowWorkType.DAILY_DIGEST).count() == 0

    assert CeleryOutbox.objects.filter(task_name=_DAILY_TASK_NAME).count() == 0


@pytest.mark.django_db
def test_post_digest_run_reports_already_built_when_occurrence_complete(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    from engram.memory.workflow_work import resolve_work_succeeded

    _make_approved_memory(f_org, f_project)

    first = f_admin_client.post(_endpoint(f_project.id))

    assert first.status_code == 202

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)
    run = WorkflowRun.objects.get(work=work)
    resolve_work_succeeded(work.id, organization_id=f_org.id, project_id=f_project.id)
    WorkflowRun.objects.filter(id=run.id).update(
        status=WorkflowRunStatus.SUCCEEDED,
        fencing_token=1,
        lease_owner='manual-digest-test',
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )

    second = f_admin_client.post(_endpoint(f_project.id))

    assert second.status_code == 200
    assert second.data['enqueued'] is False
    assert second.data['reason'] == 'already_built'
    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.DAILY_DIGEST).count() == 1


@pytest.mark.django_db
def test_post_digest_run_active_run_integrity_error_maps_to_409(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_approved_memory(f_org, f_project)

    WorkflowRun.objects.create(
        organization=f_org,
        project=f_project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.QUEUED,
    )

    monkeypatch.setattr(
        'engram.console.views.project_digest._has_active_daily_digest_run',
        lambda _organization, _project: False,
    )

    response = f_admin_client.post(_endpoint(f_project.id))

    assert response.status_code == 409
    assert response.data['code'] == 'daily_digest_already_running'
    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 1
