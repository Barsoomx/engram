from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import structlog
from django.utils import timezone
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.access.services import EffectiveScope, api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.context_api_tests import valid_context_payload
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryConflict,
    MemoryConflictResolution,
    MemoryStatus,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.inspection.services import InspectionScope, ListInspectionAuditEvents
from engram.memory.transitions import (
    OpenMemoryConflict,
    OpenMemoryConflictInput,
    PromoteMemoryCandidate,
    build_memory_fence,
)
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    candidate_in_scope,
    provenanced_candidate_in_scope,
    transition_request,
    transition_request_for,
)

RAW_KEY = 'egk_test_memory_feedback_0123456789abcdefghijklmnopqrstuvwxyz'
READ_ONLY_RAW_KEY = 'egk_test_memory_feedback_read_0123456789abcdefghijklmnopqrstuvwxyz'
PROJECT_RAW_KEY = 'egk_test_memory_feedback_project_0123456789abcdefghijklmnopqrstuvwxyz'
SECOND_RAW_KEY = 'egk_test_memory_feedback_second_0123456789abcdefghijklmnopqrstuv'
AGENT_RAW_KEY = 'egk_test_memory_feedback_agent_0123456789abcdefghijklmnopqrstuv'
AGENT_CAPS = ('memories:review', 'memories:read', 'projects:agent')


def _reader_scope(organization: Organization, project: Project, team_ids: tuple) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=organization.id,
        api_key_id=organization.id,
        project_ids=(project.id,),
        team_ids=team_ids,
        capabilities=('memories:review',),
        actor_type='api_key',
        actor_id=str(organization.id),
        project_bound=False,
    )


def _confirmed_events_for_reader(
    organization: Organization,
    project: Project,
    team_ids: tuple,
    memory: Memory,
) -> list[AuditEvent]:
    inspection_scope = InspectionScope(project=project, scope=_reader_scope(organization, project, team_ids))
    return [
        event
        for event in ListInspectionAuditEvents().execute(inspection_scope)
        if event.event_type == 'MemoryConfirmed' and event.target_id == str(memory.id)
    ]


def create_org_agent_key(organization: Organization) -> None:
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='memory-feedback-agent',
        display_name='Memory feedback agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='memory feedback agent key',
        key_prefix=api_key_prefix(AGENT_RAW_KEY),
        key_hash=hash_api_key(AGENT_RAW_KEY),
        key_fingerprint=api_key_fingerprint(AGENT_RAW_KEY),
        active=True,
    )
    for code in AGENT_CAPS:
        ApiKeyCapability.objects.get_or_create(
            api_key=api_key,
            capability=Capability.objects.get(code=code),
        )


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


def create_approved_memory_document(
    organization: Organization,
    team: Team | None,
    project: Project,
    *,
    title: str = 'Authorization before ranking',
    body: str = 'Authorization before ranking protects context bundles.',
    visibility_scope: str = VisibilityScope.PROJECT,
) -> tuple[Memory, MemoryVersion, RetrievalDocument]:
    candidate, _source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='memory-feedback',
        title=title,
        body=body,
        visibility_scope=visibility_scope,
    )
    result = PromoteMemoryCandidate().execute(transition_request(candidate))

    return result.memory, result.memory_version, result.retrieval_document


def create_approved_memory_with_open_conflict(
    organization: Organization,
    team: Team,
    project: Project,
) -> tuple[Memory, MemoryConflict]:
    base_candidate, source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='confirm-conflict',
        title='Conflicted memory',
        body='Conflicted memory body.',
    )
    memory_result = PromoteMemoryCandidate().execute(transition_request(base_candidate))
    compared, _compared_source = candidate_in_scope(
        base_candidate,
        source,
        title='Compared conflict candidate',
        body='Compared conflict candidate body.',
    )
    conflict = OpenMemoryConflict().execute(
        OpenMemoryConflictInput(
            request=transition_request_for(compared, key=f'request:{uuid.uuid4()}:conflict-open:{compared.id}:v1'),
            candidate_fence=candidate_fence_for(compared),
            memory_fence=build_memory_fence(memory_result.memory),
            evidence_hash='e' * 64,
            redacted_reason='conflicting evidence',
        ),
    )

    return memory_result.memory, MemoryConflict.objects.get(id=conflict.id)


