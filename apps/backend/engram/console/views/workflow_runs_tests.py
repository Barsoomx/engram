from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
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
    TeamMembership,
)
from engram.core.models import (
    AuditEvent,
    Memory,
    Organization,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)

_DAILY_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'
_WEEKLY_TASK_NAME = 'engram.memory.generate_weekly_digest_work_v1'


def _make_linked_digest_work_and_run(
    org: Organization,
    project: Project,
    *,
    work_type: str,
    run_type: str,
    occurrence_key: str,
    run_status: str = WorkflowRunStatus.SUCCEEDED,
) -> tuple[WorkflowWork, WorkflowRun]:
    schema = 'daily_digest_input/v1' if work_type == WorkflowWorkType.DAILY_DIGEST else 'weekly_digest_input/v1'
    snapshot: dict[str, object] = {
        'schema': schema,
        'project_id': str(project.id),
        'schedule_key': occurrence_key,
        'input_digest': 'a' * 64,
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [],
        'output_visibility_scope': 'project',
        'output_team_id': None,
    }
    if work_type == WorkflowWorkType.DAILY_DIGEST:
        snapshot['sources'] = []
    else:
        snapshot['changes'] = []

    work = WorkflowWork.objects.create(
        organization=org,
        project=project,
        work_type=work_type,
        subject_type=WorkflowSubjectType.PROJECT,
        subject_id=project.id,
        contract_version=1,
        occurrence_key=occurrence_key,
        input_fingerprint='0' * 64,
        input_snapshot=snapshot,
        disposition=WorkflowWorkDisposition.COMPLETE,
        resolution_reason=WorkflowWorkResolutionReason.SUCCEEDED,
        resolved_at=timezone.now(),
    )
    terminal = run_status in (WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED)
    run = WorkflowRun.objects.create(
        organization=org,
        project=project,
        work=work,
        run_type=run_type,
        status=run_status,
        input_snapshot=snapshot,
        request_id='linked-original',
        finished_at=timezone.now() if terminal else None,
    )

    return work, run


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
        ('memories:read', 'memories:admin', 'projects:*', 'teams:*'),
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
def test_rerun_linked_daily_digest_emits_composite_id_only_task(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    work, original = _make_linked_digest_work_and_run(
        f_admin_org,
        project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:2026-07-10',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{original.id}/rerun/')

    assert response.status_code == 202

    assert response.data['status'] == WorkflowRunStatus.QUEUED

    new_run = WorkflowRun.objects.get(id=response.data['run_id'])

    assert new_run.status == WorkflowRunStatus.QUEUED

    assert new_run.work_id == work.id

    assert new_run.rerun_of_id == original.id

    assert new_run.run_type == WorkflowRunType.DAILY_DIGEST

    assert WorkflowWork.objects.count() == 1

    outbox = CeleryOutbox.objects.get()

    assert outbox.task_name == _DAILY_TASK_NAME

    assert outbox.args == [str(work.id), str(new_run.id)]

    assert outbox.kwargs == {}

    assert outbox.task_id == f'workflow-work:{work.id}:run:{new_run.id}'

    audit = AuditEvent.objects.get(
        organization=f_admin_org,
        event_type='WorkflowRunReran',
        target_id=str(original.id),
    )

    assert audit.metadata == {'new_run_id': str(new_run.id)}


@pytest.mark.django_db
def test_rerun_linked_daily_digest_never_executes_pipeline_inline(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    work, original = _make_linked_digest_work_and_run(
        f_admin_org,
        project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:2026-07-11',
    )

    with patch('engram.memory.services.GenerateDigest.execute') as m_pipeline:
        response = f_admin_client.post(f'/v1/admin/workflow-runs/{original.id}/rerun/')

    assert response.status_code == 202

    outbox = CeleryOutbox.objects.get()

    assert outbox.task_name == _DAILY_TASK_NAME

    assert outbox.args[0] == str(work.id)

    m_pipeline.assert_not_called()


@pytest.mark.django_db
def test_rerun_linked_daily_digest_writes_reran_audit_without_result_memory(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    work, original = _make_linked_digest_work_and_run(
        f_admin_org,
        project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:2026-07-12',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{original.id}/rerun/')

    new_run_id = response.data['run_id']

    assert WorkflowRun.objects.get(id=new_run_id).work_id == work.id

    audit = AuditEvent.objects.get(
        organization=f_admin_org,
        event_type='WorkflowRunReran',
        target_id=str(original.id),
    )

    assert audit.metadata == {'new_run_id': new_run_id}


@pytest.mark.django_db
def test_rerun_unlinked_daily_digest_returns_legacy_work_unlinked(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(
        f_admin_org,
        project,
        memory_ids=[str(uuid.uuid4())],
        request_id='unlinked-daily',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'legacy_work_unlinked'

    assert response.data['error_code'] == 'legacy_work_unlinked'

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 1

    assert CeleryOutbox.objects.count() == 0

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
def test_rerun_linked_weekly_digest_emits_composite_id_only_task(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    work, original = _make_linked_digest_work_and_run(
        f_admin_org,
        project,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        occurrence_key='weekly:2026-W28',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{original.id}/rerun/')

    assert response.status_code == 202

    new_run = WorkflowRun.objects.get(id=response.data['run_id'])

    assert new_run.run_type == WorkflowRunType.WEEKLY_DIGEST

    assert new_run.status == WorkflowRunStatus.QUEUED

    assert new_run.work_id == work.id

    assert new_run.rerun_of_id == original.id

    assert WorkflowWork.objects.count() == 1

    outbox = CeleryOutbox.objects.get()

    assert outbox.task_name == _WEEKLY_TASK_NAME

    assert outbox.args == [str(work.id), str(new_run.id)]

    assert outbox.kwargs == {}

    assert outbox.task_id == f'workflow-work:{work.id}:run:{new_run.id}'


@pytest.mark.django_db
def test_rerun_unlinked_weekly_digest_returns_legacy_work_unlinked(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)

    run = _make_run(
        f_admin_org,
        project,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        request_id='unlinked-weekly',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'legacy_work_unlinked'

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.WEEKLY_DIGEST).count() == 1

    assert CeleryOutbox.objects.count() == 0

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
def test_rerun_unlinked_session_distillation_returns_legacy_work_unlinked(
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
        request_id='unlinked-distill',
    )

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'legacy_work_unlinked'

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.SESSION_DISTILLATION).count() == 1

    assert CeleryOutbox.objects.count() == 0

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
def test_rerun_work_linked_session_distillation_returns_invalid_rerun_snapshot(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
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
def test_rerun_unlinked_daily_digest_rejects_before_snapshot_parse(
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

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'legacy_work_unlinked'

    assert response.data['error_code'] == 'legacy_work_unlinked'

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0

    assert CeleryOutbox.objects.count() == 0

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 1


@pytest.mark.django_db
def test_rerun_unlinked_session_distillation_rejects_before_snapshot_parse(
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

    response = f_admin_client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409

    assert response.data['code'] == 'legacy_work_unlinked'

    assert response.data['error_code'] == 'legacy_work_unlinked'

    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0

    assert CeleryOutbox.objects.count() == 0

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


def _scoped_admin_client(org: Organization, project: Project, username: str) -> APIClient:
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(f'{username}_role', ('memories:read', 'memories:admin'))
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    ProjectGrant.objects.create(organization=org, project=project, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    return _client(token, org)


def _make_unproven_digest_memory(org: Organization, project: Project, *, title: str) -> Memory:
    return Memory.objects.create(
        organization=org,
        project=project,
        title=title,
        body='digest body',
        status='approved',
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'kind': 'digest', 'digest_kind': 'weekly_structured'},
    )


@pytest.mark.django_db
def test_retrieve_masks_unproven_digest_result_memory_title(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)
    digest = _make_unproven_digest_memory(f_admin_org, project, title='Secret weekly digest title')
    run = _make_run(f_admin_org, project, result_memory=digest, request_id='digest-result')

    response = f_admin_client.get(f'/v1/admin/workflow-runs/{run.id}/')

    assert response.status_code == 200
    assert response.data['result_memory']['title'] != 'Secret weekly digest title'
    assert response.data['result_memory']['title'] in (None, 'digest_visibility_unproven')


@pytest.mark.django_db
def test_retrieve_keeps_non_digest_result_memory_title(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    project = _make_project(f_admin_org)
    memory = Memory.objects.create(
        organization=f_admin_org,
        project=project,
        title='Plain result title',
        body='plain body',
        status='approved',
        visibility_scope=VisibilityScope.PROJECT,
    )
    run = _make_run(f_admin_org, project, result_memory=memory, request_id='plain-result')

    response = f_admin_client.get(f'/v1/admin/workflow-runs/{run.id}/')

    assert response.status_code == 200
    assert response.data['result_memory']['title'] == 'Plain result title'


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('work_type', 'run_type', 'occurrence_key'),
    [
        (WorkflowWorkType.DAILY_DIGEST, WorkflowRunType.DAILY_DIGEST, 'daily:2026-07-10'),
        (WorkflowWorkType.WEEKLY_DIGEST, WorkflowRunType.WEEKLY_DIGEST, 'weekly:2026-W28'),
    ],
)
def test_rerun_non_terminal_digest_run_conflicts_without_writing(
    work_type: str,
    run_type: str,
    occurrence_key: str,
) -> None:
    org = Organization.objects.create(name='Scoped Rerun', slug=f'scoped-rerun-{run_type}')
    project = _make_project(org, slug=f'scoped-{run_type}')
    client = _scoped_admin_client(org, project, f'scoped-admin-{run_type}')

    _work, run = _make_linked_digest_work_and_run(
        org,
        project,
        work_type=work_type,
        run_type=run_type,
        occurrence_key=occurrence_key,
        run_status=WorkflowRunStatus.QUEUED,
    )

    response = client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 409
    assert WorkflowRun.objects.filter(run_type=run_type).count() == 1
    assert CeleryOutbox.objects.count() == 0
    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
def test_rerun_daily_digest_active_run_collision_maps_to_409() -> None:
    org = Organization.objects.create(name='Daily Collision', slug='daily-collision')
    project = _make_project(org, slug='daily-collision')
    client = _scoped_admin_client(org, project, 'daily-collision-admin')

    work, original = _make_linked_digest_work_and_run(
        org,
        project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:2026-07-10',
        run_status=WorkflowRunStatus.SUCCEEDED,
    )
    WorkflowRun.objects.create(
        organization=org,
        project=project,
        work=work,
        run_type=WorkflowRunType.DAILY_DIGEST,
        status=WorkflowRunStatus.QUEUED,
        request_id='separate-active',
    )

    response = client.post(f'/v1/admin/workflow-runs/{original.id}/rerun/')

    assert response.status_code == 409
    assert response.data['code'] == 'daily_digest_already_running'
    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.DAILY_DIGEST).count() == 2


@pytest.mark.django_db
def test_rerun_out_of_scope_run_returns_404() -> None:
    org = Organization.objects.create(name='Scope Guard', slug='scope-guard')
    granted_project = _make_project(org, slug='granted')
    other_project = _make_project(org, slug='ungranted')
    client = _scoped_admin_client(org, granted_project, 'scope-guard-admin')

    _work, run = _make_linked_digest_work_and_run(
        org,
        other_project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:2026-07-10',
        run_status=WorkflowRunStatus.SUCCEEDED,
    )

    response = client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 404
    assert CeleryOutbox.objects.count() == 0
    assert WorkflowRun.objects.filter(work=_work).count() == 1


def _team_scoped_admin_client(
    org: Organization,
    team: Team,
    username: str,
) -> APIClient:
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(
        f'{username}_role',
        ('memories:read', 'memories:admin'),
    )
    OrganizationMembership.objects.create(
        organization=org,
        identity=identity,
        role=role,
    )
    TeamMembership.objects.create(
        organization=org,
        team=team,
        identity=identity,
        role=role,
    )

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    return _client(token, org)


@pytest.mark.django_db
@pytest.mark.parametrize('operation', ('list', 'retrieve'))
def test_workflow_run_reads_respect_effective_project_scope(
    operation: str,
) -> None:
    org = Organization.objects.create(name='Read Scope', slug=f'read-scope-{operation}')
    granted_project = _make_project(org, slug=f'granted-{operation}')
    foreign_project = _make_project(org, slug=f'foreign-{operation}')
    client = _scoped_admin_client(
        org,
        granted_project,
        f'read-scope-{operation}',
    )
    run = _make_run(
        org,
        foreign_project,
        request_id='foreign-project',
    )

    if operation == 'list':
        response = client.get('/v1/admin/workflow-runs/')

        assert response.status_code == 200
        assert response.data['results'] == []
    else:
        response = client.get(f'/v1/admin/workflow-runs/{run.id}/')

        assert response.status_code == 404


@pytest.mark.django_db
def test_rerun_empty_effective_project_scope_returns_404_without_writes() -> None:
    org = Organization.objects.create(name='Empty Scope', slug='empty-scope')
    project = _make_project(org, slug='ungranted')
    user = _make_user('empty-scope-admin')
    identity = _make_identity(user, org)
    role = _make_role_with_capabilities(
        'empty_scope_admin_role',
        ('memories:read', 'memories:admin'),
    )
    OrganizationMembership.objects.create(
        organization=org,
        identity=identity,
        role=role,
    )

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key
    client = _client(token, org)
    work, run = _make_linked_digest_work_and_run(
        org,
        project,
        work_type=WorkflowWorkType.DAILY_DIGEST,
        run_type=WorkflowRunType.DAILY_DIGEST,
        occurrence_key='daily:empty-scope',
    )

    response = client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 404
    assert WorkflowRun.objects.filter(work=work).count() == 1
    assert CeleryOutbox.objects.count() == 0
    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('operation', ('list', 'retrieve', 'rerun'))
def test_workflow_run_endpoints_respect_effective_team_scope(
    operation: str,
) -> None:
    org = Organization.objects.create(name='Team Scope', slug=f'team-scope-{operation}')
    project = _make_project(org, slug=f'shared-{operation}')
    allowed_team = Team.objects.create(
        organization=org,
        name='Allowed',
        slug=f'allowed-{operation}',
    )
    foreign_team = Team.objects.create(
        organization=org,
        name='Foreign',
        slug=f'foreign-{operation}',
    )
    ProjectTeam.objects.create(
        organization=org,
        project=project,
        team=allowed_team,
    )
    ProjectTeam.objects.create(
        organization=org,
        project=project,
        team=foreign_team,
    )
    client = _team_scoped_admin_client(
        org,
        allowed_team,
        f'team-scope-{operation}',
    )
    occurrence_key = f'weekly:team-scope:{operation}'
    snapshot: dict[str, object] = {
        'schema': 'weekly_digest_input/v1',
        'project_id': str(project.id),
        'team_id': str(foreign_team.id),
        'schedule_key': occurrence_key,
        'input_digest': 'a' * 64,
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [str(foreign_team.id)],
        'output_visibility_scope': 'team',
        'output_team_id': str(foreign_team.id),
        'changes': [],
    }
    work = WorkflowWork.objects.create(
        organization=org,
        project=project,
        team=foreign_team,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        subject_type=WorkflowSubjectType.TEAM,
        subject_id=foreign_team.id,
        contract_version=1,
        occurrence_key=occurrence_key,
        input_fingerprint='0' * 64,
        input_snapshot=snapshot,
        disposition=WorkflowWorkDisposition.COMPLETE,
        resolution_reason=WorkflowWorkResolutionReason.SUCCEEDED,
        resolved_at=timezone.now(),
    )
    run = WorkflowRun.objects.create(
        organization=org,
        project=project,
        team=foreign_team,
        work=work,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        status=WorkflowRunStatus.SUCCEEDED,
        input_snapshot=snapshot,
        request_id='foreign-team',
        finished_at=timezone.now(),
    )

    if operation == 'list':
        response = client.get('/v1/admin/workflow-runs/')

        assert response.status_code == 200
        assert response.data['results'] == []
    elif operation == 'retrieve':
        response = client.get(f'/v1/admin/workflow-runs/{run.id}/')

        assert response.status_code == 404
    else:
        response = client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

        assert response.status_code == 404

    assert WorkflowRun.objects.filter(work=work).count() == 1
    assert CeleryOutbox.objects.count() == 0
    assert AuditEvent.objects.filter(target_id=str(run.id)).count() == 0
