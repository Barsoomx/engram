from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import close_old_connections, connection
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    AgentSession,
    Observation,
    Organization,
    Project,
    SessionStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.observation_work_tests import create_scope
from engram.memory.tasks import distill_session_work_v1

_DISTILL_TASK_NAME = 'engram.memory.distill_session_work_v1'


def _load_lifecycle() -> object:
    import engram.memory.session_lifecycle as lifecycle

    return lifecycle


def _seed_observation(session: AgentSession, *, sequence: int, event_type: str) -> Observation:
    return Observation.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {sequence}',
        content_hash=f'content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': event_type},
    )


@pytest.mark.django_db
def test_end_watermark_excludes_lifecycle_uses_server_sequence_and_freezes_prefix() -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('watermark')
    AgentSession.objects.filter(id=session.id).update(observation_sequence_cursor=10)
    _seed_observation(session, sequence=1, event_type='session_start')
    _seed_observation(session, sequence=2, event_type='post_tool_use')
    _seed_observation(session, sequence=3, event_type='user_prompt_submit')
    _seed_observation(session, sequence=4, event_type='session_end')

    result = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    assert result.upper_sequence_inclusive == 3
    work = WorkflowWork.objects.get()
    assert work.work_type == WorkflowWorkType.SESSION_DISTILLATION
    assert work.subject_type == WorkflowSubjectType.AGENT_SESSION
    assert work.subject_id == session.id
    assert work.input_snapshot == {
        'schema': 'session_distillation_input/v1',
        'session_id': str(session.id),
        'lower_sequence_exclusive': 0,
        'upper_sequence_inclusive': 3,
    }


@pytest.mark.django_db
@pytest.mark.parametrize('source', ('explicit', 'idle'))
def test_end_atomically_transitions_creates_work_and_id_only_signal(source: str) -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope(f'atomic-{source}')
    _seed_observation(session, sequence=1, event_type='post_tool_use')
    _seed_observation(session, sequence=2, event_type='user_prompt_submit')

    result = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source=source,
    )

    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    assert session.ended_at is not None
    assert timezone.is_aware(session.ended_at)
    assert session.end_work_contract_version == 1
    work = WorkflowWork.objects.get()
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == _DISTILL_TASK_NAME
    assert queued.args == [str(work.id)]
    assert queued.kwargs == {}
    assert queued.task_id == f'workflow-work:{work.id}'
    assert isinstance(result, lifecycle.EndSessionResult)
    assert result.session_id == session.id
    assert result.transitioned is True
    assert result.work_id == work.id
    assert result.work_created is True
    assert result.disposition == WorkflowWorkDisposition.REQUIRED
    assert result.upper_sequence_inclusive == 2
    assert result.initial_signal_created is True


@pytest.mark.django_db
def test_signal_failure_rolls_back_end_marker_work_and_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('fault-signal')
    _seed_observation(session, sequence=1, event_type='post_tool_use')

    class SignalFailureError(RuntimeError):
        pass

    def fail(*args: object, **kwargs: object) -> object:
        raise SignalFailureError('signal dispatch failed')

    monkeypatch.setattr(distill_session_work_v1, 'apply_async', fail)
    with pytest.raises(SignalFailureError):
        lifecycle.EndSession().execute(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
            ended_at=timezone.now(),
            source='explicit',
        )

    session.refresh_from_db()
    assert session.status == SessionStatus.ACTIVE
    assert session.ended_at is None
    assert session.end_work_contract_version == 0
    assert WorkflowWork.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('kind', ('empty', 'lifecycle_only'))