def make_document_active_project_visible(memory: Memory) -> None:
    version = MemoryVersion.objects.filter(memory=memory, version=memory.current_version).first()
    document = RetrievalDocument.objects.filter(memory=memory).first()
    if document is not None:
        RetrievalDocument.objects.filter(id=document.id).update(
            stale=False,
            refuted=False,
            visibility_scope=VisibilityScope.PROJECT,
            organization=memory.organization,
            project=memory.project,
        )

        return

    RetrievalDocument.objects.create(
        organization=memory.organization,
        project=memory.project,
        team=memory.team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        full_text=memory.body,
    )


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
    audit = AuditEvent.objects.get(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-1',
    )
    assert audit.capability == 'memories:review'
    assert audit.target_type == 'memory'
    assert audit.target_id == str(memory.id)
    assert audit.metadata['transition_type'] == 'mark_stale'
    assert RAW_KEY not in str(response.json())
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_memory_feedback_logs_memory_feedback_recorded() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    with structlog.testing.capture_logs() as captured_logs:
        response = client.post(
            f'/v1/memories/{memory.id}/feedback',
            valid_feedback_payload(project, team, request_id='request-memory-feedback-logged'),
            format='json',
            **auth_headers(),
        )

    assert response.status_code == 200
    feedback_events = [entry for entry in captured_logs if entry['event'] == 'memory_feedback_recorded']
    assert len(feedback_events) == 1
    assert feedback_events[0]['memory_id'] == str(memory.id)
    assert feedback_events[0]['project_id'] == str(project.id)
    assert feedback_events[0]['action'] == 'stale'


@pytest.mark.django_db
def test_memory_feedback_refuted_removes_memory_from_context_items_and_surfaces_warning() -> None:
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
    body = context_response.json()
    assert body['items'] == []
    assert str(memory.id) not in [item['memory_id'] for item in body['items']]
    assert f'- [M1] {memory.title}' not in body['rendered_context']
    assert f'> - refuted memory matched: "{memory.title}"' in body['rendered_context']
    assert {
        'code': 'refuted_match',
        'message': f'refuted memory matched: "{memory.title}"',
        'memory_id': str(memory.id),
    } in body['warnings']


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
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-missing-review',
    ).exists()


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
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-wrong-project',
    ).exists()


@pytest.mark.django_db
def test_memory_feedback_denies_team_visible_memory_outside_reviewer_team() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Infrastructure', slug='infrastructure')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    memory, _version, document = create_approved_memory_document(
        organization,
        other_team,
        project,
        visibility_scope=VisibilityScope.TEAM,
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            request_id='request-memory-feedback-wrong-team',
        ),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 403
    assert response.json()['code'] == 'team_scope_denied'
    assert memory.stale is False
    assert memory.refuted is False
    assert document.stale is False
    assert document.refuted is False
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-wrong-team',
    ).exists()


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
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-oversized',
    ).exists()


@pytest.mark.django_db
def test_memory_feedback_rejects_oversized_request_id_before_mutating_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            request_id='r' * 256,
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
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='r' * 256,
    ).exists()


