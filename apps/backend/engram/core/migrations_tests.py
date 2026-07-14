import importlib
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest
from django.apps.registry import Apps
from django.db import connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from django.db.utils import IntegrityError, OperationalError
from django.utils import timezone

MIGRATE_0031 = [('core', '0031_workflowrun_active_daily_digest_unique')]
MIGRATE_0032 = [('core', '0032_workflowwork_sequence_expand')]
MIGRATE_0032B = [('core', '0032b_agentsession_end_work_db_default')]
MIGRATE_0033 = [('core', '0033_backfill_observation_sequence')]
MIGRATION_0033_NODE = ('core', '0033_backfill_observation_sequence')
MIGRATION_0033_MODULE = 'engram.core.migrations.0033_backfill_observation_sequence'


def _end_work_contract_column() -> tuple[str | None, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'core_agentsession'
              AND column_name = 'end_work_contract_version'
            """,
        )
        row = cursor.fetchone()

    assert row is not None

    return row[0], row[1]


def _drop_end_work_contract_default() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE "core_agentsession"
            ALTER COLUMN "end_work_contract_version" DROP DEFAULT
            """,
        )


def _create_historical_0031_session_scope(
    historical_apps: Apps,
) -> tuple[type[models.Model], dict[str, object]]:
    suffix = uuid.uuid4().hex
    organization_model = historical_apps.get_model('core', 'Organization')
    team_model = historical_apps.get_model('core', 'Team')
    project_model = historical_apps.get_model('core', 'Project')
    agent_model = historical_apps.get_model('core', 'Agent')
    session_model = historical_apps.get_model('core', 'AgentSession')

    organization = organization_model.objects.create(
        name=f'Rolling organization {suffix}',
        slug=f'rolling-organization-{suffix}',
    )
    team = team_model.objects.create(
        organization=organization,
        name=f'Rolling team {suffix}',
        slug=f'rolling-team-{suffix}',
    )
    project = project_model.objects.create(
        organization=organization,
        name=f'Rolling project {suffix}',
        slug=f'rolling-project-{suffix}',
    )
    agent = agent_model.objects.create(
        organization=organization,
        runtime='codex',
        external_id=f'rolling-agent-{suffix}',
    )

    return session_model, {
        'organization': organization,
        'project': project,
        'team': team,
        'agent': agent,
        'external_session_id': f'rolling-session-{suffix}',
        'runtime': 'codex',
    }


@pytest.mark.django_db(transaction=True)
def test_0032_expand_preserves_0031_rows_without_backfill() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0031)
        old_apps = executor.loader.project_state(MIGRATE_0031).apps

        organization_model = old_apps.get_model('core', 'Organization')
        team_model = old_apps.get_model('core', 'Team')
        project_model = old_apps.get_model('core', 'Project')
        agent_model = old_apps.get_model('core', 'Agent')
        session_model = old_apps.get_model('core', 'AgentSession')
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        observation_model = old_apps.get_model('core', 'Observation')
        workflow_run_model = old_apps.get_model('core', 'WorkflowRun')

        organization = organization_model.objects.create(name='Legacy organization', slug='legacy-organization')
        team = team_model.objects.create(organization=organization, name='Legacy team', slug='legacy-team')
        project = project_model.objects.create(
            organization=organization,
            name='Legacy project',
            slug='legacy-project',
        )
        agent = agent_model.objects.create(
            organization=organization,
            runtime='codex',
            external_id='legacy-agent',
        )
        session = session_model.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            external_session_id='legacy-session',
            runtime='codex',
        )
        raw_event = raw_event_model.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=session,
            event_type='post_tool_use',
            client_event_id='legacy-event',
            idempotency_key='legacy-event-key',
            content_hash='legacy-event-hash',
            runtime='codex',
            payload={'tool_name': 'bash'},
        )
        observation = observation_model.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=session,
            raw_event=raw_event,
            observation_type='decision',
            title='Legacy observation',
            content_hash='legacy-observation-hash',
        )
        workflow_run = workflow_run_model.objects.create(
            organization=organization,
            project=project,
            team=team,
            run_type='session_distillation',
            status='succeeded',
            input_snapshot={'session_id': str(session.id)},
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0032)
        new_apps = executor.loader.project_state(MIGRATE_0032).apps

        new_session_model = new_apps.get_model('core', 'AgentSession')
        new_raw_event_model = new_apps.get_model('core', 'RawEventEnvelope')
        new_observation_model = new_apps.get_model('core', 'Observation')
        new_workflow_run_model = new_apps.get_model('core', 'WorkflowRun')
        workflow_work_model = new_apps.get_model('core', 'WorkflowWork')

        migrated_session = new_session_model.objects.get(id=session.id)
        migrated_raw_event = new_raw_event_model.objects.get(id=raw_event.id)
        migrated_observation = new_observation_model.objects.get(id=observation.id)
        migrated_run = new_workflow_run_model.objects.get(id=workflow_run.id)

        assert migrated_session.observation_sequence_cursor is None
        assert migrated_session.end_work_contract_version == 0
        assert migrated_observation.session_sequence is None
        assert migrated_raw_event.normalization_contract_version is None
        assert migrated_raw_event.normalization_disposition is None
        assert migrated_raw_event.normalization_reason is None
        assert migrated_run.work_id is None
        assert workflow_work_model.objects.count() == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_fresh_0032_accepts_session_insert_from_0031_model() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0031)
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(MIGRATE_0031).apps
        old_session_model, session_kwargs = _create_historical_0031_session_scope(old_apps)

        executor.migrate(MIGRATE_0032)
        executor = MigrationExecutor(connection)

        assert _end_work_contract_column() == ('0', 'NO')

        session = old_session_model.objects.create(**session_kwargs)
        new_apps = executor.loader.project_state(MIGRATE_0032).apps
        new_session_model = new_apps.get_model('core', 'AgentSession')

        assert new_session_model.objects.get(id=session.id).end_work_contract_version == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0032b_repairs_recorded_0032_without_physical_default() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0031)
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(MIGRATE_0031).apps
        old_session_model, session_kwargs = _create_historical_0031_session_scope(old_apps)

        executor.migrate(MIGRATE_0032)
        executor = MigrationExecutor(connection)
        _drop_end_work_contract_default()
        executor = MigrationExecutor(connection)

        assert _end_work_contract_column() == (None, 'NO')

        executor.migrate(MIGRATE_0032B)
        executor = MigrationExecutor(connection)

        assert _end_work_contract_column() == ('0', 'NO')

        session = old_session_model.objects.create(**session_kwargs)
        new_apps = executor.loader.project_state(MIGRATE_0032B).apps
        new_session_model = new_apps.get_model('core', 'AgentSession')

        assert new_session_model.objects.get(id=session.id).end_work_contract_version == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0032b_reverse_and_reapply_preserve_0031_writer_contract() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0031)
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(MIGRATE_0031).apps
        old_session_model, session_kwargs = _create_historical_0031_session_scope(old_apps)

        executor.migrate(MIGRATE_0032B)
        executor = MigrationExecutor(connection)

        assert _end_work_contract_column() == ('0', 'NO')

        executor.migrate(MIGRATE_0032)
        executor = MigrationExecutor(connection)

        assert _end_work_contract_column() == ('0', 'NO')

        reversed_session = old_session_model.objects.create(**session_kwargs)
        reversed_apps = executor.loader.project_state(MIGRATE_0032).apps
        reversed_session_model = reversed_apps.get_model('core', 'AgentSession')

        assert reversed_session_model.objects.get(id=reversed_session.id).end_work_contract_version == 0

        executor.migrate(MIGRATE_0032B)
        executor = MigrationExecutor(connection)

        reapplied_kwargs = {
            **session_kwargs,
            'external_session_id': f'{session_kwargs["external_session_id"]}-reapplied',
        }
        reapplied_session = old_session_model.objects.create(**reapplied_kwargs)
        reapplied_apps = executor.loader.project_state(MIGRATE_0032B).apps
        reapplied_session_model = reapplied_apps.get_model('core', 'AgentSession')

        assert reapplied_session_model.objects.get(id=reapplied_session.id).end_work_contract_version == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


