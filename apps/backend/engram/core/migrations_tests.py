import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = [('core', '0031_workflowrun_active_daily_digest_unique')]
MIGRATE_TO = [('core', '0032_workflowwork_sequence_expand')]


@pytest.mark.django_db(transaction=True)
def test_0032_expand_preserves_0031_rows_without_backfill() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps

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
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps

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
