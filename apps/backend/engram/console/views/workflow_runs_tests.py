from __future__ import annotations

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
def test_rerun_creates_chained_run_triggers_digest_and_audits(
    f_admin_client: APIClient,
    f_admin_org: Organization,
) -> None:
    from engram.context.context_api_tests import create_project_scope
    from engram.memory.memory_digest_tests import create_digest_policy, create_source_memory

    org, team, project, _owner, _api_key = create_project_scope()

    user = _make_user('rerun-admin')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities(
        'rerun_admin',
        ('memories:read', 'memories:admin'),
    )

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key

    client = _client(token, org)

    create_digest_policy(org, team, project)

    source = create_source_memory(org, team, project, title='Rerun source')

    run = _make_run(
        org,
        project,
        memory_ids=[str(source.id)],
        request_id='original-run',
    )

    response = client.post(f'/v1/admin/workflow-runs/{run.id}/rerun/')

    assert response.status_code == 200

    new_run_id = response.data['run_id']

    assert new_run_id is not None

    new_run = WorkflowRun.objects.get(id=new_run_id)

    assert new_run.status == WorkflowRunStatus.SUCCEEDED

    assert new_run.rerun_of_id == run.id

    assert new_run.result_memory_id is not None

    audit = AuditEvent.objects.filter(
        organization=org,
        event_type='WorkflowRunReran',
        target_id=str(run.id),
    )

    assert audit.count() == 1


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
