from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.context_api_tests import create_approved_memory_document, valid_context_payload
from engram.core.models import AuditEvent, Organization, Project, ProjectTeam, Team, VisibilityScope

RAW_KEY = 'egk_test_memory_feedback_0123456789abcdefghijklmnopqrstuvwxyz'
READ_ONLY_RAW_KEY = 'egk_test_memory_feedback_read_0123456789abcdefghijklmnopqrstuvwxyz'
PROJECT_RAW_KEY = 'egk_test_memory_feedback_project_0123456789abcdefghijklmnopqrstuvwxyz'


def create_project_scope() -> tuple[Organization, Team, Project, Identity, ApiKey]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )
    ProjectTeam.objects.create(organization=organization, team=team, project=project)
    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-memory-feedback',
        display_name='Memory feedback service account',
    )
    role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)
    api_key = create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        capabilities=('memories:review', 'memories:read'),
    )

    return organization, team, project, owner, api_key


def create_scoped_api_key(
    organization: Organization,
    team: Team | None,
    project: Project | None,
    owner: Identity,
    *,
    raw_key: str = RAW_KEY,
    capabilities: tuple[str, ...] = ('memories:review',),
) -> ApiKey:
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Memory feedback key',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        team=team,
        project=project,
    )
    for capability_code in capabilities:
        ApiKeyCapability.objects.create(
            api_key=api_key,
            capability=Capability.objects.get(code=capability_code),
        )

    return api_key


def auth_headers(raw_key: str = RAW_KEY) -> dict[str, str]:
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


def valid_feedback_payload(project: Project, team: Team, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'project_id': str(project.id),
        'team_id': str(team.id),
        'action': 'stale',
        'reason': f'No longer accurate after rotating {RAW_KEY}',
        'request_id': 'request-memory-feedback-1',
        'correlation_id': 'correlation-memory-feedback-1',
    }
    payload.update(overrides)

    return payload


@pytest.mark.django_db
def test_memory_feedback_stale_updates_memory_documents_and_audit() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    memory.refresh_from_db()
    document.refresh_from_db()
    assert memory.stale is True
    assert memory.refuted is False
    assert document.stale is True
    assert document.refuted is False
    audit = AuditEvent.objects.get(event_type='MemoryFeedbackRecorded')
    assert audit.capability == 'memories:review'
    assert audit.target_type == 'memory'
    assert audit.target_id == str(memory.id)
    assert audit.metadata['action'] == 'stale'
    assert RAW_KEY not in str(response.json())
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_memory_feedback_refuted_removes_memory_from_future_context() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            action='refuted',
            request_id='request-memory-feedback-refuted',
        ),
        format='json',
        **auth_headers(),
    )
    context_response = client.post(
        '/v1/context/session-start',
        valid_context_payload(
            project,
            team,
            request_id='request-memory-feedback-context',
        ),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    assert context_response.status_code == 200
    assert context_response.json()['items'] == []
    assert str(memory.id) not in str(context_response.json())


@pytest.mark.django_db
def test_memory_feedback_requires_memories_review_capability() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=READ_ONLY_RAW_KEY,
        capabilities=('memories:read',),
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, request_id='request-memory-feedback-missing-review'),
        format='json',
        **auth_headers(READ_ONLY_RAW_KEY),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
    assert memory.stale is False
    assert memory.refuted is False
    assert document.stale is False
    assert document.refuted is False
    assert AuditEvent.objects.filter(event_type='MemoryFeedbackRecorded').count() == 0


@pytest.mark.django_db
def test_project_bound_reviewer_can_mark_project_visible_memory_with_team() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(
        organization,
        team,
        project,
        visibility_scope=VisibilityScope.PROJECT,
    )
    create_scoped_api_key(
        organization,
        None,
        project,
        owner,
        raw_key=PROJECT_RAW_KEY,
        capabilities=('memories:review',),
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            team_id=None,
            request_id='request-memory-feedback-project-visible-team',
        ),
        format='json',
        **auth_headers(PROJECT_RAW_KEY),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 200
    assert memory.stale is True
    assert memory.refuted is False
    assert document.stale is True
    assert document.refuted is False


@pytest.mark.django_db
def test_memory_feedback_denies_wrong_project_without_mutating_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_project = Project.objects.create(
        organization=organization,
        name='Frontend',
        slug='frontend',
        repository_url='https://example.test/frontend.git',
        repository_root='/workspace/frontend',
    )
    ProjectTeam.objects.create(organization=organization, team=team, project=other_project)
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            project_id=str(other_project.id),
            request_id='request-memory-feedback-wrong-project',
        ),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    assert memory.stale is False
    assert memory.refuted is False
    assert document.stale is False
    assert document.refuted is False
    assert AuditEvent.objects.filter(event_type='MemoryFeedbackRecorded').count() == 0


@pytest.mark.django_db
def test_memory_feedback_rejects_oversized_reason_before_mutating_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            reason='x' * 2001,
            request_id='request-memory-feedback-oversized',
        ),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 400
    assert memory.stale is False
    assert memory.refuted is False
    assert document.stale is False
    assert document.refuted is False
    assert AuditEvent.objects.filter(event_type='MemoryFeedbackRecorded').count() == 0
