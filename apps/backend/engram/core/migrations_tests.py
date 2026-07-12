import importlib
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest
from django.apps.registry import Apps
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from django.db.utils import OperationalError
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
) -> models.Model:
    observation_model = historical_apps.get_model('core', 'Observation')
    observation = observation_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        agent=scope['agent'],
        session=session,
        observation_type='decision',
        title='Backfill observation',
        content_hash=content_hash,
        session_sequence=session_sequence,
        prompt_number=prompt_number,
        observed_at=observed_at,
    )
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
        _create_historical_observation(old_apps, scope, session, 'ordering-2', base + timedelta(seconds=2))
        _create_historical_observation(old_apps, scope, session, 'ordering-3', base + timedelta(seconds=2))
        _create_historical_observation(old_apps, scope, session, 'ordering-4', base + timedelta(seconds=3))

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0033)

        assert _ordered_sequences(old_apps, session.id) == [1, 2, 3, 4]
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
