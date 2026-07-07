from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
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
    ContextBundleStatus,
    Memory,
    MemoryLink,
    Organization,
    Project,
    ProjectTeam,
    Runtime,
    Team,
    VisibilityScope,
)

AUDIT_RAW_KEY = 'egk_test_inspection_audit_0123456789abcdefghijklmnopqrstuvwxyz'
INSPECTION_RAW_KEY = 'egk_test_inspection_admin_0123456789abcdefghijklmnopqrstuvwxyz'
READER_INSP_RAW_KEY = 'egk_reader_insp_0123456789abcdefghijklmnopqrstu'


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
        capabilities=('memories:read', 'memories:admin', 'context:read'),
    )
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:admin'),
    )


def create_reader_insp_key(project_team_scope: tuple[object, Team, object, object, object]) -> None:
    organization, team, project, _owner, _api_key = project_team_scope
    reader = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-insp-reader',
        display_name='Inspection reader',
    )
    reader_role = Role.objects.get(code='auditor')
    OrganizationMembership.objects.create(organization=organization, identity=reader, role=reader_role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=reader, role=reader_role)
    create_scoped_api_key(
        organization,
        team,
        project,
        reader,
        raw_key=READER_INSP_RAW_KEY,
        capabilities=('memories:read', 'context:read'),
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


def create_context_bundle(
    team: Team,
    *,
    request_id: str = 'request-inspection-context-1',
    kind: str = '',
    confidence: Decimal | None = None,
    warnings: list[dict[str, object]] | None = None,
) -> ContextBundle:
    organization = team.organization
    project = team.project_links.get().project
    memory, _version, document = create_approved_memory_document(
        organization,
        team,
        project,
        title=f'Inspectable context {team.slug}',
        body='Inspect context bundle content with citations.',
        visibility_scope=VisibilityScope.TEAM,
        confidence=confidence,
        kind=kind,
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
        metadata={'warnings': warnings} if warnings is not None else {},
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

    no_read_key_raw = 'egk_deny_mem_rd_0123456789abcdefghijklmnopqrstuv'
    no_read_ident = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-deny-mem-rd',
        display_name='Deny Mem Read',
    )
    OrganizationMembership.objects.create(
        organization=organization,
        identity=no_read_ident,
        role=Role.objects.get(code='auditor'),
    )
    ProjectGrant.objects.create(
        organization=organization,
        project=project,
        identity=no_read_ident,
        role=Role.objects.get(code='auditor'),
    )
    create_scoped_api_key(
        organization,
        team,
        project,
        no_read_ident,
        raw_key=no_read_key_raw,
        capabilities=('audit:read',),
    )
    denied_response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION=f'Bearer {no_read_key_raw}',
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
def test_memory_inspection_returns_enriched_payload_fields() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Enriched memory',
        body='Enriched body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    memory.confidence = '0.750'
    memory.metadata = {
        'kind': 'digest',
        'tags': ['auth', 'retrieval'],
        'file_paths': ['apps/backend/engram/core/models.py'],
    }
    memory.save(update_fields=['confidence', 'metadata', 'updated_at'])
    client = APIClient()

    list_response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    detail_response = client.get(
        f'/v1/inspection/memories/{memory.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert list_response.status_code == 200
    item = next(i for i in list_response.json()['items'] if i['id'] == str(memory.id))
    assert item['kind'] == 'digest'
    assert item['tags'] == ['auth', 'retrieval']
    assert item['file_paths'] == ['apps/backend/engram/core/models.py']
    assert item['confidence_percent'] == 75.0
    assert item['authorized_for_injection'] is True
    assert item['project_name'] == project.name
    assert item['project_slug'] == project.slug
    assert item['captured_by'] is None

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail['kind'] == 'digest'
    assert detail['tags'] == ['auth', 'retrieval']
    assert detail['file_paths'] == ['apps/backend/engram/core/models.py']
    assert detail['confidence_percent'] == 75.0
    assert detail['authorized_for_injection'] is True
    assert detail['project_name'] == project.name
    assert detail['project_slug'] == project.slug
    assert 'related' in detail


@pytest.mark.django_db
def test_memory_inspection_authorized_for_injection_false_when_stale() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Stale memory',
        body='Stale body.',
    )
    memory.stale = True
    memory.save(update_fields=['stale', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    item = next(i for i in response.json()['items'] if i['id'] == str(memory.id))
    assert item['authorized_for_injection'] is False


@pytest.mark.django_db
def test_memory_inspection_confidence_percent_null_when_absent() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='No confidence memory',
        body='Body.',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    item = next(i for i in response.json()['items'] if i['id'] == str(memory.id))
    assert item['confidence_percent'] is None
    assert item['confidence'] is None


@pytest.mark.django_db
def test_memory_inspection_related_memories_on_detail() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    memory_a, _va, _da = create_approved_memory_document(
        organization,
        team,
        project,
        title='Memory A',
        body='Body A.',
        visibility_scope=VisibilityScope.TEAM,
    )
    memory_b, _vb, _db = create_approved_memory_document(
        organization,
        team,
        project,
        title='Memory B',
        body='Body B.',
        visibility_scope=VisibilityScope.TEAM,
    )
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory_a,
        link_type='narrowed_by',
        target=str(memory_b.id),
        label='narrowed',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/memories/{memory_a.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    detail = response.json()
    related_ids = {r['id'] for r in detail['related']}
    assert str(memory_b.id) in related_ids
    link_types = {r['link_type'] for r in detail['related'] if r['id'] == str(memory_b.id)}
    assert 'narrowed_by' in link_types


@pytest.mark.django_db
def test_memory_inspection_related_memories_excludes_other_team() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    other_team = Team.objects.create(organization=organization, name='Other', slug='other-rel')
    project.team_links.create(organization=organization, team=other_team)
    memory_a, _va, _da = create_approved_memory_document(
        organization,
        team,
        project,
        title='Memory A related',
        body='Body A.',
        visibility_scope=VisibilityScope.TEAM,
    )
    other_memory, _vo, _do = create_approved_memory_document(
        organization,
        other_team,
        project,
        title='Other team memory related',
        body='Should not appear.',
        visibility_scope=VisibilityScope.TEAM,
    )
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory_a,
        link_type='narrowed_by',
        target=str(other_memory.id),
        label='narrowed',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/memories/{memory_a.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    related_ids = {r['id'] for r in response.json()['related']}
    assert str(other_memory.id) not in related_ids


@pytest.mark.django_db
def test_memory_inspection_count_endpoint_returns_approved_count() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Approved one',
        body='Approved.',
        visibility_scope=VisibilityScope.TEAM,
    )
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Approved two',
        body='Approved.',
        visibility_scope=VisibilityScope.TEAM,
    )
    archived = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived memory',
        body='Archived.',
        visibility_scope=VisibilityScope.TEAM,
    )[0]
    archived.status = 'archived'
    archived.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories/count',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 2


@pytest.mark.django_db
def test_memory_inspection_count_requires_admin_capability() -> None:
    create_project_scope()
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories/count',
        {'project_id': str(uuid.uuid4())},
        **auth_headers(),
    )

    assert response.status_code in (400, 403)


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
def test_context_bundle_inspection_detail_exposes_warnings_and_item_kind_confidence() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    bundle = create_context_bundle(
        team,
        request_id=f'request-warnings-context-{RAW_KEY}',
        kind='gotcha',
        confidence=Decimal('0.850'),
        warnings=[
            {'code': 'stale_match', 'message': f'stale memory matched: "{RAW_KEY}"', 'memory_id': None},
        ],
    )
    client = APIClient()

    detail_response = client.get(
        f'/v1/inspection/context-bundles/{bundle.id}',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail['warnings'] == [
        {'code': 'stale_match', 'message': 'stale memory matched: "[REDACTED]"', 'memory_id': None},
    ]
    assert detail['items'][0]['kind'] == 'gotcha'
    assert detail['items'][0]['confidence'] == '0.850'
    assert RAW_KEY not in str(detail)


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
def test_audit_inspection_resolves_actor_display_name_for_api_key() -> None:
    scope = create_project_scope()
    organization, team, project, owner, api_key = scope
    create_audit_key(scope)
    audit = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id=str(api_key.id),
        target_type='memory',
        target_id=str(uuid.uuid4()),
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-actor-display',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    item = next(i for i in response.json()['items'] if i['id'] == str(audit.id))
    assert item['actor_display'] == owner.display_name
    assert 'actor_display' in item
    assert 'target_display' in item


@pytest.mark.django_db
def test_audit_inspection_resolves_target_display_name_for_memory() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Target memory title',
        body='Body.',
    )
    audit = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='non-uuid-actor',
        target_type='memory',
        target_id=str(memory.id),
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-target-display',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    item = next(i for i in response.json()['items'] if i['id'] == str(audit.id))
    assert item['target_display'] == 'Target memory title'
    assert item['actor_display'] is None


@pytest.mark.django_db
def test_audit_inspection_actor_target_display_is_n_plus_1_bounded() -> None:
    scope = create_project_scope()
    organization, team, project, owner, api_key = scope
    create_audit_key(scope)
    for i in range(5):
        AuditEvent.objects.create(
            organization=organization,
            project=project,
            event_type='MemoryRetrieved',
            actor_type='api_key',
            actor_id=str(api_key.id),
            target_type='project',
            target_id=str(project.id),
            capability='memories:read',
            result=AuditResult.ALLOWED,
            request_id=f'req-bounded-{i}',
        )
    client = APIClient()

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(
            '/v1/inspection/audit-events',
            {'project_id': str(project.id)},
            **auth_headers(AUDIT_RAW_KEY),
        )

    assert response.status_code == 200
    n_events = 5
    api_key_queries = [q for q in ctx.captured_queries if 'access_apikey' in q['sql'].lower()]
    # Batch resolution does 1 query per actor type regardless of event count.
    # Auth overhead adds a few more queries. N+1 for api_key would add n_events extra.
    # Bound: auth overhead + 1 batch < auth overhead + n_events (N+1 minimum).
    assert len(api_key_queries) <= n_events  # batch: ~4; N+1: ~8 — catches regression


@pytest.mark.django_db
def test_cross_tenant_isolation_audit_name_resolution() -> None:
    scope_a = create_project_scope()
    organization_a, team_a, project_a, owner_a, api_key_a = scope_a
    create_audit_key(scope_a)

    organization_b = Organization.objects.create(name='Other Org', slug='other-org-ct')
    team_b = Team.objects.create(organization=organization_b, name='Platform B', slug='platform-b')
    project_b = Project.objects.create(
        organization=organization_b,
        name='Backend B',
        slug='backend-b',
    )
    ProjectTeam.objects.create(organization=organization_b, team=team_b, project=project_b)
    owner_b = Identity.objects.create(
        organization=organization_b,
        identity_type='service_account',
        external_id='svc-b',
        display_name='Org B Identity Should Not Appear',
    )
    api_key_b = ApiKey.objects.create(
        organization=organization_b,
        owner_identity=owner_b,
        name='Key B',
        key_prefix='egk_test_b_',
        key_hash='hash_b_cross_tenant_test',
        key_fingerprint='fp_b_cross_tenant_test',
    )

    audit = AuditEvent.objects.create(
        organization=organization_a,
        project=project_a,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id=str(api_key_b.id),
        target_type='memory',
        target_id=str(uuid.uuid4()),
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-cross-tenant',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project_a.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    item = next(i for i in response.json()['items'] if i['id'] == str(audit.id))
    assert item['actor_display'] is None
    assert 'Org B Identity Should Not Appear' not in str(response.json())


@pytest.mark.django_db
def test_cross_tenant_isolation_related_memories() -> None:
    scope_a = create_project_scope()
    organization_a, team_a, project_a, _owner_a, _api_key_a = scope_a
    create_memory_admin_key(scope_a)

    organization_b = Organization.objects.create(name='Other Org Mem', slug='other-org-mem-ct')
    team_b = Team.objects.create(organization=organization_b, name='Platform B', slug='platform-b-mem')
    project_b = Project.objects.create(
        organization=organization_b,
        name='Backend B',
        slug='backend-b-mem',
    )
    ProjectTeam.objects.create(organization=organization_b, team=team_b, project=project_b)

    memory_a, _va, _da = create_approved_memory_document(
        organization_a,
        team_a,
        project_a,
        title='Org A memory',
        body='Org A body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    memory_b, _vb, _db = create_approved_memory_document(
        organization_b,
        team_b,
        project_b,
        title='Org B secret memory should not appear',
        body='Org B secret body.',
    )
    MemoryLink.objects.create(
        organization=organization_a,
        project=project_a,
        memory=memory_a,
        link_type='narrowed_by',
        target=str(memory_b.id),
        label='cross-tenant link',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/memories/{memory_a.id}',
        {'project_id': str(project_a.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    related_ids = {r['id'] for r in response.json()['related']}
    assert str(memory_b.id) not in related_ids
    assert 'Org B secret memory should not appear' not in str(response.json())


@pytest.mark.django_db
def test_inspection_requires_project_id() -> None:
    create_project_scope()
    client = APIClient()

    response = client.get('/v1/inspection/memories', **auth_headers())

    assert response.status_code == 400
    assert response.json()['project_id']['code'] == ['inspection_project_required']


@pytest.mark.django_db
def test_memory_list_pagination_limit_and_offset() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    for i in range(4):
        create_approved_memory_document(
            organization,
            team,
            project,
            title=f'Memory pagination {i}',
            body='Body.',
            visibility_scope=VisibilityScope.TEAM,
        )
    client = APIClient()

    page_one = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'limit': '2', 'offset': '0'},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    page_two = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'limit': '2', 'offset': '2'},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert page_one.status_code == 200
    body_one = page_one.json()
    assert body_one['count'] == 4
    assert len(body_one['items']) == 2

    assert page_two.status_code == 200
    body_two = page_two.json()
    assert body_two['count'] == 4
    assert len(body_two['items']) == 2

    ids_one = {i['id'] for i in body_one['items']}
    ids_two = {i['id'] for i in body_two['items']}
    assert ids_one.isdisjoint(ids_two)


@pytest.mark.django_db
def test_memory_list_filter_by_status() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    approved, _v, _d = create_approved_memory_document(
        organization,
        team,
        project,
        title='Approved memory',
        body='Approved.',
        visibility_scope=VisibilityScope.TEAM,
    )
    archived, _va, _da = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived memory',
        body='Archived.',
        visibility_scope=VisibilityScope.TEAM,
    )
    archived.status = 'archived'
    archived.save(update_fields=['status', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'status': 'approved'},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    assert body['items'][0]['id'] == str(approved.id)


@pytest.mark.django_db
def test_memory_list_search_matches_title_or_body() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    match, _mv, _md = create_approved_memory_document(
        organization,
        team,
        project,
        title='Vector index tuning',
        body='Ranking notes.',
        visibility_scope=VisibilityScope.TEAM,
    )
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Unrelated memory',
        body='Nothing relevant.',
        visibility_scope=VisibilityScope.TEAM,
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'search': 'vector index'},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item['id'] for item in body['items']] == [str(match.id)]


@pytest.mark.django_db
def test_memory_list_defaults_to_newest_first_and_honors_ordering() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    older, _ov, _od = create_approved_memory_document(
        organization,
        team,
        project,
        title='Older memory',
        visibility_scope=VisibilityScope.TEAM,
    )
    newer, _nv, _nd = create_approved_memory_document(
        organization,
        team,
        project,
        title='Newer memory',
        visibility_scope=VisibilityScope.TEAM,
    )
    Memory.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=2))
    Memory.objects.filter(id=newer.id).update(created_at=timezone.now())
    client = APIClient()

    default_response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    ascending_response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'ordering': 'created_at'},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert [item['id'] for item in default_response.json()['items']] == [str(newer.id), str(older.id)]
    assert [item['id'] for item in ascending_response.json()['items']] == [str(older.id), str(newer.id)]


