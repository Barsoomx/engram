from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.context.context_api_tests import create_approved_memory_document, create_project_scope
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    ContextBundleStatus,
    Memory,
    MemoryStatus,
    Runtime,
    SessionStatus,
)
from engram.inspection.services import (
    InspectionScope,
    ListInspectionAuditEvents,
    ListInspectionContextBundles,
    ListInspectionMemories,
)


def create_inspection_scope_models() -> tuple[object, object, object]:
    organization, team, project, _owner, _api_key = create_project_scope()

    return organization, team, project


def _effective_scope(organization: object, team: object) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(team.id,),
        capabilities=(),
        actor_type='api_key',
        actor_id='svc-inspection-test',
        project_bound=False,
    )


@pytest.mark.django_db
def test_list_audit_events_filters_by_correlation_id_field() -> None:
    organization, team, project = create_inspection_scope_models()
    matching = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-correlation-match',
        target_type='memory',
        target_id='target-correlation-match',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='request-correlation-match',
        correlation_id='correlation-real-123',
    )
    AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryRetrieved',
        actor_type='api_key',
        actor_id='actor-request-id-coincidence',
        target_type='memory',
        target_id='target-request-id-coincidence',
        capability='memories:read',
        result=AuditResult.ALLOWED,
        request_id='correlation-real-123',
        correlation_id='correlation-different-456',
    )
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        correlation_id='correlation-real-123',
    )

    results = list(ListInspectionAuditEvents().execute(inspection_scope))

    assert [ae.id for ae in results] == [matching.id]


@pytest.mark.django_db
def test_list_inspection_memories_defaults_to_newest_first() -> None:
    organization, team, project = create_inspection_scope_models()
    older, _older_version, _older_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Older memory',
    )
    newer, _newer_version, _newer_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Newer memory',
    )
    Memory.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=2))
    Memory.objects.filter(id=newer.id).update(created_at=timezone.now())
    inspection_scope = InspectionScope(project=project, scope=_effective_scope(organization, team))

    results = list(ListInspectionMemories().execute(inspection_scope))

    assert [memory.id for memory in results] == [newer.id, older.id]


@pytest.mark.django_db
def test_list_inspection_memories_ordering_created_at_ascending() -> None:
    organization, team, project = create_inspection_scope_models()
    older, _older_version, _older_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Older memory',
    )
    newer, _newer_version, _newer_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Newer memory',
    )
    Memory.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=2))
    Memory.objects.filter(id=newer.id).update(created_at=timezone.now())
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        ordering='created_at',
    )

    results = list(ListInspectionMemories().execute(inspection_scope))

    assert [memory.id for memory in results] == [older.id, newer.id]


@pytest.mark.django_db
def test_list_inspection_memories_search_matches_title_or_body() -> None:
    organization, team, project = create_inspection_scope_models()
    title_match, _tv, _td = create_approved_memory_document(
        organization,
        team,
        project,
        title='Vector index tuning',
        body='Notes about ranking.',
    )
    body_match, _bv, _bd = create_approved_memory_document(
        organization,
        team,
        project,
        title='Unrelated title',
        body='Deep dive into vector index tuning strategy.',
    )
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Nothing to see',
        body='Nothing relevant here.',
    )
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        search='vector index',
    )

    results = list(ListInspectionMemories().execute(inspection_scope))

    assert {memory.id for memory in results} == {title_match.id, body_match.id}


def _make_context_bundle(
    organization: object,
    team: object,
    project: object,
    *,
    request_id: str,
    session_external_id: str,
    status: str = ContextBundleStatus.CREATED,
) -> ContextBundle:
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.UNKNOWN,
        external_id=f'agent-{request_id}',
        display_name=f'agent-{request_id}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=session_external_id,
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
    )

    return ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id=request_id,
        purpose='context',
        status=status,
    )


@pytest.mark.django_db
def test_list_inspection_context_bundles_filters_by_status() -> None:
    organization, team, project = create_inspection_scope_models()
    injected = _make_context_bundle(
        organization,
        team,
        project,
        request_id='bundle-injected',
        session_external_id='sess-injected',
        status=ContextBundleStatus.INJECTED,
    )
    _make_context_bundle(
        organization,
        team,
        project,
        request_id='bundle-created',
        session_external_id='sess-created',
        status=ContextBundleStatus.CREATED,
    )
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        status=ContextBundleStatus.INJECTED,
    )

    results = list(ListInspectionContextBundles().execute(inspection_scope))

    assert [bundle.id for bundle in results] == [injected.id]


