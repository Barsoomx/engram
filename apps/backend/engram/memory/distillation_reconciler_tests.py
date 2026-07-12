from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from structlog.testing import capture_logs

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
from engram.memory.distillation_reconciler import RetryFailedDistillations


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Reconciler Org', slug='reconciler-org')


@pytest.fixture
def f_team(f_org: Organization) -> Team:
    return Team.objects.create(organization=f_org, name='Platform', slug='platform')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_agent(f_org: Organization) -> Agent:
    return Agent.objects.create(organization=f_org, runtime=Runtime.CODEX, external_id='codex-reconciler')


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
        session_sequence=1,
    )


def create_workflow_run(
    session: AgentSession,
    *,
    status: str = WorkflowRunStatus.FAILED,
    created_at: object = None,
    finished_at: object = None,
    failure_reason: str = '',
) -> WorkflowRun:
    run = WorkflowRun.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=status,
        input_snapshot={'session_id': str(session.id)},
        failure_reason=failure_reason,
    )

    update_fields: dict[str, object] = {}
    if created_at is not None:
        update_fields['created_at'] = created_at
    if finished_at is not None:
        update_fields['finished_at'] = finished_at
    if update_fields:
        WorkflowRun.objects.filter(id=run.id).update(**update_fields)
        run.refresh_from_db()

    return run


@pytest.mark.django_db
def test_ended_session_with_stale_failed_run_is_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == (session.id,)


@pytest.mark.django_db
def test_session_with_succeeded_run_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        status=WorkflowRunStatus.SUCCEEDED,
        finished_at=timezone.now() - timedelta(minutes=40),
    )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_session_at_max_attempts_cap_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=2),
        finished_at=timezone.now() - timedelta(hours=2),
        failure_reason='provider returned 400',
    )
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=1),
        finished_at=timezone.now() - timedelta(minutes=40),
        failure_reason='provider returned 400',
    )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_active_session_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent, status=SessionStatus.ACTIVE)
    create_observation(session)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_ended_session_with_zero_observations_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_session_whose_latest_run_is_not_failed_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=1),
        finished_at=timezone.now() - timedelta(minutes=40),
    )
    create_workflow_run(
        session,
        status=WorkflowRunStatus.QUEUED,
        created_at=timezone.now(),
    )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_session_with_failed_run_inside_cooldown_is_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=5))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_env_override_shrinks_cooldown_and_makes_session_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_COOLDOWN_MINUTES', '2')
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=5))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == (session.id,)


@pytest.mark.django_db
def test_env_override_lowers_max_attempts_and_makes_session_not_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_MAX_ATTEMPTS', '1')
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(session, finished_at=timezone.now() - timedelta(minutes=40))

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()


@pytest.mark.django_db
def test_two_transient_failures_past_cooldown_are_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=2),
        finished_at=timezone.now() - timedelta(hours=2),
        failure_reason='provider returned 402',
    )
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=1),
        finished_at=timezone.now() - timedelta(minutes=40),
        failure_reason='provider returned 402',
    )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == (session.id,)


@pytest.mark.django_db
def test_transient_failures_beyond_non_transient_cap_are_still_retriable(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    for index in range(5):
        create_workflow_run(
            session,
            created_at=timezone.now() - timedelta(hours=5 - index),
            finished_at=timezone.now() - timedelta(minutes=40 + index),
            failure_reason='provider timed out',
        )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == (session.id,)


@pytest.mark.django_db
def test_transient_failures_exceeding_transient_cap_are_abandoned_and_logged(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_RECONCILE_TRANSIENT_MAX_ATTEMPTS', '2')
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=2),
        finished_at=timezone.now() - timedelta(hours=2),
        failure_reason='provider returned 429',
    )
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=1),
        finished_at=timezone.now() - timedelta(minutes=40),
        failure_reason='provider returned 503',
    )

    with capture_logs() as logs:
        result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()

    abandoned = [entry for entry in logs if entry['event'] == 'distillation_reconciler_abandoned']

    assert len(abandoned) == 1
    assert abandoned[0]['session_id'] == str(session.id)
    assert abandoned[0]['failed_count'] == 2
    assert abandoned[0]['transient_count'] == 2


@pytest.mark.django_db
def test_failures_before_a_succeeded_run_are_excluded_by_the_succeeded_gate(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    create_observation(session)
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=4),
        finished_at=timezone.now() - timedelta(hours=4),
        failure_reason='provider returned 400',
    )
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=3),
        finished_at=timezone.now() - timedelta(hours=3),
        failure_reason='provider returned 400',
    )
    create_workflow_run(
        session,
        status=WorkflowRunStatus.SUCCEEDED,
        created_at=timezone.now() - timedelta(hours=2),
        finished_at=timezone.now() - timedelta(hours=2),
    )
    create_workflow_run(
        session,
        created_at=timezone.now() - timedelta(hours=1),
        finished_at=timezone.now() - timedelta(minutes=40),
        failure_reason='provider returned 400',
    )

    result = RetryFailedDistillations().execute()

    assert result.retriable_session_ids == ()
