from __future__ import annotations

import pytest

from engram.access.services import EffectiveScope
from engram.console.metrics_service import get_overview_metrics, get_sessions
from engram.core.models import (
    Agent,
    AgentSession,
    ContextBundle,
    Organization,
    Project,
    Runtime,
    SessionStatus,
)


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