def _create_historical_0032b_scope(historical_apps: Apps) -> dict[str, object]:
    suffix = uuid.uuid4().hex
    organization_model = historical_apps.get_model('core', 'Organization')
    team_model = historical_apps.get_model('core', 'Team')
    project_model = historical_apps.get_model('core', 'Project')
    agent_model = historical_apps.get_model('core', 'Agent')

    organization = organization_model.objects.create(
        name=f'Backfill organization {suffix}',
        slug=f'backfill-organization-{suffix}',
    )
    team = team_model.objects.create(
        organization=organization,
        name=f'Backfill team {suffix}',
        slug=f'backfill-team-{suffix}',
    )
    project = project_model.objects.create(
        organization=organization,
        name=f'Backfill project {suffix}',
        slug=f'backfill-project-{suffix}',
    )
    agent = agent_model.objects.create(
        organization=organization,
        runtime='codex',
        external_id=f'backfill-agent-{suffix}',
    )

    return {
        'organization': organization,
        'project': project,
        'team': team,
        'agent': agent,
    }


def _create_historical_session(
    historical_apps: Apps,
    scope: dict[str, object],
    external_session_id: str,
    session_id: uuid.UUID | None = None,
) -> models.Model:
    session_model = historical_apps.get_model('core', 'AgentSession')
    session_kwargs: dict[str, object] = {
        'organization': scope['organization'],
        'project': scope['project'],
        'team': scope['team'],
        'agent': scope['agent'],
        'external_session_id': external_session_id,
        'runtime': 'codex',
    }
    if session_id is not None:
        session_kwargs['id'] = session_id

    return session_model.objects.create(**session_kwargs)


def _create_historical_observation(
    historical_apps: Apps,
    scope: dict[str, object],
    session: models.Model,
    content_hash: str,
    created_at: datetime,
    session_sequence: int | None = None,
    prompt_number: int | None = None,
    observed_at: datetime | None = None,
    observation_id: uuid.UUID | None = None,
) -> models.Model:
    observation_model = historical_apps.get_model('core', 'Observation')
    observation_kwargs: dict[str, object] = {
        'organization': scope['organization'],
        'project': scope['project'],
        'team': scope['team'],
        'agent': scope['agent'],
        'session': session,
        'observation_type': 'decision',
        'title': 'Backfill observation',
        'content_hash': content_hash,
        'session_sequence': session_sequence,
        'prompt_number': prompt_number,
        'observed_at': observed_at,
    }
    if observation_id is not None:
        observation_kwargs['id'] = observation_id

    observation = observation_model.objects.create(**observation_kwargs)
    observation_model.objects.filter(id=observation.id).update(created_at=created_at)

    return observation


def _bulk_create_historical_observations(
    historical_apps: Apps,
    scope: dict[str, object],
    session: models.Model,
    count: int,
) -> None:
    observation_model = historical_apps.get_model('core', 'Observation')
    observations = [
        observation_model(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            agent=scope['agent'],
            session=session,
            observation_type='decision',
            title='Backfill observation',
            content_hash=f'{session.id}-{index}',
        )
        for index in range(count)
    ]
    observation_model.objects.bulk_create(observations)


def _ordered_sequences(historical_apps: Apps, session_id: uuid.UUID) -> list[int | None]:
    observation_model = historical_apps.get_model('core', 'Observation')
    rows = observation_model.objects.filter(session_id=session_id).order_by('created_at', 'id')

    return [row.session_sequence for row in rows]


def _session_cursor(historical_apps: Apps, session_id: uuid.UUID) -> int | None:
    session_model = historical_apps.get_model('core', 'AgentSession')

    return session_model.objects.get(id=session_id).observation_sequence_cursor