def _bundle_with_session(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    request_id: str,
    session_external_id: str,
    status: str,
) -> ContextBundle:
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'agent-{request_id}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=session_external_id,
        runtime=Runtime.CODEX,
    )

    return ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id=request_id,
        purpose='session_start',
        status=status,
    )


@pytest.mark.django_db
def test_context_bundle_list_filters_by_status_and_session() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    injected = _bundle_with_session(
        organization,
        team,
        project,
        request_id='request-injected-context',
        session_external_id='session-injected',
        status=ContextBundleStatus.INJECTED,
    )
    _bundle_with_session(
        organization,
        team,
        project,
        request_id='request-skipped-context',
        session_external_id='session-skipped',
        status=ContextBundleStatus.SKIPPED,
    )
    client = APIClient()

    status_response = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id), 'status': ContextBundleStatus.INJECTED},
        **auth_headers(INSPECTION_RAW_KEY),
    )
    session_response = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id), 'session_id': str(injected.session_id)},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert [item['id'] for item in status_response.json()['items']] == [str(injected.id)]
    assert [item['id'] for item in session_response.json()['items']] == [str(injected.id)]


@pytest.mark.django_db
def test_memory_list_filter_by_kind() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    digest, _vd, _dd = create_approved_memory_document(
        organization,
        team,
        project,
        title='Digest memory',
        body='Body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    digest.metadata = {'kind': 'digest'}
    digest.kind = 'digest'
    digest.save(update_fields=['metadata', 'kind', 'updated_at'])
    snippet, _vs, _ds = create_approved_memory_document(
        organization,
        team,
        project,
        title='Snippet memory',
        body='Body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    snippet.metadata = {'kind': 'snippet'}
    snippet.kind = 'snippet'
    snippet.save(update_fields=['metadata', 'kind', 'updated_at'])
    client = APIClient()

    response = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id), 'kind': 'digest'},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    assert body['items'][0]['id'] == str(digest.id)


