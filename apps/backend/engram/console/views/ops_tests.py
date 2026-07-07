from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
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
    AuditResult,
    CandidateStatus,
    MemoryCandidate,
    Organization,
    Project,
    Team,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret


def _make_project(org: Organization) -> Project:
    return Project.objects.create(organization=org, name='Proj', slug=f'proj-{Project.objects.count()}')


def _make_proposed_candidate(org: Organization, project: Project, *, created_at: object = None) -> MemoryCandidate:
    counter = MemoryCandidate.objects.count()
    candidate = MemoryCandidate.objects.create(
        organization=org,
        project=project,
        title=f'Candidate {counter}',
        body=f'Body {counter}',
        status=CandidateStatus.PROPOSED,
        content_hash=f'hash-c-{counter}',
        confidence='0.300',
    )
    if created_at is not None:
        MemoryCandidate.objects.filter(id=candidate.id).update(created_at=created_at)
        candidate.refresh_from_db()

    return candidate


def _make_provider_error(org: Organization, project: Project, *, created_at: object = None) -> ProviderCallRecord:
    counter = ProviderCallRecord.objects.count()
    team = Team.objects.create(organization=org, name=f'Team {counter}', slug=f'team-{counter}')
    secret = ProviderSecret.objects.create(
        organization=org,
        team=team,
        name='OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    policy = ModelPolicy.objects.create(
        organization=org,
        team=team,
        project=project,
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )
    record = ProviderCallRecord.objects.create(
        organization=org,
        project=project,
        team=team,
        policy=policy,
        secret=secret,
        provider='openai',
        model='gpt-4.1-mini',
        task_type='generation',
        policy_version=1,
        request_id=f'req-{counter}',
        redaction_state='clean',
        result=AuditResult.ERROR,
    )
    if created_at is not None:
        ProviderCallRecord.objects.filter(id=record.id).update(created_at=created_at)
        record.refresh_from_db()

    return record


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
def test_ops_overview_exposes_review_backlog_gauges(f_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='ops-org')
    project = _make_project(org)
    _make_proposed_candidate(org, project, created_at=timezone.now() - timedelta(hours=2))
    _make_proposed_candidate(org, project)

    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert body['review_backlog_count'] == 2
    assert isinstance(body['oldest_proposed_age_seconds'], int)
    assert body['oldest_proposed_age_seconds'] >= 7000
    assert body['provider_errors_24h'] == 0


@pytest.mark.django_db
def test_ops_overview_review_gauges_are_empty_without_backlog(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert body['review_backlog_count'] == 0
    assert body['oldest_proposed_age_seconds'] is None
    assert body['provider_errors_24h'] == 0


@pytest.mark.django_db
def test_ops_overview_review_gauges_scoped_to_active_organization(f_admin_client: APIClient) -> None:
    org = Organization.objects.get(slug='ops-org')
    project = _make_project(org)
    other_org = Organization.objects.create(name='OtherOrg', slug='other-org')
    other_project = _make_project(other_org)

    _make_proposed_candidate(org, project)
    _make_proposed_candidate(other_org, other_project)

    _make_provider_error(org, project, created_at=timezone.now() - timedelta(hours=1))
    _make_provider_error(org, project, created_at=timezone.now() - timedelta(hours=30))
    _make_provider_error(other_org, other_project, created_at=timezone.now() - timedelta(hours=1))

    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert body['review_backlog_count'] == 1
    assert body['provider_errors_24h'] == 1


@pytest.mark.django_db
def test_ops_overview_requires_memories_admin(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/ops/overview')

    assert response.status_code == 403


@pytest.mark.django_db
def test_ops_overview_requires_authentication() -> None:
    client = APIClient()

    response = client.get('/v1/admin/ops/overview')

    assert response.status_code == 401


@pytest.mark.django_db
@override_settings(ENGRAM_OPS_GLOBAL_COUNTERS=True)
def test_ops_overview_includes_global_counters_when_enabled(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert 'outbox_backlog_count' in body
    assert 'outbox_oldest_age_seconds' in body
    assert 'dead_letter_count' in body
    assert 'failed_workflow_runs' in body
    assert 'pending_embedding_count' in body


@pytest.mark.django_db
@override_settings(ENGRAM_OPS_GLOBAL_COUNTERS=False)
def test_ops_overview_omits_global_counters_when_disabled(f_admin_client: APIClient) -> None:
    response = f_admin_client.get('/v1/admin/ops/overview')

    assert response.status_code == 200
    body = response.json()
    assert 'outbox_backlog_count' not in body
    assert 'outbox_oldest_age_seconds' not in body
    assert 'dead_letter_count' not in body
    assert 'failed_workflow_runs' in body
    assert 'pending_embedding_count' in body