@pytest.mark.django_db(transaction=True)
def test_0033_orders_by_created_at_then_id_and_sets_cursor() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'ordering-session')
        base = timezone.now()
        _create_historical_observation(old_apps, scope, session, 'ordering-1', base + timedelta(seconds=1))
        _create_historical_observation(
            old_apps,
            scope,
            session,
            'ordering-2',
            base + timedelta(seconds=2),
            observation_id=uuid.UUID(int=902),
        )
        _create_historical_observation(
            old_apps,
            scope,
            session,
            'ordering-3',
            base + timedelta(seconds=2),
            observation_id=uuid.UUID(int=901),
        )
        _create_historical_observation(old_apps, scope, session, 'ordering-4', base + timedelta(seconds=3))

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        observation_model = old_apps.get_model('core', 'Observation')
        tie_break_first = observation_model.objects.get(id=uuid.UUID(int=901))
        tie_break_second = observation_model.objects.get(id=uuid.UUID(int=902))

        assert _ordered_sequences(old_apps, session.id) == [1, 2, 3, 4]
        assert tie_break_first.session_sequence == 2
        assert tie_break_second.session_sequence == 3
        assert _session_cursor(old_apps, session.id) == 4
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_sets_zero_cursor_for_empty_session() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'empty-session')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        observation_model = old_apps.get_model('core', 'Observation')

        assert observation_model.objects.filter(session_id=session.id).count() == 0
        assert _session_cursor(old_apps, session.id) == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_repairs_null_duplicate_and_wrong_sequences() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'repair-session')
        base = timezone.now()
        _create_historical_observation(
            old_apps, scope, session, 'repair-1', base + timedelta(seconds=1), session_sequence=2
        )
        _create_historical_observation(
            old_apps, scope, session, 'repair-2', base + timedelta(seconds=2), session_sequence=1
        )
        _create_historical_observation(
            old_apps, scope, session, 'repair-3', base + timedelta(seconds=3), session_sequence=None
        )
        session_model = old_apps.get_model('core', 'AgentSession')
        session_model.objects.filter(id=session.id).update(observation_sequence_cursor=5)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        assert _ordered_sequences(old_apps, session.id) == [1, 2, 3]
        assert _session_cursor(old_apps, session.id) == 3
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_preflight_cap_aborts_before_any_session_mutation() -> None:
    migration_module = importlib.import_module(MIGRATION_0033_MODULE)
    max_per_session = migration_module.MAX_OBSERVATIONS_PER_SESSION

    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        base = timezone.now()
        session_low = _create_historical_session(old_apps, scope, 'cap-low', session_id=uuid.UUID(int=1))
        session_high = _create_historical_session(old_apps, scope, 'cap-high', session_id=uuid.UUID(int=2))
        _create_historical_observation(old_apps, scope, session_low, 'cap-low-1', base + timedelta(seconds=1))
        _create_historical_observation(old_apps, scope, session_low, 'cap-low-2', base + timedelta(seconds=2))
        _bulk_create_historical_observations(old_apps, scope, session_high, max_per_session + 1)

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError) as excinfo:
            executor.migrate(MIGRATE_0033)

        message = str(excinfo.value)

        assert str(session_high.id) in message
        assert str(max_per_session + 1) in message
        assert _ordered_sequences(old_apps, session_low.id) == [None, None]
        assert _session_cursor(old_apps, session_low.id) is None

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0033_NODE not in reloaded.loader.applied_migrations
    finally:
        old_apps.get_model('core', 'Observation').objects.filter(session_id=session_high.id).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_uses_500_row_batches() -> None:
    migration_module = importlib.import_module(MIGRATION_0033_MODULE)
    batch_size = migration_module.UPDATE_BATCH_SIZE

    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    recorded_batch_sizes: list[int | None] = []
    original_bulk_update = QuerySet.bulk_update

    def _instrumented_bulk_update(self, objs, fields, batch_size=None):  # noqa: ANN001, ANN202
        recorded_batch_sizes.append(batch_size)

        return original_bulk_update(self, objs, fields, batch_size=batch_size)

    try:
        QuerySet.bulk_update = _instrumented_bulk_update
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'batch-session')
        _bulk_create_historical_observations(old_apps, scope, session, 1001)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        assert recorded_batch_sizes
        assert all(size is not None and size <= 500 for size in recorded_batch_sizes)
        assert all(size == batch_size for size in recorded_batch_sizes)
        assert _ordered_sequences(old_apps, session.id) == list(range(1, 1002))
        assert _session_cursor(old_apps, session.id) == 1001
    finally:
        QuerySet.bulk_update = original_bulk_update
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_skips_consistent_session_on_rerun() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'rerun-session')
        base = timezone.now()
        _create_historical_observation(
            old_apps, scope, session, 'rerun-1', base + timedelta(seconds=1), session_sequence=2
        )
        _create_historical_observation(
            old_apps, scope, session, 'rerun-2', base + timedelta(seconds=2), session_sequence=1
        )
        session_model = old_apps.get_model('core', 'AgentSession')
        session_model.objects.filter(id=session.id).update(observation_sequence_cursor=7)
        observation_model = old_apps.get_model('core', 'Observation')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        first_state = {
            row.id: (row.session_sequence, row.updated_at)
            for row in observation_model.objects.filter(session_id=session.id)
        }
        first_cursor = _session_cursor(old_apps, session.id)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0032B)

        recorded_row_counts: list[int] = []
        original_bulk_update = QuerySet.bulk_update

        def _instrumented_bulk_update(self, objs, fields, batch_size=None):  # noqa: ANN001, ANN202
            recorded_row_counts.append(len(objs))

            return original_bulk_update(self, objs, fields, batch_size=batch_size)

        try:
            QuerySet.bulk_update = _instrumented_bulk_update
            executor = MigrationExecutor(connection)
            executor.migrate(MIGRATE_0033)
        finally:
            QuerySet.bulk_update = original_bulk_update

        second_state = {
            row.id: (row.session_sequence, row.updated_at)
            for row in observation_model.objects.filter(session_id=session.id)
        }
        second_cursor = _session_cursor(old_apps, session.id)

        assert first_cursor == 2
        assert second_cursor == 2
        assert second_state == first_state
        assert recorded_row_counts == []
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_failed_session_rolls_back_and_prior_sessions_remain_committed() -> None:
    migration_module = importlib.import_module(MIGRATION_0033_MODULE)
    original_timeout = migration_module.SESSION_LOCK_TIMEOUT

    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    holder_connection = None

    try:
        migration_module.SESSION_LOCK_TIMEOUT = '250ms'
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        base = timezone.now()
        session_low = _create_historical_session(old_apps, scope, 'lock-low', session_id=uuid.UUID(int=1))
        session_high = _create_historical_session(old_apps, scope, 'lock-high', session_id=uuid.UUID(int=2))
        _create_historical_observation(old_apps, scope, session_low, 'lock-low-1', base + timedelta(seconds=1))
        _create_historical_observation(old_apps, scope, session_low, 'lock-low-2', base + timedelta(seconds=2))
        _create_historical_observation(old_apps, scope, session_high, 'lock-high-1', base + timedelta(seconds=1))
        _create_historical_observation(old_apps, scope, session_high, 'lock-high-2', base + timedelta(seconds=2))

        settings = connection.settings_dict
        holder_connection = psycopg.connect(
            dbname=settings['NAME'],
            user=settings['USER'],
            password=settings['PASSWORD'],
            host=settings['HOST'] or 'localhost',
            port=settings['PORT'] or None,
        )
        holder_cursor = holder_connection.cursor()
        holder_cursor.execute(
            'SELECT id FROM core_agentsession WHERE id = %s FOR UPDATE',
            (str(session_high.id),),
        )
        holder_cursor.fetchone()

        executor = MigrationExecutor(connection)
        with pytest.raises((RuntimeError, OperationalError)):
            executor.migrate(MIGRATE_0033)

        assert _ordered_sequences(old_apps, session_low.id) == [1, 2]
        assert _session_cursor(old_apps, session_low.id) == 2
        assert _ordered_sequences(old_apps, session_high.id) == [None, None]
        assert _session_cursor(old_apps, session_high.id) is None

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0033_NODE not in reloaded.loader.applied_migrations
    finally:
        migration_module.SESSION_LOCK_TIMEOUT = original_timeout
        if holder_connection is not None:
            holder_connection.rollback()
            holder_connection.close()
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_retry_after_failure_resumes_without_renumbering() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        base = timezone.now()
        session_low = _create_historical_session(old_apps, scope, 'resume-low', session_id=uuid.UUID(int=1))
        session_high = _create_historical_session(old_apps, scope, 'resume-high', session_id=uuid.UUID(int=2))
        _create_historical_observation(
            old_apps, scope, session_low, 'resume-low-1', base + timedelta(seconds=1), session_sequence=1
        )
        _create_historical_observation(
            old_apps, scope, session_low, 'resume-low-2', base + timedelta(seconds=2), session_sequence=2
        )
        session_model = old_apps.get_model('core', 'AgentSession')
        session_model.objects.filter(id=session_low.id).update(observation_sequence_cursor=2)
        _create_historical_observation(old_apps, scope, session_high, 'resume-high-1', base + timedelta(seconds=1))
        _create_historical_observation(old_apps, scope, session_high, 'resume-high-2', base + timedelta(seconds=2))
        observation_model = old_apps.get_model('core', 'Observation')
        low_before = {
            row.id: (row.session_sequence, row.updated_at)
            for row in observation_model.objects.filter(session_id=session_low.id)
        }

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        low_after = {
            row.id: (row.session_sequence, row.updated_at)
            for row in observation_model.objects.filter(session_id=session_low.id)
        }

        assert low_after == low_before
        assert _session_cursor(old_apps, session_low.id) == 2
        assert _ordered_sequences(old_apps, session_high.id) == [1, 2]
        assert _session_cursor(old_apps, session_high.id) == 2
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_does_not_use_client_or_prompt_order() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'misleading-session')
        base = timezone.now()
        first = _create_historical_observation(
            old_apps,
            scope,
            session,
            'misleading-1',
            base + timedelta(seconds=1),
            prompt_number=30,
            observed_at=base + timedelta(seconds=300),
        )
        _create_historical_observation(
            old_apps,
            scope,
            session,
            'misleading-2',
            base + timedelta(seconds=2),
            prompt_number=20,
            observed_at=base + timedelta(seconds=200),
        )
        last = _create_historical_observation(
            old_apps,
            scope,
            session,
            'misleading-3',
            base + timedelta(seconds=3),
            prompt_number=10,
            observed_at=base + timedelta(seconds=100),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        observation_model = old_apps.get_model('core', 'Observation')

        assert _ordered_sequences(old_apps, session.id) == [1, 2, 3]
        assert observation_model.objects.get(id=first.id).session_sequence == 1
        assert observation_model.objects.get(id=last.id).session_sequence == 3
        assert _session_cursor(old_apps, session.id) == 3
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0033_has_non_atomic_runpython_and_noop_reverse() -> None:
    migration_module = importlib.import_module(MIGRATION_0033_MODULE)
    migration_class = migration_module.Migration

    assert migration_class.atomic is False
    assert list(migration_class.dependencies) == [('core', '0032b_agentsession_end_work_db_default')]

    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0032B)
        old_apps = executor.loader.project_state(MIGRATE_0032B).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'reverse-session')
        base = timezone.now()
        _create_historical_observation(old_apps, scope, session, 'reverse-1', base + timedelta(seconds=1))
        _create_historical_observation(old_apps, scope, session, 'reverse-2', base + timedelta(seconds=2))

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        assert _ordered_sequences(old_apps, session.id) == [1, 2]
        assert _session_cursor(old_apps, session.id) == 2

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0032B)

        assert _ordered_sequences(old_apps, session.id) == [1, 2]
        assert _session_cursor(old_apps, session.id) == 2
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


MIGRATE_0034 = [('core', '0034_memory_loop_input_contract')]
MIGRATION_0034_NODE = ('core', '0034_memory_loop_input_contract')


def _set_session_cursor(historical_apps: Apps, session_id: uuid.UUID, value: int) -> None:
    session_model = historical_apps.get_model('core', 'AgentSession')
    session_model.objects.filter(id=session_id).update(observation_sequence_cursor=value)


