from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.celeryconfig import beat_schedule
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    Organization,
    Project,
    RawEventEnvelope,
    Runtime,
    SessionStatus,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.session_sweep import SweepStaleSessions
from engram.memory.tasks import sweep_stale_sessions

DISTILL_WORK_TASK_NAME = 'engram.memory.distill_session_work_v1'
LEGACY_DISTILL_TASK_NAME = 'engram.memory.distill_session'


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Sweep Org', slug='sweep-org')


@pytest.fixture
def f_team(f_org: Organization) -> Team:
    return Team.objects.create(organization=f_org, name='Platform', slug='platform')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return Agent.objects.create(organization=f_org, runtime=Runtime.CODEX, external_id='codex-sweep')


def create_session(
    organization: Organization,
    team: Team,
    project: Project,
    agent: Agent,
    *,
    status: str = SessionStatus.ACTIVE,
    started_at: object = None,
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
        started_at=started_at,
    )


def create_raw_event(session: AgentSession, *, received_at: object, suffix: str = '1') -> RawEventEnvelope:
    envelope = RawEventEnvelope.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        agent=session.agent,
        session=session,
        event_type='post_tool_use',
        client_event_id=f'event-{session.external_session_id}-{suffix}',
        idempotency_key=f'idem-{session.external_session_id}-{suffix}',
        content_hash=f'hash-{session.external_session_id}-{suffix}',
        runtime=Runtime.CODEX,
        payload={'event': 'noop'},
        normalization_contract_version=0,
    )
    RawEventEnvelope.objects.filter(id=envelope.id).update(received_at=received_at)
    envelope.refresh_from_db()

    return envelope


def create_observation(session: AgentSession, *, suffix: str = '1', session_sequence: int = 1) -> Observation:
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
        session_sequence=session_sequence,
    )


def test_beat_schedule_registers_stale_session_sweep() -> None:
    assert 'stale-session-sweep' in beat_schedule

    entry = beat_schedule['stale-session-sweep']

    assert entry['task'] == 'engram.memory.sweep_stale_sessions'
    assert entry['schedule'] == timedelta(minutes=5)


