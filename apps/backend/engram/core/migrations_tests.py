import uuid

import pytest
from django.apps.registry import Apps
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor

MIGRATE_0031 = [('core', '0031_workflowrun_active_daily_digest_unique')]
MIGRATE_0032 = [('core', '0032_workflowwork_sequence_expand')]
MIGRATE_0032B = [('core', '0032b_agentsession_end_work_db_default')]


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