@pytest.mark.django_db
def test_audit_list_filter_by_event_type() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    retrieved = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-1',
        target_type='memory',
        target_id='target-1',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-filter-event-type-1',
    )
    AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryCreated',
        actor_type='api_key',
        actor_id='actor-2',
        target_type='memory',
        target_id='target-2',
        capability='memories:write',
        result=AuditResult.ALLOWED,
        request_id='req-filter-event-type-2',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id), 'event_type': 'MemoryRetrieved'},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    assert body['items'][0]['id'] == str(retrieved.id)


@pytest.mark.django_db
def test_audit_list_filter_by_correlation_id() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    target_event = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-corr',
        target_type='memory',
        target_id='target-corr',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-corr-target-123',
        correlation_id='correlation-target-123',
    )
    AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-corr-2',
        target_type='memory',
        target_id='target-corr-2',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='correlation-target-123',
        correlation_id='correlation-other-456',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id), 'correlation_id': 'correlation-target-123'},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    assert body['items'][0]['id'] == str(target_event.id)


@pytest.mark.django_db
def test_audit_list_filter_by_since_until() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    now = timezone.now()
    old_event = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-since',
        target_type='memory',
        target_id='target-since-old',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-since-old',
    )
    old_event.created_at = now - timedelta(days=10)
    old_event.save(update_fields=['created_at'])
    new_event = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-since-new',
        target_type='memory',
        target_id='target-since-new',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-since-new',
    )
    new_event.created_at = now - timedelta(days=1)
    new_event.save(update_fields=['created_at'])
    since_str = (now - timedelta(days=5)).isoformat()
    until_str = now.isoformat()
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id), 'since': since_str, 'until': until_str},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    ids = {item['id'] for item in body['items']}
    assert str(new_event.id) in ids
    assert str(old_event.id) not in ids