def test_no_input_end_creates_terminal_no_op_without_signal(kind: str) -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope(f'no-input-{kind}')
    if kind == 'lifecycle_only':
        _seed_observation(session, sequence=1, event_type='session_start')
        _seed_observation(session, sequence=2, event_type='session_end')

    result = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    assert result.transitioned is True
    assert result.work_created is True
    assert result.work_id is not None
    assert result.disposition == WorkflowWorkDisposition.NO_OP
    assert result.upper_sequence_inclusive == 0
    assert result.initial_signal_created is False
    work = WorkflowWork.objects.get()
    assert work.disposition == WorkflowWorkDisposition.NO_OP
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_INPUT
    assert CeleryOutbox.objects.count() == 0
    session.refresh_from_db()
    assert session.status == SessionStatus.ENDED
    assert session.end_work_contract_version == 1


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_ends_converge_on_one_generation_and_one_signal() -> None:
    lifecycle = _load_lifecycle()
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL concurrency semantics')

    organization, project, session = create_scope('concurrent-end')
    _seed_observation(session, sequence=1, event_type='post_tool_use')
    barrier = Barrier(2)

    def end() -> object:
        close_old_connections()
        try:
            barrier.wait(timeout=5)

            return lifecycle.EndSession().execute(
                organization_id=organization.id,
                project_id=project.id,
                session_id=session.id,
                ended_at=timezone.now(),
                source='idle',
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(end) for _index in range(2)]
        results = [future.result(timeout=15) for future in futures]

    winners = [result for result in results if result.transitioned]
    losers = [result for result in results if not result.transitioned]
    assert len(winners) == 1
    assert len(losers) == 1
    winner = winners[0]
    loser = losers[0]
    assert winner.work_created is True
    assert winner.work_id is not None
    assert winner.initial_signal_created is True
    assert loser.work_id is None
    assert loser.work_created is False
    assert loser.disposition is None
    assert loser.upper_sequence_inclusive is None
    assert loser.initial_signal_created is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_end_on_ended_session_returns_false_without_new_work_or_signal() -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('duplicate-end')
    _seed_observation(session, sequence=1, event_type='post_tool_use')

    first = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    assert first.transitioned is True
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1

    second = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    assert second.transitioned is False
    assert second.work_id is None
    assert second.work_created is False
    assert second.disposition is None
    assert second.upper_sequence_inclusive is None
    assert second.initial_signal_created is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_reactivation_with_new_useful_input_yields_larger_upper_and_new_signal() -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('reactivate-useful')
    _seed_observation(session, sequence=1, event_type='post_tool_use')

    first = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    first_work = WorkflowWork.objects.get()
    assert first.upper_sequence_inclusive == 1

    AgentSession.objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=2,
    )
    _seed_observation(session, sequence=2, event_type='post_tool_use')

    second = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    assert second.transitioned is True
    assert second.work_created is True
    assert second.upper_sequence_inclusive == 2
    assert second.work_id != first_work.id
    assert second.initial_signal_created is True
    works = WorkflowWork.objects.all()
    assert works.count() == 2
    assert len(set(works.values_list('input_fingerprint', flat=True))) == 2
    assert CeleryOutbox.objects.count() == 2


@pytest.mark.django_db
def test_lifecycle_only_reactivation_reuses_prior_generation_without_new_signal() -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('reactivate-lifecycle')
    _seed_observation(session, sequence=1, event_type='post_tool_use')

    first = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    first_work = WorkflowWork.objects.get()
    assert first.upper_sequence_inclusive == 1

    AgentSession.objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=2,
    )
    _seed_observation(session, sequence=2, event_type='session_start')

    second = lifecycle.EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    assert second.transitioned is True
    assert second.work_created is False
    assert second.work_id == first_work.id
    assert second.upper_sequence_inclusive == 1
    assert second.initial_signal_created is False
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    first_work.refresh_from_db()
    assert first_work.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
@pytest.mark.parametrize(
    'control',
    ('foreign_organization', 'foreign_project', 'nonexistent_session', 'naive_ended_at'),
)
def test_negative_controls_reject_before_any_write(control: str) -> None:
    lifecycle = _load_lifecycle()
    organization, project, session = create_scope('negative')
    _seed_observation(session, sequence=1, event_type='post_tool_use')

    organization_id = organization.id
    project_id = project.id
    session_id = session.id
    ended_at = timezone.now()
    if control == 'foreign_organization':
        organization_id = Organization.objects.create(name='Foreign', slug='foreign-negative').id
    elif control == 'foreign_project':
        project_id = Project.objects.create(
            organization=organization,
            name='Foreign',
            slug='foreign-negative-project',
        ).id
    elif control == 'nonexistent_session':
        session_id = uuid.uuid4()
    else:
        ended_at = timezone.now().replace(tzinfo=None)

    with pytest.raises((ValueError, ObjectDoesNotExist)):
        lifecycle.EndSession().execute(
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            ended_at=ended_at,
            source='explicit',
        )

    session.refresh_from_db()
    assert session.status == SessionStatus.ACTIVE
    assert session.ended_at is None
    assert session.end_work_contract_version == 0
    assert WorkflowWork.objects.count() == 0
    assert CeleryOutbox.objects.count() == 0