def _create_historical_raw_event(
    historical_apps: Apps,
    scope: dict[str, object],
    session: models.Model,
    tag: str,
    version: int | None = None,
    disposition: str | None = None,
    reason: str | None = None,
) -> models.Model:
    raw_event_model = historical_apps.get_model('core', 'RawEventEnvelope')

    return raw_event_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        agent=scope['agent'],
        session=session,
        event_type='post_tool_use',
        client_event_id=f'event-{tag}',
        idempotency_key=f'key-{tag}',
        content_hash=f'raw-{tag}',
        runtime='codex',
        payload={'tool_name': 'bash'},
        normalization_contract_version=version,
        normalization_disposition=disposition,
        normalization_reason=reason,
    )


def _capture_and_drop_raw_norm_constraint(table: str) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s',
            ['core_raw_norm_expand_valid'],
        )
        row = cursor.fetchone()
        definition = row[0] if row is not None else None
        if definition is not None:
            cursor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "core_raw_norm_expand_valid"')

    return definition


def _restore_raw_norm_constraint(table: str, definition: str | None) -> None:
    if definition is None:
        return

    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM pg_constraint WHERE conname = %s', ['core_raw_norm_expand_valid'])
        if cursor.fetchone() is None:
            cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "core_raw_norm_expand_valid" {definition}')


def _delete_raw_event_by_key(table: str, idempotency_key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM "{table}" WHERE idempotency_key = %s', [idempotency_key])  # noqa: S608


@pytest.mark.django_db(transaction=True)
def test_0034_marks_all_null_normalization_as_v0() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'norm-v0-session')
        _set_session_cursor(old_apps, session.id, 0)
        null_event = _create_historical_raw_event(old_apps, scope, session, 'nullrow')
        obs_event = _create_historical_raw_event(
            old_apps, scope, session, 'obsrow', version=1, disposition='observation'
        )
        noop_event = _create_historical_raw_event(
            old_apps, scope, session, 'nooprow', version=1, disposition='no_op', reason='evidence_only'
        )
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        obs_updated_at = raw_event_model.objects.get(id=obs_event.id).updated_at
        noop_updated_at = raw_event_model.objects.get(id=noop_event.id).updated_at

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        new_apps = executor.loader.project_state(MIGRATE_0034).apps
        new_raw_event_model = new_apps.get_model('core', 'RawEventEnvelope')
        migrated_null = new_raw_event_model.objects.get(id=null_event.id)
        migrated_obs = new_raw_event_model.objects.get(id=obs_event.id)
        migrated_noop = new_raw_event_model.objects.get(id=noop_event.id)

        assert migrated_null.normalization_contract_version == 0
        assert migrated_null.normalization_disposition is None
        assert migrated_null.normalization_reason is None
        assert migrated_obs.normalization_contract_version == 1
        assert migrated_obs.normalization_disposition == 'observation'
        assert migrated_obs.normalization_reason is None
        assert migrated_obs.updated_at == obs_updated_at
        assert migrated_noop.normalization_contract_version == 1
        assert migrated_noop.normalization_disposition == 'no_op'
        assert migrated_noop.normalization_reason == 'evidence_only'
        assert migrated_noop.updated_at == noop_updated_at
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_preflight_rejects_partial_normalization_tuple() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    table: str | None = None
    definition: str | None = None

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'partial-session')
        _set_session_cursor(old_apps, session.id, 0)
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        table = raw_event_model._meta.db_table
        definition = _capture_and_drop_raw_norm_constraint(table)
        partial = _create_historical_raw_event(
            old_apps, scope, session, 'partial', version=None, disposition='observation'
        )

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError):
            executor.migrate(MIGRATE_0034)

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0034_NODE not in reloaded.loader.applied_migrations
        assert raw_event_model.objects.get(id=partial.id).normalization_contract_version is None
        assert raw_event_model.objects.get(id=partial.id).normalization_disposition == 'observation'
    finally:
        if table is not None:
            _delete_raw_event_by_key(table, 'key-partial')
            _restore_raw_norm_constraint(table, definition)
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_preflight_rejects_unknown_normalization_version() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    table: str | None = None
    definition: str | None = None

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'unknown-version-session')
        _set_session_cursor(old_apps, session.id, 0)
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        table = raw_event_model._meta.db_table
        definition = _capture_and_drop_raw_norm_constraint(table)
        unknown = _create_historical_raw_event(old_apps, scope, session, 'unknown', version=2)

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError):
            executor.migrate(MIGRATE_0034)

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0034_NODE not in reloaded.loader.applied_migrations
        assert raw_event_model.objects.get(id=unknown.id).normalization_contract_version == 2
    finally:
        if table is not None:
            _delete_raw_event_by_key(table, 'key-unknown')
            _restore_raw_norm_constraint(table, definition)
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_preflight_rejects_unsequenced_observations() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'unsequenced-session')
        _set_session_cursor(old_apps, session.id, 0)
        _create_historical_observation(old_apps, scope, session, 'unseq', timezone.now(), session_sequence=None)

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError):
            executor.migrate(MIGRATE_0034)

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0034_NODE not in reloaded.loader.applied_migrations
        assert _ordered_sequences(old_apps, session.id) == [None]
    finally:
        old_apps.get_model('core', 'Observation').objects.filter(session_id=session.id).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_rejects_null_inserts_after_contract() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'reject-null-session')
        _set_session_cursor(old_apps, session.id, 0)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        session_model = old_apps.get_model('core', 'AgentSession')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(old_apps, scope, session, 'null-version', version=None)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_observation(
                    old_apps, scope, session, 'null-seq', timezone.now(), session_sequence=None
                )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                session_model.objects.create(
                    organization=scope['organization'],
                    project=scope['project'],
                    team=scope['team'],
                    agent=scope['agent'],
                    external_session_id='null-cursor-session',
                    runtime='codex',
                    observation_sequence_cursor=None,
                )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_allows_only_three_normalization_forms() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'three-forms-session')
        _set_session_cursor(old_apps, session.id, 0)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')

        legal_v0 = _create_historical_raw_event(old_apps, scope, session, 'legal-v0', version=0)
        legal_obs = _create_historical_raw_event(
            old_apps, scope, session, 'legal-obs', version=1, disposition='observation'
        )
        legal_noop = _create_historical_raw_event(
            old_apps, scope, session, 'legal-noop', version=1, disposition='no_op', reason='evidence_only'
        )

        assert raw_event_model.objects.filter(id__in=[legal_v0.id, legal_obs.id, legal_noop.id]).count() == 3

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(
                    old_apps, scope, session, 'bad-v0-disp', version=0, disposition='observation'
                )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(
                    old_apps,
                    scope,
                    session,
                    'bad-obs-reason',
                    version=1,
                    disposition='observation',
                    reason='evidence_only',
                )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(
                    old_apps, scope, session, 'bad-noop-reason', version=1, disposition='no_op', reason='other'
                )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(old_apps, scope, session, 'bad-null-disp', version=1, disposition=None)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_pre_0034_history_accepts_legacy_null_inserts() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'legacy-null-session')
        _set_session_cursor(old_apps, session.id, 0)
        raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        observation_model = old_apps.get_model('core', 'Observation')
        legacy_raw = _create_historical_raw_event(old_apps, scope, session, 'legacy-null')
        legacy_obs = _create_historical_observation(
            old_apps, scope, session, 'legacy-null-obs', timezone.now(), session_sequence=None
        )

        assert raw_event_model.objects.filter(id=legacy_raw.id).count() == 1
        assert observation_model.objects.filter(id=legacy_obs.id).count() == 1

        observation_model.objects.filter(id=legacy_obs.id).delete()
        raw_event_model.objects.filter(id=legacy_raw.id).delete()

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_raw_event(old_apps, scope, session, 'post-null')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_observation(
                    old_apps, scope, session, 'post-null-obs', timezone.now(), session_sequence=None
                )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_preserves_end_work_marker_semantics() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'end-marker-session')
        session_model = old_apps.get_model('core', 'AgentSession')
        session_model.objects.filter(id=session.id).update(
            status='ended',
            end_work_contract_version=0,
            observation_sequence_cursor=0,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        new_apps = executor.loader.project_state(MIGRATE_0034).apps
        new_session_model = new_apps.get_model('core', 'AgentSession')
        migrated = new_session_model.objects.get(id=session.id)

        assert migrated.status == 'ended'
        assert migrated.end_work_contract_version == 0
        assert _end_work_contract_column() == ('0', 'NO')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                new_session_model.objects.filter(id=session.id).update(end_work_contract_version=2)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0034_reverse_restores_pre_contract_nullability() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0033)
        old_apps = executor.loader.project_state(MIGRATE_0033).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'reverse-contract-session')
        _set_session_cursor(old_apps, session.id, 0)
        legacy = _create_historical_raw_event(old_apps, scope, session, 'reverse-null')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        new_apps = executor.loader.project_state(MIGRATE_0034).apps
        new_raw_event_model = new_apps.get_model('core', 'RawEventEnvelope')

        assert new_raw_event_model.objects.get(id=legacy.id).normalization_contract_version == 0

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)
        reverted_raw_event_model = old_apps.get_model('core', 'RawEventEnvelope')
        post_reverse = _create_historical_raw_event(old_apps, scope, session, 'post-reverse-null')

        assert reverted_raw_event_model.objects.filter(id=post_reverse.id).count() == 1
        assert reverted_raw_event_model.objects.get(id=legacy.id).normalization_contract_version == 0

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)
        reapplied_apps = executor.loader.project_state(MIGRATE_0034).apps
        reapplied_raw_event_model = reapplied_apps.get_model('core', 'RawEventEnvelope')

        assert reapplied_raw_event_model.objects.get(id=legacy.id).normalization_contract_version == 0
        assert reapplied_raw_event_model.objects.get(id=post_reverse.id).normalization_contract_version == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


