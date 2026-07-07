from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from engram import celeryconfig
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    Organization,
    Project,
    Runtime,
    SessionStatus,
    Team,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.confidence_decay import DecayMemoryConfidenceResult
from engram.memory.tasks import (
    decay_memory_confidence,
    distill_session,
    generate_daily_digest,
    generate_weekly_digest,
    process_observation_recorded,
    retry_failed_distillations,
)


def test_task_routes_send_ingest_tasks_to_near_realtime_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.process_observation_recorded']['queue'] == (
        celeryconfig.QUEUE_NEAR_REALTIME
    )


def test_task_routes_send_distill_and_digest_tasks_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.distill_session']['queue'] == celeryconfig.QUEUE_BATCH
    assert celeryconfig.task_routes['engram.memory.generate_daily_digest']['queue'] == celeryconfig.QUEUE_BATCH
    assert celeryconfig.task_routes['engram.memory.generate_weekly_digest']['queue'] == celeryconfig.QUEUE_BATCH


def test_celeryconfig_sets_global_time_limits() -> None:
    assert celeryconfig.task_soft_time_limit == 120
    assert celeryconfig.task_time_limit == 180


def test_ingest_and_digest_tasks_ack_late_and_reject_on_worker_lost() -> None:
    for task in (process_observation_recorded, distill_session, generate_daily_digest, generate_weekly_digest):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True


def test_distill_session_has_a_per_task_time_limit_override_above_the_global_default() -> None:
    assert distill_session.soft_time_limit == 600
    assert distill_session.time_limit == 660
    assert celeryconfig.task_soft_time_limit == 120
    assert celeryconfig.task_time_limit == 180


def test_process_observation_recorded_has_a_per_task_time_limit() -> None:
    assert process_observation_recorded.soft_time_limit == 60
    assert process_observation_recorded.time_limit == 90


def test_task_routes_send_retry_failed_distillations_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.retry_failed_distillations']['queue'] == celeryconfig.QUEUE_BATCH


def test_beat_schedule_registers_retry_failed_distillations() -> None:
    assert 'retry-failed-distillations' in celeryconfig.beat_schedule

    entry = celeryconfig.beat_schedule['retry-failed-distillations']

    assert entry['task'] == 'engram.memory.retry_failed_distillations'
    assert entry['schedule'] == timedelta(minutes=30)


def test_task_routes_send_decay_memory_confidence_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.decay_memory_confidence']['queue'] == celeryconfig.QUEUE_BATCH


def test_beat_schedule_registers_confidence_decay() -> None:
    assert 'confidence-decay' in celeryconfig.beat_schedule

    entry = celeryconfig.beat_schedule['confidence-decay']

    assert entry['task'] == 'engram.memory.decay_memory_confidence'


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Tasks Org', slug='tasks-org')


@pytest.fixture
def f_team(f_org: Organization) -> Team:
    return Team.objects.create(organization=f_org, name='Platform', slug='platform')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return Agent.objects.create(organization=f_org, runtime=Runtime.CODEX, external_id='codex-tasks')


def create_session(
    organization: Organization,
    team: Team,
    project: Project,
    agent: Agent,
    *,
    status: str = SessionStatus.ENDED,
    suffix: str = '1',
) -> AgentSession:
    return AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        status=status,
    )


def create_observation(session: AgentSession, *, suffix: str = '1') -> Observation:
    return Observation.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {suffix}',
        body=f'body {suffix}',
        content_hash=f'hash-obs-{session.external_session_id}-{suffix}',
        observed_at=timezone.now(),
    )


def create_failed_workflow_run(session: AgentSession) -> WorkflowRun:
    run = WorkflowRun.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.FAILED,
        input_snapshot={'session_id': str(session.id)},
    )
    WorkflowRun.objects.filter(id=run.id).update(finished_at=timezone.now() - timedelta(minutes=40))
    run.refresh_from_db()

    return run


@pytest.mark.django_db
def test_retry_failed_distillations_enqueues_the_retriable_session(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_failed_workflow_run(session)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = retry_failed_distillations()

    m_delay.assert_called_once_with(str(session.id))
    assert result == {'retried': 1}


@pytest.mark.django_db
def test_retry_failed_distillations_is_a_no_op_when_nothing_is_eligible(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent, status=SessionStatus.ACTIVE)
    create_observation(session)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = retry_failed_distillations()

    m_delay.assert_not_called()
    assert result == {'retried': 0}


def test_decay_memory_confidence_invokes_the_service() -> None:
    m_result = DecayMemoryConfidenceResult(organizations=2, projects=3, memories=5)

    with mock.patch('engram.memory.tasks.DecayMemoryConfidence.execute', return_value=m_result) as m_execute:
        result = decay_memory_confidence()

    m_execute.assert_called_once_with()
    assert result == {'organizations': 2, 'projects': 3, 'memories': 5}