@pytest.mark.django_db
def test_memory_feedback_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        {
            'repository_url': project.repository_url,
            'action': 'stale',
            'reason': 'no longer accurate',
            'request_id': 'request-memory-feedback-repo-url',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.stale is True
    assert document.stale is True


@pytest.mark.django_db
def test_memory_feedback_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        {
            'repository_url': 'https://github.com/acme/never-created-feedback',
            'action': 'stale',
            'reason': 'no longer accurate',
            'request_id': 'request-memory-feedback-unknown-repo',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_memory_feedback_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-feedback')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-feedback',
        repository_url='git@github.com:acme/foreign-feedback.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        {
            'repository_url': 'https://github.com/acme/foreign-feedback',
            'action': 'stale',
            'reason': 'no longer accurate',
            'request_id': 'request-memory-feedback-cross-org',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_memory_feedback_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-feedback-inorg',
        repository_url='git@github.com:acme/foreign-in-org-feedback.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, foreign_project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        {
            'repository_url': 'https://github.com/acme/foreign-in-org-feedback',
            'action': 'stale',
            'reason': 'no longer accurate',
            'request_id': 'request-memory-feedback-foreign-inorg',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_memory_feedback_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        {
            'action': 'stale',
            'reason': 'no longer accurate',
            'request_id': 'request-memory-feedback-missing-both',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_memory_feedback_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-feedback',
        repository_url='git@github.com:acme/decoy-feedback.git',
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            repository_url='https://github.com/acme/decoy-feedback',
            request_id='request-memory-feedback-project-wins',
        ),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.stale is True


@pytest.mark.django_db
def test_memory_feedback_repeated_request_id_in_repository_url_mode_is_idempotent() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()
    payload = {
        'repository_url': project.repository_url,
        'action': 'stale',
        'reason': 'no longer accurate',
        'request_id': 'request-memory-feedback-repo-url-replay',
    }

    first = client.post(
        f'/v1/memories/{memory.id}/feedback',
        payload,
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )
    second = client.post(
        f'/v1/memories/{memory.id}/feedback',
        payload,
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    memory.refresh_from_db()
    document.refresh_from_db()
    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    assert first.json()['already_applied'] is False
    assert second.json()['already_applied'] is True
    assert memory.stale is True
    assert (
        AuditEvent.objects.filter(
            event_type='MemoryTransitionCommitted',
            request_id='request-memory-feedback-repo-url-replay',
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_memory_feedback_rejects_oversized_correlation_id_before_mutating_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(
            project,
            team,
            request_id='request-memory-feedback-oversized-correlation',
            correlation_id='c' * 256,
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
    assert not AuditEvent.objects.filter(
        event_type='MemoryTransitionCommitted',
        request_id='request-memory-feedback-oversized-correlation',
    ).exists()


@pytest.mark.django_db
def test_memory_feedback_stale_response_includes_confirmed_at() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    confirmed_memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Confirmed then stale',
        body='Confirmed then stale body.',
    )
    Memory.objects.filter(id=confirmed_memory.id).update(last_confirmed_at=timezone.now())
    confirmed_memory.refresh_from_db()
    expected_confirmed_at = confirmed_memory.last_confirmed_at.isoformat()
    never_confirmed_memory, _v2, _d2 = create_approved_memory_document(
        organization,
        team,
        project,
        title='Never confirmed',
        body='Never confirmed body.',
    )
    client = APIClient()

    confirmed_response = client.post(
        f'/v1/memories/{confirmed_memory.id}/feedback',
        valid_feedback_payload(project, team, request_id='request-memory-feedback-confirmed-then-stale'),
        format='json',
        **auth_headers(),
    )
    never_confirmed_response = client.post(
        f'/v1/memories/{never_confirmed_memory.id}/feedback',
        valid_feedback_payload(project, team, request_id='request-memory-feedback-never-confirmed-stale'),
        format='json',
        **auth_headers(),
    )

    assert confirmed_response.status_code == 200, confirmed_response.json()
    assert never_confirmed_response.status_code == 200, never_confirmed_response.json()
    assert confirmed_response.json()['confirmed_at'] == expected_confirmed_at
    assert never_confirmed_response.json()['confirmed_at'] == ''


@pytest.mark.django_db
def test_memory_feedback_confirmed_sets_last_confirmed_at_and_audit() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-1'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body['action'] == 'confirmed'
    assert body['confirmed_at'] != ''
    assert body['stale'] is False
    assert body['refuted'] is False
    assert body['already_applied'] is False
    assert memory.last_confirmed_at is not None
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1
    )


@pytest.mark.django_db
def test_memory_feedback_confirmed_does_not_bump_updated_at() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    updated_at_before = memory.updated_at
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-no-bump'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.updated_at == updated_at_before
    assert memory.last_confirmed_at is not None


@pytest.mark.django_db
def test_memory_feedback_confirmed_idempotent_same_request_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()
    payload = valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-idempotent')

    first = client.post(f'/v1/memories/{memory.id}/feedback', payload, format='json', **auth_headers())
    memory.refresh_from_db()
    first_confirmed_at = memory.last_confirmed_at
    second = client.post(f'/v1/memories/{memory.id}/feedback', payload, format='json', **auth_headers())
    memory.refresh_from_db()

    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    assert first.json()['already_applied'] is False
    assert second.json()['already_applied'] is True
    assert memory.last_confirmed_at == first_confirmed_at
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1
    )


@pytest.mark.django_db
def test_memory_feedback_confirmed_new_request_id_refreshes_timestamp() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-first'),
        format='json',
        **auth_headers(),
    )
    memory.refresh_from_db()
    first_confirmed_at = memory.last_confirmed_at
    client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-second'),
        format='json',
        **auth_headers(),
    )
    memory.refresh_from_db()

    assert memory.last_confirmed_at > first_confirmed_at
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 2
    )