@pytest.mark.django_db
def test_stale_active_session_with_useful_input_ends_via_primitive_and_signals_distill_work(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_raw_event(session, received_at=timezone.now() - timedelta(minutes=40))
    create_observation(session, session_sequence=1)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    assert session.ended_at is not None
    assert session.end_work_contract_version == 1
    m_delay.assert_not_called()
    work = WorkflowWork.objects.get()
    assert work.work_type == WorkflowWorkType.SESSION_DISTILLATION
    assert work.subject_type == WorkflowSubjectType.AGENT_SESSION
    assert work.subject_id == session.id
    assert work.input_snapshot['upper_sequence_inclusive'] == 1
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    queued = CeleryOutbox.objects.get(task_name=DISTILL_WORK_TASK_NAME)
    assert queued.args == [str(work.id)]
    assert queued.task_id == f'workflow-work:{work.id}'
    assert queued.kwargs == {}
    assert CeleryOutbox.objects.filter(task_name=LEGACY_DISTILL_TASK_NAME).count() == 0
    assert result == {'swept': 1, 'distilled': 1}


@pytest.mark.django_db
def test_stale_empty_session_ends_via_primitive_without_signal(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_raw_event(session, received_at=timezone.now() - timedelta(minutes=40))

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    assert session.end_work_contract_version == 1
    m_delay.assert_not_called()
    work = WorkflowWork.objects.get()
    assert work.work_type == WorkflowWorkType.SESSION_DISTILLATION
    assert work.disposition == WorkflowWorkDisposition.NO_OP
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT
    assert CeleryOutbox.objects.filter(task_name=DISTILL_WORK_TASK_NAME).count() == 0
    assert CeleryOutbox.objects.filter(task_name=LEGACY_DISTILL_TASK_NAME).count() == 0
    assert result == {'swept': 1, 'distilled': 0}


@pytest.mark.django_db
def test_fresh_active_session_is_left_untouched(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_raw_event(session, received_at=timezone.now())
    create_observation(session)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ACTIVE
    assert session.ended_at is None
    m_delay.assert_not_called()
    assert result == {'swept': 0, 'distilled': 0}


@pytest.mark.django_db
def test_already_ended_session_is_not_reswept(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent, status=SessionStatus.ENDED)
    create_raw_event(session, received_at=timezone.now() - timedelta(minutes=10))
    create_observation(session)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    assert session.ended_at is None
    m_delay.assert_not_called()
    assert result == {'swept': 0, 'distilled': 0}


@pytest.mark.django_db
def test_stale_session_with_zero_observations_is_ended_without_distill(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_raw_event(session, received_at=timezone.now() - timedelta(minutes=40))

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    m_delay.assert_not_called()
    assert result == {'swept': 1, 'distilled': 0}


@pytest.mark.django_db
def test_falls_back_to_started_at_when_no_raw_events(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(
        f_org,
        f_team,
        f_project,
        f_agent,
        started_at=timezone.now() - timedelta(minutes=40),
    )

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    m_delay.assert_not_called()
    assert result == {'swept': 1, 'distilled': 0}


@pytest.mark.django_db
def test_falls_back_to_updated_at_when_no_raw_events_and_no_started_at(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    AgentSession.objects.filter(id=session.id).update(updated_at=timezone.now() - timedelta(minutes=40))

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    m_delay.assert_not_called()
    assert result == {'swept': 1, 'distilled': 0}


@pytest.mark.django_db
def test_race_guard_skips_session_ended_between_scan_and_update(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_raw_event(session, received_at=timezone.now() - timedelta(minutes=40))
    create_observation(session)

    original_end_session = SweepStaleSessions._end_session

    def racing_end_session(self: SweepStaleSessions, session_id: object) -> bool:
        AgentSession.objects.filter(id=session_id).update(
            status=SessionStatus.ENDED,
            ended_at=timezone.now(),
        )

        return original_end_session(self, session_id)

    monkeypatch.setattr(SweepStaleSessions, '_end_session', racing_end_session)

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    m_delay.assert_not_called()
    assert result == {'swept': 0, 'distilled': 0}


@pytest.mark.django_db
def test_counters_report_totals_across_mixed_batch(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    stale_with_observations = create_session(f_org, f_team, f_project, f_agent, suffix='stale-with-obs')
    create_raw_event(stale_with_observations, received_at=timezone.now() - timedelta(minutes=40), suffix='1')
    create_observation(stale_with_observations, suffix='1', session_sequence=1)

    stale_without_observations = create_session(f_org, f_team, f_project, f_agent, suffix='stale-without-obs')
    create_raw_event(stale_without_observations, received_at=timezone.now() - timedelta(minutes=40), suffix='2')

    fresh = create_session(f_org, f_team, f_project, f_agent, suffix='fresh')
    create_raw_event(fresh, received_at=timezone.now(), suffix='3')

    with mock.patch('engram.memory.tasks.distill_session.delay') as m_delay:
        result = sweep_stale_sessions()

    assert result == {'swept': 2, 'distilled': 1}
    m_delay.assert_not_called()
    signals = CeleryOutbox.objects.filter(task_name=DISTILL_WORK_TASK_NAME)
    assert signals.count() == 1
    work = WorkflowWork.objects.get(subject_id=stale_with_observations.id)
    assert work.work_type == WorkflowWorkType.SESSION_DISTILLATION
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert signals.get().args == [str(work.id)]
    assert CeleryOutbox.objects.filter(task_name=LEGACY_DISTILL_TASK_NAME).count() == 0


@pytest.mark.django_db
def test_env_override_moves_stale_cutoff_boundary(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_SESSION_IDLE_TIMEOUT_MINUTES', '1')

    stale = create_session(f_org, f_team, f_project, f_agent, suffix='stale')
    create_raw_event(stale, received_at=timezone.now() - timedelta(minutes=2), suffix='1')

    fresh = create_session(f_org, f_team, f_project, f_agent, suffix='fresh')
    create_raw_event(fresh, received_at=timezone.now() - timedelta(seconds=10), suffix='2')

    with mock.patch('engram.memory.tasks.distill_session.delay'):
        sweep_stale_sessions()

    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == SessionStatus.ENDED
    assert fresh.status == SessionStatus.ACTIVE
