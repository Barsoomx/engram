from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKeyCapability, Capability, Identity, OrganizationMembership, ProjectGrant, Role
from engram.context.context_api_tests import (
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
    create_scoped_api_key,
)
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    ContextBundleItem,
    Runtime,
    Team,
    VisibilityScope,
)

AUDIT_RAW_KEY = 'egk_test_inspection_audit_0123456789abcdefghijklmnopqrstuvwxyz'
INSPECTION_RAW_KEY = 'egk_test_inspection_admin_0123456789abcdefghijklmnopqrstuvwxyz'


def create_memory_admin_key(project_team_scope: tuple[object, Team, object, object, object]) -> None:
    organization, team, project, _owner, _api_key = project_team_scope
    admin = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-inspection-admin',
        display_name='Inspection admin',
    )
    admin_role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=admin, role=admin_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=admin, role=admin_role)
    api_key = create_scoped_api_key(
        organization,
        team,
        project,
        admin,
        raw_key=INSPECTION_RAW_KEY,
        capabilities=('memories:read', 'memories:admin'),
    )
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:admin'),
    )


def create_audit_key(project_team_scope: tuple[object, Team, object, object, object]) -> None:
    organization, team, project, _owner, _api_key = project_team_scope
    auditor = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-inspection-audit',
        display_name='Inspection auditor',
    )
    auditor_role = Role.objects.get(code='auditor')
    OrganizationMembership.objects.create(organization=organization, identity=auditor, role=auditor_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=auditor, role=auditor_role)
    api_key = create_scoped_api_key(
        organization,
        team,
        project,
        auditor,
        raw_key=AUDIT_RAW_KEY,
        capabilities=('audit:read', 'memories:read'),
    )
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='audit:read'),
    )


def create_context_bundle(team: Team, *, request_id: str = 'request-inspection-context-1') -> ContextBundle:
    organization = team.organization
    project = team.project_links.get().project
    memory, _version, document = create_approved_memory_document(
        organization,
        team,
        project,
        title=f'Inspectable context {team.slug}',
        body='Inspect context bundle content with citations.',
        visibility_scope=VisibilityScope.TEAM,
    )
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'inspection-agent-{team.slug}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'inspection-session-{team.slug}',
        runtime=Runtime.CODEX,
    )
    bundle = ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id=request_id,
        purpose='session_start',
        query_text='inspect context',
        rendered_text=f'[M1] Inspectable context {RAW_KEY}',
        authorization_scope={'capability': 'memories:read', 'authorization': f'Bearer {RAW_KEY}'},
        selected_count=1,
    )
    ContextBundleItem.objects.create(
        bundle=bundle,
        organization=organization,
        project=project,
        memory=memory,
        retrieval_document=document,
        rank=1,
        citation='M1',
        inclusion_reason='exact match',
        scope_evidence={'visibility_scope': 'team', 'team_id': str(team.id), 'token': RAW_KEY},
    )

    return bundle