@pytest.mark.django_db
def test_memory_feedback_confirmed_replay_after_stale_returns_original_success() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    first = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-replay-stale'),
        format='json',
        **auth_headers(),
    )
    original_confirmed_at = first.json()['confirmed_at']
    stale = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='stale', request_id='request-confirm-replay-mark-stale'),
        format='json',
        **auth_headers(),
    )
    replay = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-replay-stale'),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 200, first.json()
    assert stale.status_code == 200, stale.json()
    assert replay.status_code == 200, replay.json()
    replay_body = replay.json()
    assert replay_body['already_applied'] is True
    assert replay_body['confirmed_at'] == original_confirmed_at
    assert replay_body['stale'] is True


@pytest.mark.django_db
def test_memory_feedback_confirmed_replay_reports_original_timestamp() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    first = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-r1'),
        format='json',
        **auth_headers(),
    )
    second = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-r2'),
        format='json',
        **auth_headers(),
    )
    replay = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-r1'),
        format='json',
        **auth_headers(),
    )

    assert replay.json()['confirmed_at'] == first.json()['confirmed_at']
    assert replay.json()['confirmed_at'] != second.json()['confirmed_at']


@pytest.mark.django_db
def test_memory_feedback_confirmed_audit_has_team_and_redacted_reason() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Infra', slug='infra-confirm-audit')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        visibility_scope=VisibilityScope.TEAM,
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-team-audit'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    event = AuditEvent.objects.get(event_type='MemoryConfirmed', target_id=str(memory.id))
    assert event.team_id == memory.team_id
    assert event.team_id is not None
    assert RAW_KEY not in event.metadata['reason']
    assert event.metadata['reason'] != ''
    outside_team_events = _confirmed_events_for_reader(organization, project, (other_team.id,), memory)
    assert event.id not in [visible.id for visible in outside_team_events]


@pytest.mark.django_db
def test_memory_feedback_confirmed_audit_visibility_follows_project_scope() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Infra', slug='infra-confirm-project')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        visibility_scope=VisibilityScope.PROJECT,
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-project-audit'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.visibility_scope == VisibilityScope.PROJECT
    assert memory.team_id is not None
    event = AuditEvent.objects.get(event_type='MemoryConfirmed', target_id=str(memory.id))
    assert event.team_id is None
    outside_team_events = _confirmed_events_for_reader(organization, project, (other_team.id,), memory)
    assert event.id in [visible.id for visible in outside_team_events]


@pytest.mark.django_db
def test_memory_feedback_confirmed_isolated_per_actor() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=SECOND_RAW_KEY,
        capabilities=('memories:review', 'memories:read'),
    )
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()
    pinned = 'request-confirm-shared-key'

    first = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=pinned),
        format='json',
        **auth_headers(),
    )
    second = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=pinned),
        format='json',
        **auth_headers(SECOND_RAW_KEY),
    )
    memory.refresh_from_db()
    second_confirmed_at = memory.last_confirmed_at
    replay = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=pinned),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    assert first.json()['already_applied'] is False
    assert second.json()['already_applied'] is False
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 2
    )
    assert second_confirmed_at is not None
    assert replay.json()['already_applied'] is True


@pytest.mark.django_db
def test_memory_feedback_confirmed_uses_current_at_processing_semantics() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    memory.current_version = 2
    memory.body = 'Revised authorization body after new evidence.'
    memory.save(update_fields=['current_version', 'body', 'updated_at'])
    memory.refresh_from_db()
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-current-version'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.last_confirmed_at is not None
    assert memory.last_confirmed_at >= memory.updated_at
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1
    )


def _assert_not_confirmable(response: Any, memory: Memory) -> None:
    memory.refresh_from_db()
    assert response.status_code == 400, response.json()
    assert response.json()['code'] == 'memory_not_confirmable'
    assert memory.last_confirmed_at is None
    assert not AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).exists()


@pytest.mark.django_db
def test_memory_feedback_confirmed_rejected_on_stale_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    Memory.objects.filter(id=memory.id).update(stale=True)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-stale'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(response, memory)


@pytest.mark.django_db
def test_memory_feedback_confirmed_rejected_on_refuted_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    Memory.objects.filter(id=memory.id).update(refuted=True)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-refuted'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(response, memory)


@pytest.mark.django_db
@pytest.mark.parametrize('memory_status', [MemoryStatus.CONFLICT, MemoryStatus.ARCHIVED, MemoryStatus.REFUTED])
def test_memory_feedback_confirmed_rejected_on_non_approved_status(memory_status: str) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    Memory.objects.filter(id=memory.id).update(status=memory_status, stale=False, refuted=False)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=f'request-confirm-status-{memory_status}'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(response, memory)


