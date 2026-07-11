from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connection, transaction
from django.db.transaction import TransactionManagementError
from django.test.utils import CaptureQueriesContext

from engram.core.models import Agent, AgentSession, Observation, Organization, Project, Team
from engram.memory.observation_work import allocate_observation_sequence, lock_session_for_observation


def create_scope(suffix: str) -> tuple[Organization, Project, AgentSession]:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id=f'agent-{suffix}')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime='codex',
        observation_sequence_cursor=0,
    )
    return organization, project, session


@pytest.mark.django_db(transaction=True)
def test_lock_session_requires_an_active_transaction() -> None:
    organization, project, session = create_scope('atomic-required-lock')

    with pytest.raises(TransactionManagementError, match='active transaction'):
        lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
        )


@pytest.mark.django_db(transaction=True)
def test_allocate_sequence_requires_an_active_transaction() -> None:
    _organization, _project, session = create_scope('atomic-required-allocate')

    with pytest.raises(TransactionManagementError, match='active transaction'):
        allocate_observation_sequence(session)


@pytest.mark.django_db
def test_lock_session_uses_exact_scope_and_locks_only_session_row() -> None:
    organization, project, session = create_scope('exact-scope')
    other_organization, other_project, other_session = create_scope('foreign-scope')

    with CaptureQueriesContext(connection) as queries:
        with transaction.atomic():
            locked = lock_session_for_observation(
                organization_id=organization.id,
                project_id=project.id,
                session_id=session.id,
            )
            assert locked.id == session.id

    sql = '\n'.join(query['sql'] for query in queries)
    assert 'FOR UPDATE OF "core_agentsession"' in sql

    with transaction.atomic(), pytest.raises(AgentSession.DoesNotExist):
        lock_session_for_observation(
            organization_id=other_organization.id,
            project_id=project.id,
            session_id=session.id,
        )
    with transaction.atomic(), pytest.raises(AgentSession.DoesNotExist):
        lock_session_for_observation(
            organization_id=organization.id,
            project_id=other_project.id,
            session_id=session.id,
        )
    with transaction.atomic(), pytest.raises(AgentSession.DoesNotExist):
        lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=other_session.id,
        )


@pytest.mark.django_db
def test_allocate_sequence_uses_max_existing_positive_sequence_for_legacy_null_cursor() -> None:
    organization, project, session = create_scope('null-cursor')
    AgentSession.objects.filter(id=session.id).update(observation_sequence_cursor=None)
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='decision',
        title='Existing null sequence',
        content_hash='null-sequence-content',
        session_sequence=None,
    )
    for sequence in (3, 9):
        Observation.objects.create(
            organization=organization,
            project=project,
            team=session.team,
            agent=session.agent,
            session=session,
            observation_type='decision',
            title=f'Existing sequence {sequence}',
            content_hash=f'existing-sequence-content-{sequence}',
            session_sequence=sequence,
        )

    with transaction.atomic():
        locked = lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
        )
        assert allocate_observation_sequence(locked) == 10
        assert locked.observation_sequence_cursor == 10

    session.refresh_from_db()
    assert session.observation_sequence_cursor == 10


@pytest.mark.django_db
@pytest.mark.parametrize(('cursor', 'expected'), [(0, 1), (7, 8)])
def test_allocate_sequence_increments_zero_or_normal_cursor(cursor: int, expected: int) -> None:
    organization, project, session = create_scope(f'cursor-{cursor}')
    AgentSession.objects.filter(id=session.id).update(observation_sequence_cursor=cursor)

    with transaction.atomic():
        locked = lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
        )
        assert allocate_observation_sequence(locked) == expected

    session.refresh_from_db()
    assert session.observation_sequence_cursor == expected


@pytest.mark.django_db
def test_allocate_sequence_catches_up_when_cursor_is_behind_existing_maximum() -> None:
    organization, project, session = create_scope('cursor-behind')
    AgentSession.objects.filter(id=session.id).update(observation_sequence_cursor=2)
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='decision',
        title='Existing high sequence',
        content_hash='existing-high-sequence',
        session_sequence=11,
    )

    with transaction.atomic():
        locked = lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
        )
        assert allocate_observation_sequence(locked) == 12


@pytest.mark.django_db
def test_sequential_allocations_are_monotonic_and_persist_once_each() -> None:
    organization, project, session = create_scope('sequential')

    with transaction.atomic():
        locked = lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=session.id,
        )
        assert allocate_observation_sequence(locked) == 1
        assert allocate_observation_sequence(locked) == 2

    session.refresh_from_db()
    assert session.observation_sequence_cursor == 2


@pytest.mark.django_db
def test_interfaces_have_no_client_sequence_timestamp_or_uuid_inputs() -> None:
    lock_parameters = inspect.signature(lock_session_for_observation).parameters
    allocate_parameters = inspect.signature(allocate_observation_sequence).parameters

    assert tuple(lock_parameters) == ('organization_id', 'project_id', 'session_id')
    assert tuple(allocate_parameters) == ('session',)


@pytest.mark.django_db(transaction=True)
def test_concurrent_allocations_are_serialized_by_session_row_lock() -> None:
    _organization, _project, session = create_scope('concurrent')

    def allocate() -> int:
        close_old_connections()
        try:
            with transaction.atomic():
                locked = lock_session_for_observation(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    session_id=session.id,
                )
                return allocate_observation_sequence(locked)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        sequences = list(executor.map(lambda _index: allocate(), range(2)))

    assert sorted(sequences) == [1, 2]
    session.refresh_from_db()
    assert session.observation_sequence_cursor == 2