MIGRATE_0035 = [('core', '0035_workflow_work_execution')]
MIGRATION_0035_NODE = ('core', '0035_workflow_work_execution')

HEX64 = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
HEX64_UPPER = HEX64.upper()


def _hex_fingerprint() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _create_historical_work(
    historical_apps: Apps,
    scope: dict[str, object],
    disposition: str = 'required',
    work_type: str = 'observation_processing',
    subject_type: str = 'observation',
    subject_id: uuid.UUID | None = None,
    occurrence_key: str = '',
) -> models.Model:
    work_model = historical_apps.get_model('core', 'WorkflowWork')
    if disposition == 'required':
        resolution_reason = ''
        resolved_at: datetime | None = None
    elif disposition == 'no_op':
        resolution_reason = 'no_input'
        resolved_at = timezone.now()
    else:
        resolution_reason = 'succeeded'
        resolved_at = timezone.now()

    return work_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=None,
        work_type=work_type,
        subject_type=subject_type,
        subject_id=subject_id or uuid.uuid4(),
        contract_version=1,
        occurrence_key=occurrence_key,
        input_fingerprint=_hex_fingerprint(),
        input_snapshot={'seed': 'value'},
        disposition=disposition,
        resolution_reason=resolution_reason,
        resolved_at=resolved_at,
    )


def _create_historical_run(
    historical_apps: Apps,
    scope: dict[str, object],
    work: models.Model | None = None,
    status: str = 'queued',
    run_type: str = 'observation_processing',
) -> models.Model:
    run_model = historical_apps.get_model('core', 'WorkflowRun')

    return run_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=None,
        run_type=run_type,
        status=status,
        work=work,
        input_snapshot={},
        provider_call_ids=[],
        failure_reason='legacy detail',
    )


def _execution_work_fields(scope: dict[str, object], **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        'organization': scope['organization'],
        'project': scope['project'],
        'team': None,
        'work_type': 'observation_processing',
        'subject_type': 'observation',
        'subject_id': uuid.uuid4(),
        'contract_version': 1,
        'occurrence_key': '',
        'input_fingerprint': _hex_fingerprint(),
        'input_snapshot': {'seed': 'value'},
        'disposition': 'required',
        'resolution_reason': '',
        'resolved_at': None,
        'execution_state': 'ready',
        'fencing_token': 0,
        'lease_owner': '',
        'lease_expires_at': None,
        'heartbeat_at': None,
        'next_retry_at': None,
        'failure_streak': 0,
        'blocked_configuration_fingerprint': '',
    }
    fields.update(overrides)

    return fields


def _v1_run_fields(scope: dict[str, object], work: models.Model, status: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        'organization': scope['organization'],
        'project': scope['project'],
        'team': None,
        'run_type': work.work_type,
        'status': status,
        'work': work,
        'input_snapshot': {},
        'provider_call_ids': [],
        'execution_contract_version': 1,
        'origin': 'automatic',
        'fencing_token': None,
        'lease_owner': '',
        'dispatched_at': None,
        'lease_expires_at': None,
        'heartbeat_at': None,
        'started_at': None,
        'finished_at': None,
        'failure_class': '',
        'failure_code': '',
        'configuration_fingerprint': '',
    }
    fields.update(overrides)

    return fields