@pytest.mark.django_db
@pytest.mark.parametrize(
    'field_updates',
    [
        {'kind': 'digest'},
        {'confidence': None},
        {'confidence': Decimal('0.200')},
        {'confidence': Decimal('0.100')},
    ],
)
def test_memory_feedback_confirmed_rejected_on_decay_ineligible_memory(field_updates: dict[str, Any]) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    Memory.objects.filter(id=memory.id).update(**field_updates)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-decay-ineligible'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(response, memory)


@pytest.mark.django_db
def test_memory_feedback_confirmed_allowed_when_org_decay_disabled() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    OrganizationSettings.objects.create(organization=organization, confidence_decay_enabled=False)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-org-disabled'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert response.status_code == 200, response.json()
    assert memory.last_confirmed_at is not None
    assert (
        AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1
    )


@pytest.mark.django_db
def test_memory_feedback_confirmed_rejected_on_open_conflict() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, conflict = create_approved_memory_with_open_conflict(organization, team, project)
    client = APIClient()

    rejected = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-open-conflict'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(rejected, memory)

    MemoryConflict.objects.filter(id=conflict.id).update(
        resolved_transition=conflict.opened_transition_id,
        resolution=MemoryConflictResolution.SUPERSEDE_MEMORY,
        resolved_at=timezone.now(),
    )

    resolved = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-conflict-resolved'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert resolved.status_code == 200, resolved.json()
    assert memory.last_confirmed_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize(
    'scenario',
    ['no_document', 'stale_document', 'refuted_document', 'session_scope', 'organization_scope', 'unseen_team_scope', 'org_project_drift'],
)
def test_memory_feedback_confirmed_rejected_when_no_caller_retrievable_document(scenario: str) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    if scenario == 'no_document':
        memory = Memory.objects.create(
            organization=organization,
            project=project,
            team=team,
            title='Bare approved memory',
            body='Bare approved memory body.',
            status=MemoryStatus.APPROVED,
            visibility_scope=VisibilityScope.PROJECT,
            confidence=Decimal('0.900'),
        )
        MemoryVersion.objects.create(
            organization=organization,
            project=project,
            memory=memory,
            version=memory.current_version,
            body=memory.body,
            content_hash='a' * 64,
        )
        client = APIClient()

        rejected = client.post(
            f'/v1/memories/{memory.id}/feedback',
            valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-doc-no_document'),
            format='json',
            **auth_headers(),
        )

        _assert_not_confirmable(rejected, memory)

        make_document_active_project_visible(memory)

        allowed = client.post(
            f'/v1/memories/{memory.id}/feedback',
            valid_feedback_payload(project, team, action='confirmed', request_id='request-confirm-doc-no_document-ok'),
            format='json',
            **auth_headers(),
        )

        memory.refresh_from_db()
        assert allowed.status_code == 200, allowed.json()
        assert memory.last_confirmed_at is not None

        return

    memory, _version, document = create_approved_memory_document(organization, team, project)
    documents = RetrievalDocument.objects.filter(memory=memory)

    if scenario == 'stale_document':
        documents.update(stale=True)
    elif scenario == 'refuted_document':
        documents.update(refuted=True)
    elif scenario == 'session_scope':
        documents.update(visibility_scope=VisibilityScope.SESSION)
    elif scenario == 'organization_scope':
        documents.update(visibility_scope=VisibilityScope.ORGANIZATION)
    elif scenario == 'unseen_team_scope':
        unseen_team = Team.objects.create(organization=organization, name='Unseen', slug='unseen-confirm-doc')
        documents.update(visibility_scope=VisibilityScope.TEAM, team=unseen_team)
    elif scenario == 'org_project_drift':
        decoy_org = Organization.objects.create(name='Decoy', slug='decoy-confirm-doc')
        decoy_project = Project.objects.create(organization=decoy_org, name='Decoy', slug='decoy-confirm-doc')
        documents.update(organization=decoy_org, project=decoy_project)

    client = APIClient()

    rejected = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=f'request-confirm-doc-{scenario}'),
        format='json',
        **auth_headers(),
    )

    _assert_not_confirmable(rejected, memory)

    make_document_active_project_visible(memory)

    allowed = client.post(
        f'/v1/memories/{memory.id}/feedback',
        valid_feedback_payload(project, team, action='confirmed', request_id=f'request-confirm-doc-{scenario}-ok'),
        format='json',
        **auth_headers(),
    )

    memory.refresh_from_db()
    assert allowed.status_code == 200, allowed.json()
    assert memory.last_confirmed_at is not None
