from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings

from engram.access.services import EffectiveScope
from engram.console.metrics_service import get_overview_metrics, get_sessions
from engram.core.models import (
    Agent,
    AgentSession,
    ContextBundle,
    Memory,
    MemoryStatus,
    Organization,
    Project,
    Runtime,
    SessionStatus,
)

LOCMEM_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'metrics-service-tests',
    },
}


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Engram', slug='engram')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return Agent.objects.create(
        organization=f_org,
        runtime=Runtime.UNKNOWN,
        external_id='agent-1',
        display_name='agent-1',
    )


def scope_for(organization: Organization, project: Project) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=organization.id,
        api_key_id=organization.id,
        project_ids=(project.id,),
        team_ids=(),
        capabilities=(),
        actor_type='api_key',
        actor_id='actor-1',
    )


@pytest.mark.django_db
def test_get_sessions_returns_session_model_id_when_set(
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    AgentSession.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        external_session_id='sess-with-model-id',
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
        model_id='claude-sonnet-4-5',
    )

    results = get_sessions(f_org, scope_for(f_org, f_project))

    assert results[0]['model_id'] == 'claude-sonnet-4-5'


@pytest.mark.django_db
def test_get_sessions_returns_blank_model_id_when_unset(
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    AgentSession.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        external_session_id='sess-without-model-id',
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
    )

    results = get_sessions(f_org, scope_for(f_org, f_project))

    assert results[0]['model_id'] == ''


@pytest.mark.django_db
def test_get_overview_metrics_avg_retrieval_latency_none_when_no_bundles(
    f_org: Organization,
    f_project: Project,
) -> None:
    metrics = get_overview_metrics(f_org, scope_for(f_org, f_project))

    assert metrics['avg_retrieval_latency_ms'] is None
    assert metrics['avg_retrieval_latency_measured'] is False


@pytest.mark.django_db
def test_get_overview_metrics_avg_retrieval_latency_measured_when_bundles_have_latency(
    f_org: Organization,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = AgentSession.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        external_session_id='sess-latency',
        runtime=Runtime.UNKNOWN,
        status=SessionStatus.ACTIVE,
    )
    ContextBundle.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        session=session,
        request_id='req-latency-1',
        purpose='session_start',
        retrieval_latency_ms=100,
    )
    ContextBundle.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        session=session,
        request_id='req-latency-2',
        purpose='session_start',
        retrieval_latency_ms=200,
    )
    ContextBundle.objects.create(
        organization=f_org,
        project=f_project,
        agent=f_agent,
        session=session,
        request_id='req-latency-unmeasured',
        purpose='session_start',
    )

    metrics = get_overview_metrics(f_org, scope_for(f_org, f_project))

    assert metrics['avg_retrieval_latency_ms'] == 150
    assert metrics['avg_retrieval_latency_measured'] is True


@pytest.fixture
def f_locmem_cache() -> None:
    with override_settings(CACHES=LOCMEM_CACHES):
        cache.clear()
        yield
        cache.clear()


@pytest.mark.django_db
def test_get_overview_metrics_uses_cache_on_second_call_within_ttl(
    f_locmem_cache: None,
    f_org: Organization,
    f_project: Project,
    django_assert_num_queries: Any,
) -> None:
    Memory.objects.create(
        organization=f_org,
        project=f_project,
        title='memory-1',
        body='body-1',
        status=MemoryStatus.APPROVED,
    )

    first = get_overview_metrics(f_org, scope_for(f_org, f_project))

    Memory.objects.create(
        organization=f_org,
        project=f_project,
        title='memory-2',
        body='body-2',
        status=MemoryStatus.APPROVED,
    )

    with django_assert_num_queries(0):
        second = get_overview_metrics(f_org, scope_for(f_org, f_project))

    assert first['memories_indexed'] == 1
    assert second['memories_indexed'] == 1


@pytest.mark.django_db
def test_get_overview_metrics_cache_is_scope_specific(
    f_locmem_cache: None,
    f_org: Organization,
    f_project: Project,
) -> None:
    other_org = Organization.objects.create(name='Other', slug='other')
    other_project = Project.objects.create(organization=other_org, name='Other Backend', slug='other-backend')

    Memory.objects.create(
        organization=f_org,
        project=f_project,
        title='memory-org-a',
        body='body-org-a',
        status=MemoryStatus.APPROVED,
    )
    Memory.objects.create(
        organization=other_org,
        project=other_project,
        title='memory-org-b-1',
        body='body-org-b-1',
        status=MemoryStatus.APPROVED,
    )
    Memory.objects.create(
        organization=other_org,
        project=other_project,
        title='memory-org-b-2',
        body='body-org-b-2',
        status=MemoryStatus.APPROVED,
    )

    org_a_metrics = get_overview_metrics(f_org, scope_for(f_org, f_project))
    org_b_metrics = get_overview_metrics(other_org, scope_for(other_org, other_project))

    assert org_a_metrics['memories_indexed'] == 1
    assert org_b_metrics['memories_indexed'] == 2