def _execution_column(column: str) -> tuple[str | None, str] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'core_workflowwork'
              AND column_name = %s
            """,
            [column],
        )
        row = cursor.fetchone()

    return (row[0], row[1]) if row is not None else None


def _table_indexdefs(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute('SELECT indexdef FROM pg_indexes WHERE tablename = %s', [table])

        return [row[0] for row in cursor.fetchall()]


@pytest.mark.django_db(transaction=True)
def test_0035_backfills_dispositions_to_execution_states() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0034)
        old_apps = executor.loader.project_state(MIGRATE_0034).apps
        scope = _create_historical_0032b_scope(old_apps)
        required = _create_historical_work(old_apps, scope, disposition='required')
        complete = _create_historical_work(old_apps, scope, disposition='complete')
        no_op = _create_historical_work(old_apps, scope, disposition='no_op')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        work_model = new_apps.get_model('core', 'WorkflowWork')
        migrated_required = work_model.objects.get(id=required.id)
        migrated_complete = work_model.objects.get(id=complete.id)
        migrated_no_op = work_model.objects.get(id=no_op.id)

        assert migrated_required.execution_state == 'ready'
        assert migrated_complete.execution_state == 'settled'
        assert migrated_no_op.execution_state == 'settled'
        assert migrated_required.fencing_token == 0
        assert migrated_required.failure_streak == 0
        assert migrated_required.lease_owner == ''
        assert migrated_required.lease_expires_at is None
        assert migrated_required.heartbeat_at is None
        assert migrated_required.next_retry_at is None
        assert migrated_required.blocked_configuration_fingerprint == ''
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_work_accepts_legal_execution_shapes() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        now = timezone.now()

        legal_shapes = [
            _execution_work_fields(scope),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='celery@host:12:uuid',
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
            ),
            _execution_work_fields(
                scope,
                execution_state='retry_wait',
                fencing_token=1,
                failure_streak=1,
                next_retry_at=now + timedelta(seconds=30),
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint=HEX64,
            ),
            _execution_work_fields(
                scope,
                execution_state='terminal_failure',
                fencing_token=1,
                failure_streak=1,
            ),
            _execution_work_fields(
                scope,
                execution_state='settled',
                disposition='complete',
                resolution_reason='succeeded',
                resolved_at=now,
            ),
            _execution_work_fields(
                scope,
                execution_state='settled',
                disposition='no_op',
                resolution_reason='no_input',
                resolved_at=now,
            ),
        ]

        created = [work_model.objects.create(**shape) for shape in legal_shapes]

        assert work_model.objects.filter(id__in=[row.id for row in created]).count() == len(legal_shapes)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_work_rejects_illegal_execution_shapes() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        now = timezone.now()

        illegal_shapes = [
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='',
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
            ),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='owner',
                heartbeat_at=None,
                lease_expires_at=now + timedelta(seconds=120),
            ),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='owner',
                heartbeat_at=now,
                lease_expires_at=None,
            ),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='owner',
                heartbeat_at=now,
                lease_expires_at=now,
            ),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='owner',
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                next_retry_at=now + timedelta(seconds=30),
            ),
            _execution_work_fields(
                scope,
                execution_state='leased',
                fencing_token=1,
                lease_owner='owner',
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                blocked_configuration_fingerprint=HEX64,
            ),
            _execution_work_fields(
                scope,
                execution_state='retry_wait',
                fencing_token=1,
                failure_streak=1,
                next_retry_at=None,
            ),
            _execution_work_fields(
                scope,
                execution_state='retry_wait',
                fencing_token=1,
                failure_streak=1,
                next_retry_at=now + timedelta(seconds=30),
                lease_owner='owner',
            ),
            _execution_work_fields(
                scope,
                execution_state='retry_wait',
                fencing_token=1,
                failure_streak=1,
                next_retry_at=now + timedelta(seconds=30),
                lease_expires_at=now + timedelta(seconds=120),
            ),
            _execution_work_fields(
                scope,
                execution_state='retry_wait',
                fencing_token=1,
                failure_streak=1,
                next_retry_at=now + timedelta(seconds=30),
                blocked_configuration_fingerprint=HEX64,
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint='',
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint=HEX64_UPPER,
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint='not-hex',
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint=HEX64,
                lease_owner='owner',
            ),
            _execution_work_fields(
                scope,
                execution_state='blocked',
                fencing_token=1,
                failure_streak=1,
                blocked_configuration_fingerprint=HEX64,
                next_retry_at=now + timedelta(seconds=30),
            ),
            _execution_work_fields(scope, execution_state='ready', lease_owner='owner'),
            _execution_work_fields(scope, execution_state='ready', next_retry_at=now + timedelta(seconds=30)),
            _execution_work_fields(scope, execution_state='ready', blocked_configuration_fingerprint=HEX64),
            _execution_work_fields(
                scope,
                execution_state='terminal_failure',
                disposition='complete',
                resolution_reason='succeeded',
                resolved_at=now,
            ),
            _execution_work_fields(scope, execution_state='terminal_failure', lease_owner='owner'),
            _execution_work_fields(scope, execution_state='settled', disposition='required'),
            _execution_work_fields(
                scope,
                execution_state='settled',
                disposition='complete',
                resolution_reason='succeeded',
                resolved_at=now,
                next_retry_at=now + timedelta(seconds=30),
            ),
            _execution_work_fields(scope, execution_state='ready', fencing_token=-1),
            _execution_work_fields(scope, execution_state='ready', failure_streak=-1),
        ]

        for shape in illegal_shapes:
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    work_model.objects.create(**shape)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_run_accepts_legal_v1_shapes() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()

        def _fresh_work() -> models.Model:
            return work_model.objects.create(**_execution_work_fields(scope))

        queued = run_model.objects.create(**_v1_run_fields(scope, _fresh_work(), 'queued', dispatched_at=now))
        running = run_model.objects.create(
            **_v1_run_fields(
                scope,
                _fresh_work(),
                'running',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
            )
        )
        succeeded = run_model.objects.create(
            **_v1_run_fields(
                scope,
                _fresh_work(),
                'succeeded',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
            )
        )
        failed = run_model.objects.create(
            **_v1_run_fields(
                scope,
                _fresh_work(),
                'failed',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
                failure_class='provider_transient',
                failure_code='provider_timeout',
            )
        )
        config_failed = run_model.objects.create(
            **_v1_run_fields(
                scope,
                _fresh_work(),
                'failed',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
                failure_class='configuration',
                failure_code='model_policy_unavailable',
                configuration_fingerprint=HEX64,
            )
        )

        assert (
            run_model.objects.filter(id__in=[queued.id, running.id, succeeded.id, failed.id, config_failed.id]).count()
            == 5
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_run_rejects_illegal_v1_shapes() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()
        expires = now + timedelta(seconds=120)
        finished = now + timedelta(seconds=10)

        def _running(**overrides: object) -> dict[str, object]:
            base = {
                'fencing_token': 1,
                'lease_owner': 'owner',
                'started_at': now,
                'heartbeat_at': now,
                'lease_expires_at': expires,
            }
            base.update(overrides)

            return base

        def _succeeded(**overrides: object) -> dict[str, object]:
            base = _running(finished_at=finished)
            base.update(overrides)

            return base

        def _failed(**overrides: object) -> dict[str, object]:
            base = _succeeded(failure_class='provider_transient', failure_code='provider_timeout')
            base.update(overrides)

            return base

        illegal = [
            ('queued', {'dispatched_at': now, 'fencing_token': 1}),
            ('queued', {'dispatched_at': now, 'started_at': now}),
            ('queued', {'dispatched_at': now, 'lease_owner': 'owner'}),
            (
                'queued',
                {'dispatched_at': now, 'failure_class': 'provider_transient', 'failure_code': 'provider_timeout'},
            ),
            ('queued', {'dispatched_at': None}),
            ('running', _running(fencing_token=0)),
            ('running', _running(fencing_token=None)),
            ('running', _running(lease_owner='')),
            ('running', _running(started_at=None)),
            ('running', _running(heartbeat_at=None)),
            ('running', _running(lease_expires_at=None)),
            ('running', _running(finished_at=finished)),
            ('running', _running(failure_class='provider_transient', failure_code='provider_timeout')),
            ('succeeded', _succeeded(fencing_token=None)),
            ('succeeded', _succeeded(lease_owner='')),
            ('succeeded', _succeeded(started_at=None)),
            ('succeeded', _succeeded(finished_at=None)),
            ('succeeded', _succeeded(failure_class='provider_transient', failure_code='provider_timeout')),
            ('failed', _failed(fencing_token=None)),
            ('failed', _failed(failure_class='')),
            ('failed', _failed(failure_code='')),
            ('failed', _failed(finished_at=None)),
        ]

        for status, overrides in illegal:
            work = work_model.objects.create(**_execution_work_fields(scope))
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    run_model.objects.create(**_v1_run_fields(scope, work, status, **overrides))
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_run_configuration_fingerprint_bound_to_configuration_failure() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()

        def _failed(**overrides: object) -> dict[str, object]:
            work = work_model.objects.create(**_execution_work_fields(scope))
            base = _v1_run_fields(
                scope,
                work,
                'failed',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
            )
            base.update(overrides)

            return base

        valid = run_model.objects.create(
            **_failed(
                failure_class='configuration', failure_code='model_policy_unavailable', configuration_fingerprint=HEX64
            )
        )

        assert run_model.objects.filter(id=valid.id).count() == 1

        illegal = [
            _failed(
                failure_class='configuration', failure_code='model_policy_unavailable', configuration_fingerprint=''
            ),
            _failed(
                failure_class='configuration',
                failure_code='model_policy_unavailable',
                configuration_fingerprint=HEX64_UPPER,
            ),
            _failed(
                failure_class='provider_transient', failure_code='provider_timeout', configuration_fingerprint=HEX64
            ),
        ]

        for shape in illegal:
            with pytest.raises(IntegrityError):
                with transaction.atomic():
                    run_model.objects.create(**shape)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_unique_work_fencing_token_for_v1() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()
        work = work_model.objects.create(**_execution_work_fields(scope))

        run_model.objects.create(
            **_v1_run_fields(
                scope,
                work,
                'failed',
                fencing_token=5,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
                failure_class='provider_transient',
                failure_code='provider_timeout',
            )
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                run_model.objects.create(
                    **_v1_run_fields(
                        scope,
                        work,
                        'running',
                        fencing_token=5,
                        lease_owner='owner',
                        started_at=now,
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=120),
                    )
                )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_allows_null_fencing_token_duplicates_for_v1() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()
        work = work_model.objects.create(**_execution_work_fields(scope))

        first = run_model.objects.create(**_v1_run_fields(scope, work, 'queued', dispatched_at=now))
        second = run_model.objects.create(**_v1_run_fields(scope, work, 'queued', dispatched_at=now))

        assert run_model.objects.filter(id__in=[first.id, second.id], fencing_token__isnull=True).count() == 2
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_at_most_one_running_v1_run_per_work() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(new_apps)
        work_model = new_apps.get_model('core', 'WorkflowWork')
        run_model = new_apps.get_model('core', 'WorkflowRun')
        now = timezone.now()
        work = work_model.objects.create(**_execution_work_fields(scope))

        run_model.objects.create(
            **_v1_run_fields(
                scope,
                work,
                'running',
                fencing_token=1,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
            )
        )
        succeeded = run_model.objects.create(
            **_v1_run_fields(
                scope,
                work,
                'succeeded',
                fencing_token=2,
                lease_owner='owner',
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=120),
                finished_at=now + timedelta(seconds=10),
            )
        )

        assert run_model.objects.filter(id=succeeded.id).count() == 1

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                run_model.objects.create(
                    **_v1_run_fields(
                        scope,
                        work,
                        'running',
                        fencing_token=3,
                        lease_owner='owner',
                        started_at=now,
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=120),
                    )
                )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_v0_rows_keep_defaults_and_stay_readable() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0034)
        old_apps = executor.loader.project_state(MIGRATE_0034).apps
        scope = _create_historical_0032b_scope(old_apps)
        work = _create_historical_work(old_apps, scope, disposition='complete')
        linked_succeeded = _create_historical_run(old_apps, scope, work=work, status='succeeded')
        unlinked_queued = _create_historical_run(old_apps, scope, work=None, status='queued')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0035)
        new_apps = executor.loader.project_state(MIGRATE_0035).apps
        run_model = new_apps.get_model('core', 'WorkflowRun')
        migrated_linked = run_model.objects.get(id=linked_succeeded.id)
        migrated_unlinked = run_model.objects.get(id=unlinked_queued.id)

        for migrated in (migrated_linked, migrated_unlinked):
            assert migrated.execution_contract_version == 0
            assert migrated.origin == 'legacy'
            assert migrated.fencing_token is None
            assert migrated.lease_owner == ''
            assert migrated.dispatched_at is None
            assert migrated.lease_expires_at is None
            assert migrated.heartbeat_at is None
            assert migrated.failure_class == ''
            assert migrated.failure_code == ''
            assert migrated.configuration_fingerprint == ''
            assert migrated.failure_reason == 'legacy detail'
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('status', ['queued', 'running'])
def test_0035_activation_fails_closed_on_linked_v0_active_run(status: str) -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0034)
        old_apps = executor.loader.project_state(MIGRATE_0034).apps
        scope = _create_historical_0032b_scope(old_apps)
        work = _create_historical_work(old_apps, scope, disposition='required')
        run = _create_historical_run(old_apps, scope, work=work, status=status)

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError):
            executor.migrate(MIGRATE_0035)

        reloaded = MigrationExecutor(connection)

        assert MIGRATION_0035_NODE not in reloaded.loader.applied_migrations
    finally:
        old_apps.get_model('core', 'WorkflowRun').objects.filter(id=run.id).delete()
        old_apps.get_model('core', 'WorkflowWork').objects.filter(id=work.id).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_reverse_restores_pre_execution_schema() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0034)
        old_apps = executor.loader.project_state(MIGRATE_0034).apps
        scope = _create_historical_0032b_scope(old_apps)
        work = _create_historical_work(old_apps, scope, disposition='complete')
        legacy_run = _create_historical_run(old_apps, scope, work=work, status='succeeded')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0035)

        assert _execution_column('execution_state') is not None

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0034)

        assert _execution_column('execution_state') is None

        reverted_run = old_apps.get_model('core', 'WorkflowRun').objects.get(id=legacy_run.id)

        assert reverted_run.status == 'succeeded'
        assert reverted_run.failure_reason == 'legacy detail'

        post_reverse_work = _create_historical_work(old_apps, scope, disposition='required')

        assert old_apps.get_model('core', 'WorkflowWork').objects.filter(id=post_reverse_work.id).count() == 1

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0035)

        assert _execution_column('execution_state') is not None

        reapplied_apps = executor.loader.project_state(MIGRATE_0035).apps
        reapplied_run_model = reapplied_apps.get_model('core', 'WorkflowRun')

        assert reapplied_run_model.objects.get(id=legacy_run.id).execution_contract_version == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_creates_execution_indexes() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        work_indexdefs = _table_indexdefs('core_workflowwork')
        run_indexdefs = _table_indexdefs('core_workflowrun')

        expected_work = [
            '(organization_id, project_id, execution_state, next_retry_at)',
            '(organization_id, project_id, work_type, execution_state)',
            '(execution_state, lease_expires_at)',
        ]
        expected_run = [
            '(work_id, status, created_at)',
            '(work_id, fencing_token)',
            '(organization_id, project_id, failure_class, finished_at)',
        ]

        for columns in expected_work:
            assert any(columns in definition for definition in work_indexdefs)

        for columns in expected_run:
            assert any(columns in definition for definition in run_indexdefs)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


MIGRATE_0036 = [('core', '0036_distillation_coverage')]
MIGRATION_0036_NODE = ('core', '0036_distillation_coverage')
MIGRATE_0037 = [('core', '0037_distillation_stage_policy_role_coord')]
MIGRATION_0037_NODE = ('core', '0037_distillation_stage_policy_role_coord')

_DISTILLATION_TABLES = (
    'core_distillationwindow',
    'core_distillationchunk',
    'core_distillationstage',
    'core_distillationobservationcoverage',
    'core_memorycandidatesource',
)

_DISTILLATION_CONSTRAINTS = (
    'core_distill_window_scope_hash_uniq',
    'core_distill_window_bounds_ck',
    'core_distill_window_obs_count_pos',
    'core_distill_window_budget_pos',
    'core_distill_window_reduction_target_pos',
    'core_distill_window_input_hash_hex',
    'core_distill_window_contract_ck',
    'core_distill_window_chunk_contract_ck',
    'core_distill_chunk_window_ordinal_uniq',
    'core_distill_chunk_window_hash_uniq',
    'core_distill_chunk_sequence_bounds_ck',
    'core_distill_chunk_obs_count_pos',
    'core_distill_chunk_input_hash_hex',
    'core_distill_stage_key_uniq',
    'core_distill_stage_coord_uniq',
    'core_distill_stage_extract_shape_ck',
    'core_distill_stage_reduce_shape_ck',
    'core_distill_stage_status_shape_ck',
    'core_distill_stage_policy_version_pos',
    'core_distill_stage_target_key_hex',
    'core_distill_stage_stage_key_hex',
    'core_distill_stage_input_hash_hex',
    'core_distill_coverage_window_obs_uniq',
    'core_distill_coverage_window_seq_uniq',
    'core_distill_coverage_digest_hex',
    'core_distill_coverage_seq_pos',
    'core_candidate_source_uniq',
    'core_candidate_source_anchors_hex',
    'core_memory_candidate_decision_ver_ck',
)

_DISTILLATION_INDEX_COLUMNS = {
    'core_distillationwindow': '(organization_id, project_id, session_id, upper_sequence_inclusive)',
    'core_distillationchunk': '(organization_id, project_id, window_id, ordinal)',
    'core_memorycandidatesource': '(organization_id, project_id, candidate_id)',
}


def _pg_table_exists(table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT to_regclass(%s)', [f'public.{table}'])
        row = cursor.fetchone()

    return row is not None and row[0] is not None


def _pg_constraint_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM pg_constraint WHERE conname = %s', [name])

        return cursor.fetchone() is not None


def _pg_index_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM pg_indexes WHERE indexname = %s', [name])

        return cursor.fetchone() is not None


def _pg_column(table: str, column: str) -> tuple[str | None, str] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            [table, column],
        )
        row = cursor.fetchone()

    return (row[0], row[1]) if row is not None else None


def _create_historical_0036_stage_fixture(
    historical_apps: Apps,
    suffix: str,
    *,
    policy_role: str = 'primary',
    stage_key: str | None = None,
) -> tuple[type[models.Model], models.Model]:
    scope = _create_historical_0032b_scope(historical_apps)
    session = _create_historical_session(historical_apps, scope, f'distill-stage-{suffix}')
    work = _create_historical_work(
        historical_apps,
        scope,
        work_type='session_distillation',
        subject_type='agent_session',
        subject_id=session.id,
    )
    secret_model = historical_apps.get_model('model_policy', 'ProviderSecret')
    policy_model = historical_apps.get_model('model_policy', 'ModelPolicy')
    secret = secret_model.objects.create(
        organization=scope['organization'],
        team=scope['team'],
        name=f'distill-secret-{suffix}',
        provider='openai',
        scope='team',
        current_version=1,
    )
    policy = policy_model.objects.create(
        organization=scope['organization'],
        team=scope['team'],
        project=scope['project'],
        name=f'distill-policy-{suffix}',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=2,
    )
    window_model = historical_apps.get_model('core', 'DistillationWindow')
    window = window_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        work=work,
        session=session,
        contract_version=1,
        lower_sequence_exclusive=0,
        upper_sequence_inclusive=1,
        observation_count=1,
        input_hash=('a' * 63) + suffix[-1],
        chunk_char_budget=8000,
        reduction_target=12,
        chunk_contract_version=1,
    )
    chunk_model = historical_apps.get_model('core', 'DistillationChunk')
    chunk = chunk_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        window=window,
        ordinal=0,
        first_sequence=1,
        last_sequence=1,
        observation_count=1,
        input_manifest={'schema': 'distillation_chunk_manifest.v1', 'ordinal': 0, 'observations': []},
        input_hash=('b' * 63) + suffix[-1],
    )
    stage_model = historical_apps.get_model('core', 'DistillationStage')
    stage = stage_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        window=window,
        chunk=chunk,
        stage_kind='extract',
        level=0,
        ordinal=0,
        target_key=('c' * 63) + suffix[-1],
        stage_key=stage_key or (uuid.uuid4().hex * 2),
        input_hash=('d' * 63) + suffix[-1],
        input_manifest={'chunk_ordinal': 0},
        prompt_contract='distill_extract.v1',
        policy=policy,
        policy_version=2,
        policy_role=policy_role,
        status='required',
        attempt_count=0,
    )

    return stage_model, stage


@pytest.mark.django_db(transaction=True)
def test_0036_depends_on_0035_and_creates_distillation_tables() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0036)
        migration = executor.loader.graph.nodes[MIGRATION_0036_NODE]

        assert MIGRATION_0035_NODE in migration.dependencies
        for table in _DISTILLATION_TABLES:
            assert _pg_table_exists(table)
        for name in _DISTILLATION_CONSTRAINTS:
            assert _pg_constraint_exists(name)
        assert _pg_index_exists('core_distill_stage_target_complete_uniq')
        for table, columns in _DISTILLATION_INDEX_COLUMNS.items():
            assert any(columns in definition for definition in _table_indexdefs(table))
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0036_adds_candidate_decision_contract_column_and_enum() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0036)
        column = _pg_column('core_memorycandidate', 'decision_work_contract_version')

        assert column is not None
        assert column[0] is not None and '0' in column[0]

        new_apps = executor.loader.project_state(MIGRATE_0036).apps
        work_model = new_apps.get_model('core', 'WorkflowWork')
        work_type_choices = dict(work_model._meta.get_field('work_type').choices)
        subject_choices = dict(work_model._meta.get_field('subject_type').choices)

        assert 'candidate_decision' in work_type_choices
        assert 'memory_candidate' in subject_choices
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0036_reverse_drops_tables_and_reapply_restores_them() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0036)
        assert all(_pg_table_exists(table) for table in _DISTILLATION_TABLES)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0035)

        assert not any(_pg_table_exists(table) for table in _DISTILLATION_TABLES)
        assert _pg_column('core_memorycandidate', 'decision_work_contract_version') is None

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0036)

        assert all(_pg_table_exists(table) for table in _DISTILLATION_TABLES)
        assert _pg_column('core_memorycandidate', 'decision_work_contract_version') is not None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0036_fresh_database_accepts_window_and_chunk_insert() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0036)
        new_apps = executor.loader.project_state(MIGRATE_0036).apps
        scope = _create_historical_0032b_scope(new_apps)
        session = _create_historical_session(new_apps, scope, 'window-fresh-session')
        work = _create_historical_work(
            new_apps,
            scope,
            disposition='required',
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=session.id,
        )
        window_model = new_apps.get_model('core', 'DistillationWindow')
        chunk_model = new_apps.get_model('core', 'DistillationChunk')

        window = window_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            work=work,
            session=session,
            contract_version=1,
            lower_sequence_exclusive=0,
            upper_sequence_inclusive=1,
            observation_count=1,
            input_hash='a' * 64,
            chunk_char_budget=8000,
            reduction_target=12,
            chunk_contract_version=1,
        )
        chunk_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            window=window,
            ordinal=0,
            first_sequence=1,
            last_sequence=1,
            observation_count=1,
            input_manifest={'schema': 'distillation_chunk_manifest.v1', 'ordinal': 0, 'observations': []},
            input_hash='b' * 64,
        )

        assert window_model.objects.filter(work=work).count() == 1
        assert chunk_model.objects.filter(window=window).count() == 1
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0037_policy_role_coordinate_constraint_round_trip() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    assert MIGRATION_0037_NODE in executor.loader.graph.nodes

    migration = executor.loader.graph.nodes[MIGRATION_0037_NODE]
    assert MIGRATION_0036_NODE in migration.dependencies

    def create_variant(
        stage_model: type[models.Model],
        base: models.Model,
        *,
        policy_role: str,
        stage_key: str,
        suffix: str,
    ) -> models.Model:
        return stage_model.objects.create(
            organization_id=base.organization_id,
            project_id=base.project_id,
            team_id=base.team_id,
            window_id=base.window_id,
            chunk_id=base.chunk_id,
            stage_kind=base.stage_kind,
            level=base.level,
            ordinal=base.ordinal,
            target_key=('e' * 63) + suffix,
            stage_key=stage_key,
            input_hash=('f' * 63) + suffix,
            input_manifest=base.input_manifest,
            prompt_contract=base.prompt_contract,
            policy_id=base.policy_id,
            policy_version=base.policy_version,
            policy_role=policy_role,
            status='required',
            attempt_count=0,
        )

    try:
        executor.migrate(MIGRATE_0036)
        apps_0036 = executor.loader.project_state(MIGRATE_0036).apps
        stage_model_0036, primary = _create_historical_0036_stage_fixture(
            apps_0036,
            uuid.uuid4().hex,
            stage_key='1' * 64,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            create_variant(
                stage_model_0036,
                primary,
                policy_role='fallback',
                stage_key='2' * 64,
                suffix='0',
            )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0037)
        apps_0037 = executor.loader.project_state(MIGRATE_0037).apps
        stage_model_0037 = apps_0037.get_model('core', 'DistillationStage')
        primary_0037 = stage_model_0037.objects.get(id=primary.id)
        fallback = create_variant(
            stage_model_0037,
            primary_0037,
            policy_role='fallback',
            stage_key='2' * 64,
            suffix='1',
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            create_variant(
                stage_model_0037,
                primary_0037,
                policy_role='primary',
                stage_key='3' * 64,
                suffix='2',
            )

        with transaction.atomic(), pytest.raises(IntegrityError):
            create_variant(
                stage_model_0037,
                primary_0037,
                policy_role='fallback',
                stage_key='6' * 64,
                suffix='5',
            )

        stage_model_0037.objects.filter(id=fallback.id).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0036)
        apps_reversed = executor.loader.project_state(MIGRATE_0036).apps
        stage_model_reversed = apps_reversed.get_model('core', 'DistillationStage')
        primary_reversed = stage_model_reversed.objects.get(id=primary.id)

        with transaction.atomic(), pytest.raises(IntegrityError):
            create_variant(
                stage_model_reversed,
                primary_reversed,
                policy_role='fallback',
                stage_key='4' * 64,
                suffix='3',
            )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0037)
        apps_reapplied = executor.loader.project_state(MIGRATE_0037).apps
        stage_model_reapplied = apps_reapplied.get_model('core', 'DistillationStage')
        primary_reapplied = stage_model_reapplied.objects.get(id=primary.id)
        fallback_reapplied = create_variant(
            stage_model_reapplied,
            primary_reapplied,
            policy_role='fallback',
            stage_key='5' * 64,
            suffix='4',
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            create_variant(
                stage_model_reapplied,
                primary_reapplied,
                policy_role='fallback',
                stage_key='7' * 64,
                suffix='6',
            )

        assert stage_model_reapplied.objects.filter(id__in=[primary.id, fallback_reapplied.id]).count() == 2
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)