@pytest.mark.django_db
def test_audit_detail_returns_event_with_name_maps() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, api_key = scope
    create_audit_key(scope)
    ae = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id=str(api_key.id),
        target_type='project',
        target_id=str(project.id),
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-detail-audit',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/audit-events/{ae.id}',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['id'] == str(ae.id)
    assert body['event_type'] == 'MemoryRetrieved'
    assert 'actor_display' in body
    assert 'target_display' in body


@pytest.mark.django_db
def test_audit_detail_cross_org_returns_404() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_audit_key(scope)
    other_org = Organization.objects.create(name='Other', slug='other-audit-detail')
    other_project = Project.objects.create(organization=other_org, name='Other', slug='other-audit-p')
    other_ae = AuditEvent.objects.create(
        organization=other_org,
        project=other_project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='other-actor',
        target_type='memory',
        target_id='other-target',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-cross-org-audit',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/audit-events/{other_ae.id}',
        {'project_id': str(project.id)},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'audit_event_not_found'


@pytest.mark.django_db
def test_audit_detail_missing_capability_returns_403() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    ae = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-no-cap',
        target_type='memory',
        target_id='target-no-cap',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='req-no-cap-audit',
    )
    client = APIClient()

    response = client.get(
        f'/v1/inspection/audit-events/{ae.id}',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_context_bundle_list_filter_by_since_until() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    now = timezone.now()
    old_bundle = create_context_bundle(team, request_id='req-old-bundle')
    old_bundle.created_at = now - timedelta(days=10)
    old_bundle.save(update_fields=['created_at'])
    agent = old_bundle.agent
    session = old_bundle.session
    new_bundle = ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id='req-new-bundle',
        purpose='session_start',
        query_text='new bundle query',
        rendered_text='new bundle rendered',
        authorization_scope={},
        selected_count=0,
    )
    new_bundle.created_at = now - timedelta(days=1)
    new_bundle.save(update_fields=['created_at'])
    since_str = (now - timedelta(days=5)).isoformat()
    client = APIClient()

    response = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id), 'since': since_str},
        **auth_headers(INSPECTION_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    ids = {item['id'] for item in body['items']}
    assert str(new_bundle.id) in ids
    assert str(old_bundle.id) not in ids


@pytest.mark.django_db
def test_reader_role_can_inspect_memories_count_and_context_bundles() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    create_reader_insp_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Reader visible memory',
        body='Visible body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    bundle = create_context_bundle(team)
    client = APIClient()

    mem_list = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        **auth_headers(READER_INSP_RAW_KEY),
    )
    mem_detail = client.get(
        f'/v1/inspection/memories/{memory.id}',
        {'project_id': str(project.id)},
        **auth_headers(READER_INSP_RAW_KEY),
    )
    mem_count = client.get(
        '/v1/inspection/memories/count',
        {'project_id': str(project.id)},
        **auth_headers(READER_INSP_RAW_KEY),
    )
    ctx_list = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id)},
        **auth_headers(READER_INSP_RAW_KEY),
    )
    ctx_detail = client.get(
        f'/v1/inspection/context-bundles/{bundle.id}',
        {'project_id': str(project.id)},
        **auth_headers(READER_INSP_RAW_KEY),
    )

    assert mem_list.status_code == 200
    mem_ids = {item['id'] for item in mem_list.json()['items']}
    assert str(memory.id) in mem_ids
    assert mem_detail.status_code == 200
    assert mem_detail.json()['id'] == str(memory.id)
    assert mem_count.status_code == 200
    assert mem_count.json()['count'] >= 1
    assert ctx_list.status_code == 200
    assert ctx_detail.status_code == 200


