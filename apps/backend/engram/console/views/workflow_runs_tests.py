from __future__ import annotations

import uuid
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
from engram.core.models import (
    AuditEvent,
    Memory,
    Organization,
    Project,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)


def _make_user(username: str = 'alice') -> User:
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
    capability, _ = Capability.objects.get_or_create(
        code=code,
        defaults={'description': code},
    )

    return capability


def _make_role_with_capabilities(
    code: str,
    capability_codes: tuple[str, ...],
) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    capabilities = [_ensure_capability(raw) for raw in capability_codes]

    for capability in capabilities:
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _client(token: str, org: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.fixture
def f_admin_client() -> APIClient:
    user = _make_user('workflow-admin')

    org = Organization.objects.create(name='Workflows', slug='workflows')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities(
        'workflow_admin',
        ('memories:read', 'memories:admin'),
    )

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    return _client(token, org)


@pytest.fixture
def f_admin_org(f_admin_client: APIClient) -> Organization:
    return Organization.objects.get(slug='workflows')


@pytest.fixture
def f_reader_client() -> APIClient:
    user = _make_user('workflow-reader')

    org = Organization.objects.create(name='Readers', slug='readers')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities('workflow_reader', ('memories:read',))

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    return _client(token, org)


@pytest.fixture
def f_reader_org(f_reader_client: APIClient) -> Organization:
    return Organization.objects.get(slug='readers')


def _make_project(organization: Organization, slug: str = 'backend') -> Project:
    return Project.objects.create(organization=organization, name=slug, slug=slug)


def _make_run(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    run_type: str = WorkflowRunType.DAILY_DIGEST,
    status: str = WorkflowRunStatus.SUCCEEDED,
    memory_ids: list[str] | None = None,
    provider_call_ids: list[str] | None = None,
    result_memory: Memory | None = None,
    escalation: bool = False,
    failure_reason: str = '',
    request_id: str = '',
    correlation_id: str = '',
) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization=organization,
        project=project,
        team=team,
        run_type=run_type,
        status=status,
        input_snapshot={
            'memory_ids': memory_ids or [],
            'window_days': 7,
        },
        provider_call_ids=provider_call_ids or [],
        result_memory=result_memory,
        escalation=escalation,
        failure_reason=failure_reason,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@pytest.mark.django_db
def test_list_returns_tenant_scoped_runs(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    _make_run(f_admin_org, project, request_id='visible')

    other_org = Organization.objects.create(name='Other', slug='other-org')

    other_project = _make_project(other_org, slug='other')

    _make_run(other_org, other_project, request_id='leaked')

    response = f_admin_client.get('/v1/admin/workflow-runs/')

    assert response.status_code == 200

    request_ids = [entry['request_id'] for entry in response.data['results']]

    assert 'visible' in request_ids

    assert 'leaked' not in request_ids


@pytest.mark.django_db
def test_list_filters_by_status_and_run_type(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    _make_run(f_admin_org, project, status=WorkflowRunStatus.SUCCEEDED, request_id='ok')

    _make_run(f_admin_org, project, status=WorkflowRunStatus.FAILED, request_id='bad')

    response = f_admin_client.get(
        '/v1/admin/workflow-runs/',
        {'status': WorkflowRunStatus.FAILED, 'run_type': WorkflowRunType.DAILY_DIGEST},
    )

    assert response.status_code == 200

    request_ids = [entry['request_id'] for entry in response.data['results']]

    assert request_ids == ['bad']


@pytest.mark.django_db
def test_list_filters_by_request_id(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    _make_run(f_admin_org, project, request_id='req-target', correlation_id='corr-a')

    _make_run(f_admin_org, project, request_id='req-other', correlation_id='corr-b')

    response = f_admin_client.get(
        '/v1/admin/workflow-runs/',
        {'request_id': 'req-target'},
    )

    assert response.status_code == 200

    request_ids = [entry['request_id'] for entry in response.data['results']]

    assert request_ids == ['req-target']


@pytest.mark.django_db
def test_list_filters_by_correlation_id(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    _make_run(f_admin_org, project, request_id='run-1', correlation_id='corr-target')

    _make_run(f_admin_org, project, request_id='run-2', correlation_id='corr-other')

    response = f_admin_client.get(
        '/v1/admin/workflow-runs/',
        {'correlation_id': 'corr-target'},
    )

    assert response.status_code == 200

    request_ids = [entry['request_id'] for entry in response.data['results']]

    assert request_ids == ['run-1']


@pytest.mark.django_db
def test_list_filters_by_project_team_and_escalation(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    team = Team.objects.create(organization=f_admin_org, name='Squad', slug='squad')

    other_project = _make_project(f_admin_org, slug='p2')

    _make_run(f_admin_org, other_project, request_id='other-project')

    _make_run(
        f_admin_org,
        project,
        team=team,
        escalation=True,
        request_id='escalated',
    )

    response = f_admin_client.get(
        '/v1/admin/workflow-runs/',
        {
            'project_id': str(project.id),
            'team_id': str(team.id),
            'escalation': 'true',
        },
    )

    assert response.status_code == 200

    request_ids = [entry['request_id'] for entry in response.data['results']]

    assert request_ids == ['escalated']


@pytest.mark.django_db
def test_retrieve_joins_inputs_curator_actions_and_provider_calls(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    source = Memory.objects.create(
        organization=f_admin_org,
        project=project,
        title='Source',
        body='Source body.',
        status='approved',
        visibility_scope=VisibilityScope.PROJECT,
    )

    run = _make_run(
        f_admin_org,
        project,
        memory_ids=[str(source.id)],
        request_id='join-run',
    )

    AuditEvent.objects.create(
        organization=f_admin_org,
        project=project,
        event_type='DigestGenerated',
        actor_type='api_key',
        target_type='memory',
        target_id=str(source.id),
        request_id='join-run',
    )

    response = f_admin_client.get(f'/v1/admin/workflow-runs/{run.id}/')

    assert response.status_code == 200

    data = response.data

    assert data['input_snapshot']['memory_ids'] == [str(source.id)]

    curator_types = [entry['event_type'] for entry in data['curator_actions']]

    assert curator_types == ['DigestGenerated']


@pytest.mark.django_db
def test_retrieve_returns_404_for_other_org_run(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    other_org = Organization.objects.create(name='Foreign', slug='foreign')

    other_project = _make_project(other_org, slug='fp')

    foreign_run = _make_run(other_org, other_project, request_id='foreign')

    response = f_admin_client.get(f'/v1/admin/workflow-runs/{foreign_run.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_rerun_daily_digest_creates_queued_run_and_dispatches_task(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    source_id = uuid.uuid4()

    run = _make_run(
        f_admin_org,
        project,
        memory_ids=[str(source_id)],
        request_id='original-run',
    )

    with patch('engram.console.views.workflow_runs.generate_daily_digest') as m_task:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 202

    new_run_id = response.data['run_id']

    assert new_run_id is not None

    assert response.data['status'] == WorkflowRunStatus.QUEUED

    new_run = WorkflowRun.objects.get(id=new_run_id)

    assert new_run.status == WorkflowRunStatus.QUEUED

    assert new_run.rerun_of_id == run.id

    assert new_run.run_type == WorkflowRunType.DAILY_DIGEST

    assert new_run.input_snapshot['memory_ids'] == [str(source_id)]

    m_task.delay.assert_called_once()

    args = m_task.delay.call_args[0]

    kwargs = m_task.delay.call_args[1]

    assert args[0] == str(f_admin_org.id)

    assert args[1] == str(project.id)

    assert args[2] == [str(source_id)]

    assert kwargs['workflow_run_id'] == str(new_run.id)


@pytest.mark.django_db
def test_rerun_daily_digest_never_executes_pipeline_inline(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(f_admin_org, project, memory_ids=[], request_id='inline-guard-run')

    with (
        patch('engram.console.views.workflow_runs.generate_daily_digest') as m_task,
        patch('engram.memory.services.GenerateDigest.execute') as m_pipeline,
    ):
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 202

    m_task.delay.assert_called_once()

    m_pipeline.assert_not_called()


@pytest.mark.django_db
def test_rerun_daily_digest_writes_audit_event_without_result_memory_id(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(f_admin_org, project, memory_ids=[], request_id='audit-run')

    with patch('engram.console.views.workflow_runs.generate_daily_digest'):
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    new_run_id = response.data['run_id']

    audit = AuditEvent.objects.get(
        organization=f_admin_org,
        event_type='WorkflowRunReran',
        target_id=str(run.id),
    )

    assert audit.metadata == {'new_run_id': new_run_id}


@pytest.mark.django_db
@pytest.mark.parametrize('running_status', [WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING])
def test_rerun_daily_digest_conflicts_with_active_run_for_project(
    f_admin_client: APIClient,
    f_admin_org: Organization,
    running_status: str,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(f_admin_org, project, memory_ids=[], request_id='conflict-original')

    WorkflowRun.objects.create(
        organization=f_admin_org,
        project=project,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=running_status,
    )

    with patch('engram.console.views.workflow_runs.generate_daily_digest') as m_task:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'daily_digest_already_running'

    m_task.delay.assert_not_called()


@pytest.mark.django_db
def test_rerun_dispatches_weekly_digest_for_weekly_run_type(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(
        f_admin_org,
        project,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        request_id='weekly-original',
    )

    with (
        patch('engram.console.views.workflow_runs.generate_weekly_digest') as m_weekly,
        patch('engram.console.views.workflow_runs.generate_daily_digest') as m_daily,
    ):
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 202

    new_run_id = response.data['run_id']

    new_run = WorkflowRun.objects.get(id=new_run_id)

    assert new_run.run_type == WorkflowRunType.WEEKLY_DIGEST

    assert new_run.status == WorkflowRunStatus.QUEUED

    assert new_run.rerun_of_id == run.id

    m_weekly.delay.assert_called_once()

    args = m_weekly.delay.call_args[0]

    kwargs = m_weekly.delay.call_args[1]

    assert args[0] == str(f_admin_org.id)

    assert args[1] == str(project.id)

    assert kwargs['workflow_run_id'] == str(new_run.id)

    m_daily.delay.assert_not_called()


@pytest.mark.django_db
def test_rerun_dispatches_session_distillation_for_session_run_type(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    session_id = uuid.uuid4()

    run = WorkflowRun.objects.create(
        organization=f_admin_org,
        project=project,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.SUCCEEDED,
        input_snapshot={'session_id': str(session_id)},
        request_id='distill-original',
    )

    with patch('engram.console.views.workflow_runs.distill_session') as m_distill:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 202

    new_run_id = response.data['run_id']

    new_run = WorkflowRun.objects.get(id=new_run_id)

    assert new_run.run_type == WorkflowRunType.SESSION_DISTILLATION

    assert new_run.status == WorkflowRunStatus.QUEUED

    assert new_run.rerun_of_id == run.id

    assert new_run.input_snapshot == {'session_id': str(session_id)}

    m_distill.delay.assert_called_once()

    args = m_distill.delay.call_args[0]

    kwargs = m_distill.delay.call_args[1]

    assert args[0] == str(session_id)

    assert kwargs['workflow_run_id'] == str(new_run.id)


@pytest.mark.django_db
def test_rerun_work_linked_session_distillation_returns_invalid_rerun_snapshot(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    from engram.core.models import (
        WorkflowSubjectType,
        WorkflowWork,
        WorkflowWorkType,
    )

    project = _make_project(f_admin_org)

    session_id = uuid.uuid4()

    work = WorkflowWork.objects.create(
        organization=f_admin_org,
        project=project,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        subject_type=WorkflowSubjectType.AGENT_SESSION,
        subject_id=session_id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint='0' * 64,
        input_snapshot={'session_id': str(session_id)},
    )

    run = WorkflowRun.objects.create(
        organization=f_admin_org,
        project=project,
        work=work,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.SUCCEEDED,
        input_snapshot={'session_id': str(session_id)},
        request_id='work-linked-original',
    )

    with patch('engram.console.views.workflow_runs.distill_session') as m_distill:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 400

    assert response.data['code'] == 'invalid_rerun_snapshot'

    assert response.data['error_code'] == 'invalid_rerun_snapshot'

    m_distill.delay.assert_not_called()

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.SESSION_DISTILLATION).count() == 1


@pytest.mark.django_db
def test_rerun_returns_400_for_unsupported_run_type(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(
        f_admin_org,
        project,
        run_type=WorkflowRunType.OBSERVATION_PROCESSING,
        request_id='unsupported-original',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 400

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
def test_rerun_denied_without_admin_capability(
    f_reader_client: APIClient,
    f_reader_org: Organization,
) -> None:
    project = _make_project(f_reader_org)

    run = _make_run(f_reader_org, project, request_id='protected')

    response = f_reader_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_rerun_daily_digest_invalid_memory_ids_returns_invalid_rerun_snapshot(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(
        f_admin_org,
        project,
        memory_ids=['not-a-uuid'],
        request_id='invalid-memory-ids',
    )

    with patch('engram.console.views.workflow_runs.generate_daily_digest') as m_task:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 400

    assert response.data['code'] == 'invalid_rerun_snapshot'

    assert response.data['error_code'] == 'invalid_rerun_snapshot'

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0

    m_task.delay.assert_not_called()

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 1


@pytest.mark.django_db
def test_rerun_session_distillation_invalid_session_id_returns_invalid_rerun_snapshot(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = WorkflowRun.objects.create(
        organization=f_admin_org,
        project=project,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.SUCCEEDED,
        input_snapshot={'session_id': 'not-a-uuid'},
        request_id='invalid-session-id',
    )

    with patch('engram.console.views.workflow_runs.distill_session') as m_distill:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 400

    assert response.data['code'] == 'invalid_rerun_snapshot'

    assert response.data['error_code'] == 'invalid_rerun_snapshot'

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0

    m_distill.delay.assert_not_called()

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.SESSION_DISTILLATION).count() == 1


@pytest.mark.django_db
def test_list_denied_without_read_capability() -> None:
    user = _make_user('no-cap')

    org = Organization.objects.create(name='Nocap', slug='nocap')

    identity = _make_identity(user, org)

    role, _ = Role.objects.get_or_create(code='no_caps', defaults={'name': 'no_caps'})

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    client = _client(token, org)

    response = client.get('/v1/admin/workflow-runs/')

    assert response.status_code == 403