@pytest.mark.django_db
def test_memory_inspection_lists_authorized_memories_and_redacts_detail_metadata() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    project_memory, _project_version, _project_document = create_approved_memory_document(
        organization,
        None,
        project,
        title=f'Project level memory {RAW_KEY}',
        body=f'Project memory visible to project readers. {RAW_KEY}',
        visibility_scope=VisibilityScope.PROJECT,
    )
    team_memory, _team_version, _team_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Team level memory',
        body='Team memory visible to bound team readers.',
        visibility_scope=VisibilityScope.TEAM,
    )
    other_team = Team.objects.create(organization=organization, name='Support', slug='support')
    project.team_links.create(organization=organization, team=other_team)
    other_memory, _other_version, _other_document = create_approved_memory_document(
        organization,
        other_team,
        project,
        title='Other team memory',
        body='This must not leak to the platform key.',
        visibility_scope=VisibilityScope.TEAM,
    )
    project_memory.metadata = {'authorization': f'Bearer {RAW_KEY}', 'safe': 'visible'}
    project_memory.save(update_fields=['metadata', 'updated_at'])
    client = APIClient()

    denied_response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(),
    )
    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    detail_response = client.get(
        f'/v1/inspection/memories/{project_memory.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert denied_response.status_code == 403
    assert denied_response.json()['code'] == 'missing_capability'

    assert response.status_code == 200
    body = response.json()
    memory_ids = {item['id'] for item in body['items']}
    assert memory_ids == {str(project_memory.id), str(team_memory.id)}
    assert str(other_memory.id) not in memory_ids
    assert body['count'] == 2

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail['id'] == str(project_memory.id)
    assert detail['title'] == 'Project level memory [REDACTED]'
    assert detail['body'] == 'Project memory visible to project readers. [REDACTED]'
    assert detail['metadata']['authorization'] == '[REDACTED]'
    assert detail['metadata']['safe'] == 'visible'
    assert detail['versions'][0]['version'] == 1
    assert detail['retrieval_documents'][0]['memory_id'] == str(project_memory.id)
    assert RAW_KEY not in str(detail)


@pytest.mark.django_db
def test_context_bundle_inspection_returns_items_and_hides_other_team_bundles() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    allowed_bundle = create_context_bundle(team, request_id=f'request-allowed-context-{RAW_KEY}')
    other_team = Team.objects.create(organization=organization, name='Support', slug='support')
    project.team_links.create(organization=organization, team=other_team)
    other_bundle = create_context_bundle(other_team, request_id='request-other-context')
    client = APIClient()

    denied_response = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id)},
        **auth_headers(),
    )
    response = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    detail_response = client.get(
        f'/v1/inspection/context-bundles/{allowed_bundle.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    denied_detail = client.get(
        f'/v1/inspection/context-bundles/{other_bundle.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert denied_response.status_code == 403
    assert denied_response.json()['code'] == 'missing_capability'

    assert response.status_code == 200
    assert [item['id'] for item in response.json()['items']] == [str(allowed_bundle.id)]
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail['id'] == str(allowed_bundle.id)
    assert detail['request_id'] == 'request-allowed-context-[REDACTED]'
    assert detail['rendered_text'] == '[M1] Inspectable context [REDACTED]'
    assert detail['items'][0]['citation'] == 'M1'
    assert detail['items'][0]['scope_evidence']['token'] == '[REDACTED]'
    assert RAW_KEY not in str(detail)
    assert denied_detail.status_code == 404
    assert denied_detail.json()['code'] == 'context_bundle_not_found'


@pytest.mark.django_db
def test_audit_inspection_requires_audit_read_and_redacts_metadata() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    audit = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id=f'api-key-{RAW_KEY}',
        target_type='context_bundle',
        target_id=f'bundle-{RAW_KEY}',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id=f'request-audit-inspection-{RAW_KEY}',
        correlation_id=f'correlation-audit-inspection-{RAW_KEY}',
        metadata={'authorization': f'Bearer {RAW_KEY}', 'selected_count': 1},
    )
    client = APIClient()

    denied_response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **auth_headers(),
    )
    allowed_response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )
    second_allowed_response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert denied_response.status_code == 403
    assert denied_response.json()['code'] == 'missing_capability'

    assert allowed_response.status_code == 200
    body = allowed_response.json()
    assert body['items'][0]['id'] == str(audit.id)
    assert body['items'][0]['actor_id'] == 'api-key-[REDACTED]'
    assert body['items'][0]['target_id'] == 'bundle-[REDACTED]'
    assert body['items'][0]['request_id'] == 'request-audit-inspection-[REDACTED]'
    assert body['items'][0]['correlation_id'] == 'correlation-audit-inspection-[REDACTED]'
    assert body['items'][0]['metadata']['authorization'] == '[REDACTED]'
    assert body['items'][0]['metadata']['selected_count'] == 1
    assert RAW_KEY not in str(body)

    assert second_allowed_response.status_code == 200
    assert [item['id'] for item in second_allowed_response.json()['items']] == [str(audit.id)]


@pytest.mark.django_db
def test_inspection_requires_project_id() -> None:
    create_project_scope()
    client = APIClient()

    response = client.get('/v1/inspection/memories', **auth_headers())

    assert response.status_code == 400
    assert response.json()['project_id']['code'] == ['inspection_project_required']