@pytest.mark.django_db
def test_list_inspection_context_bundles_filters_by_session_id() -> None:
    organization, team, project = create_inspection_scope_models()
    target = _make_context_bundle(
        organization,
        team,
        project,
        request_id='bundle-target',
        session_external_id='sess-target',
    )
    _make_context_bundle(
        organization,
        team,
        project,
        request_id='bundle-other',
        session_external_id='sess-other',
    )
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        session_id=str(target.session_id),
    )

    results = list(ListInspectionContextBundles().execute(inspection_scope))

    assert [bundle.id for bundle in results] == [target.id]


@pytest.mark.django_db
def test_list_inspection_memories_count_defaults_to_approved_only() -> None:
    organization, team, project = create_inspection_scope_models()
    create_approved_memory_document(organization, team, project, title='Approved memory')
    archived, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived memory',
    )
    archived.status = MemoryStatus.ARCHIVED
    archived.save(update_fields=['status', 'updated_at'])
    inspection_scope = InspectionScope(project=project, scope=_effective_scope(organization, team))

    count = ListInspectionMemories().count(inspection_scope)

    assert count == 1


@pytest.mark.django_db
def test_list_inspection_memories_count_honors_status_param() -> None:
    organization, team, project = create_inspection_scope_models()
    create_approved_memory_document(organization, team, project, title='Approved memory')
    for index in range(2):
        archived, _version, _document = create_approved_memory_document(
            organization,
            team,
            project,
            title=f'Archived memory {index}',
        )
        archived.status = MemoryStatus.ARCHIVED
        archived.save(update_fields=['status', 'updated_at'])
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        status=MemoryStatus.ARCHIVED,
    )

    count = ListInspectionMemories().count(inspection_scope)

    assert count == 2


@pytest.mark.django_db
def test_list_inspection_memories_count_honors_kind_param() -> None:
    organization, team, project = create_inspection_scope_models()
    digest, _digest_version, _digest_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Digest memory',
    )
    digest.metadata = {'kind': 'digest'}
    digest.kind = 'digest'
    digest.save(update_fields=['metadata', 'kind', 'updated_at'])
    snippet, _snippet_version, _snippet_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Snippet memory',
    )
    snippet.metadata = {'kind': 'snippet'}
    snippet.kind = 'snippet'
    snippet.save(update_fields=['metadata', 'kind', 'updated_at'])
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        kind='digest',
    )

    count = ListInspectionMemories().count(inspection_scope)

    assert count == 1


@pytest.mark.django_db
def test_list_inspection_memories_defaults_to_approved_only_matching_count() -> None:
    organization, team, project = create_inspection_scope_models()
    create_approved_memory_document(organization, team, project, title='Approved memory')
    archived, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived memory',
    )
    archived.status = MemoryStatus.ARCHIVED
    archived.save(update_fields=['status', 'updated_at'])
    inspection_scope = InspectionScope(project=project, scope=_effective_scope(organization, team))

    memories = list(ListInspectionMemories().execute(inspection_scope))
    count = ListInspectionMemories().count(inspection_scope)

    assert {memory.status for memory in memories} == {MemoryStatus.APPROVED}
    assert len(memories) == 1
    assert len(memories) == count


@pytest.mark.django_db
def test_list_inspection_memories_count_honors_search() -> None:
    organization, team, project = create_inspection_scope_models()
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Findme distinct memory',
        body='distinct body one',
    )
    create_approved_memory_document(
        organization,
        team,
        project,
        title='Other memory',
        body='other body two',
    )
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        search='Findme',
    )

    memories = list(ListInspectionMemories().execute(inspection_scope))
    count = ListInspectionMemories().count(inspection_scope)

    assert len(memories) == 1
    assert count == len(memories)


@pytest.mark.django_db
def test_list_inspection_memories_honors_explicit_status_param() -> None:
    organization, team, project = create_inspection_scope_models()
    create_approved_memory_document(organization, team, project, title='Approved memory')
    archived, _version, _document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived memory',
    )
    archived.status = MemoryStatus.ARCHIVED
    archived.save(update_fields=['status', 'updated_at'])
    inspection_scope = InspectionScope(
        project=project,
        scope=_effective_scope(organization, team),
        status=MemoryStatus.ARCHIVED,
    )

    memories = list(ListInspectionMemories().execute(inspection_scope))

    assert {memory.status for memory in memories} == {MemoryStatus.ARCHIVED}
    assert len(memories) == 1