@pytest.mark.django_db
def test_reader_denied_without_required_read_capability() -> None:
    scope = create_project_scope()
    organization, team, project, _owner, _api_key = scope
    create_memory_admin_key(scope)
    memory, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Auth memory',
        body='Body.',
        visibility_scope=VisibilityScope.TEAM,
    )
    create_context_bundle(team)

    no_cap_raw = 'egk_no_cap_rd_000000123456789abcdefghijklmnopqrstuv'
    no_cap_ident = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-no-cap-rd',
        display_name='No Cap Read',
    )
    OrganizationMembership.objects.create(
        organization=organization,
        identity=no_cap_ident,
        role=Role.objects.get(code='auditor'),
    )
    ProjectGrant.objects.create(
        organization=organization,
        project=project,
        identity=no_cap_ident,
        role=Role.objects.get(code='auditor'),
    )
    create_scoped_api_key(
        organization,
        team,
        project,
        no_cap_ident,
        raw_key=no_cap_raw,
        capabilities=('audit:read',),
    )
    client = APIClient()

    denied_mem = client.get(
        '/v1/inspection/memories',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION=f'Bearer {no_cap_raw}',
    )
    denied_ctx = client.get(
        '/v1/inspection/context-bundles',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION=f'Bearer {no_cap_raw}',
    )

    assert denied_mem.status_code == 403
    assert denied_mem.json()['code'] == 'missing_capability'
    assert denied_ctx.status_code == 403
    assert denied_ctx.json()['code'] == 'missing_capability'
