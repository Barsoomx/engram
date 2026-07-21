from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from engram.context.context_api_tests import auth_headers, create_approved_memory_document, create_project_scope
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    Memory,
    MemoryStatus,
    Runtime,
)
from engram.inspection.filters import InspectionContextBundleFilterSet, InspectionMemoryFilterSet
from engram.inspection.inspection_api_tests import AUDIT_RAW_KEY, create_audit_key


def _create_context_bundle(organization: Any, team: Any, project: Any, *, request_id: str) -> ContextBundle:
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
        external_session_id=f'session-{request_id}',
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
        query_text='q',
        rendered_text='r',
        token_budget=100,
        selected_count=0,
    )


@pytest.mark.django_db
def test_memory_filterset_filters_by_status_and_kind() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    approved, _version, _document = create_approved_memory_document(organization, team, project, title='Approved')

    archived, _archived_version, _archived_document = create_approved_memory_document(
        organization,
        team,
        project,
        title='Archived',
    )

    archived.status = MemoryStatus.ARCHIVED

    archived.metadata = {'kind': 'digest'}

    archived.save(update_fields=['status', 'metadata', 'updated_at'])

    queryset = Memory.objects.filter(organization=organization, project=project)

    by_status = InspectionMemoryFilterSet(data={'status': MemoryStatus.APPROVED}, queryset=queryset).qs

    assert {m.id for m in by_status} == {approved.id}

    by_kind = InspectionMemoryFilterSet(data={'kind': 'digest'}, queryset=queryset).qs

    assert {m.id for m in by_kind} == {archived.id}


@pytest.mark.django_db
def test_auditevent_target_index_present_in_migrated_schema() -> None:
    expected_columns = ['organization_id', 'project_id', 'target_type', 'target_id', 'created_at']
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, AuditEvent._meta.db_table)

    present = any(entry.get('columns') == expected_columns and entry.get('index') for entry in constraints.values())

    assert present


@pytest.mark.django_db
def test_audit_list_filter_by_target_id_and_target_type() -> None:
    scope = create_project_scope()
    organization, _team, project, _owner, _api_key = scope
    create_audit_key(scope)
    shared_id = str(uuid.uuid4())
    memory_event = AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryTransitionCommitted',
        actor_type='api_key',
        actor_id='actor-target',
        target_type='memory',
        target_id=shared_id,
        capability='memories:write',
        result=AuditResult.RECORDED,
        request_id='req-target-memory',
    )
    AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryLinkCreated',
        actor_type='api_key',
        actor_id='actor-target',
        target_type='memory_link',
        target_id=shared_id,
        capability='memories:write',
        result=AuditResult.RECORDED,
        request_id='req-target-link',
    )
    AuditEvent.objects.create(
        organization=organization,
        project=project,
        event_type='MemoryTransitionCommitted',
        actor_type='api_key',
        actor_id='actor-other',
        target_type='memory',
        target_id=str(uuid.uuid4()),
        capability='memories:write',
        result=AuditResult.RECORDED,
        request_id='req-target-unrelated',
    )
    client = APIClient()

    response = client.get(
        '/v1/inspection/audit-events',
        {'project_id': str(project.id), 'target_id': shared_id, 'target_type': 'memory'},
        **auth_headers(AUDIT_RAW_KEY),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 1
    assert body['items'][0]['id'] == str(memory_event.id)


@pytest.mark.django_db
def test_context_bundle_filterset_filters_by_since_until() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    now = timezone.now()

    old_bundle = _create_context_bundle(organization, team, project, request_id='bundle-since-old')

    old_bundle.created_at = now - timedelta(days=10)

    old_bundle.save(update_fields=['created_at'])

    new_bundle = _create_context_bundle(organization, team, project, request_id='bundle-since-new')

    new_bundle.created_at = now - timedelta(days=1)

    new_bundle.save(update_fields=['created_at'])

    queryset = ContextBundle.objects.filter(organization=organization, project=project)

    filtered = InspectionContextBundleFilterSet(
        data={'since': (now - timedelta(days=5)).isoformat(), 'until': now.isoformat()},
        queryset=queryset,
    ).qs

    ids = {bundle.id for bundle in filtered}

    assert new_bundle.id in ids

    assert old_bundle.id not in ids
