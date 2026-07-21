import importlib
import uuid
from datetime import datetime, timedelta

import pytest
from django.apps.registry import Apps
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATE_0034 = [('core', '0034_memory_loop_input_contract')]
MIGRATE_0035 = [('core', '0035_workflow_work_execution')]
MIGRATE_0036 = [('core', '0036_distillation_coverage')]
MIGRATE_0037 = [('core', '0037_distillation_stage_policy_role_coord')]
MIGRATE_0038 = [('core', '0038_atomic_memory_transitions')]
MIGRATE_0039 = [('core', '0039_import_provenance')]
MIGRATE_0040 = [
    ('core', '0040_curation_decision'),
    ('model_policy', '0004_alter_providercallrecord_result'),
]
MIGRATE_0042 = [
    ('core', '0042_memory_confidence_decayed_at'),
    ('model_policy', '0004_alter_providercallrecord_result'),
]
MIGRATE_0043 = [
    ('core', '0043_curation_decision_evidence_context'),
    ('model_policy', '0004_alter_providercallrecord_result'),
]
MIGRATE_0044 = [('core', '0044_memory_last_confirmed_at')]
MIGRATION_0044_NODE = ('core', '0044_memory_last_confirmed_at')
MIGRATE_0045 = [('core', '0045_agent_proposal_source')]
MIGRATION_0045_NODE = ('core', '0045_agent_proposal_source')
MIGRATION_0045_MODULE = 'engram.core.migrations.0045_agent_proposal_source'
MIGRATE_0046 = [('core', '0046_merge_20260721_1032')]
MIGRATE_0047 = [('core', '0047_distill_stage_coord_prompt_contract')]


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
def test_0038_accepts_retrieval_document_insert_from_0037_model() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0037)
        old_apps = executor.loader.project_state(MIGRATE_0037).apps
        organization_model = old_apps.get_model('core', 'Organization')
        project_model = old_apps.get_model('core', 'Project')
        memory_model = old_apps.get_model('core', 'Memory')
        memory_version_model = old_apps.get_model('core', 'MemoryVersion')
        retrieval_document_model = old_apps.get_model('core', 'RetrievalDocument')
        suffix = uuid.uuid4().hex
        organization = organization_model.objects.create(
            name=f'Rolling {suffix}',
            slug=f'rolling-{suffix}',
        )
        project = project_model.objects.create(
            organization=organization,
            name=f'Rolling project {suffix}',
            slug=f'rolling-project-{suffix}',
        )
        memory = memory_model.objects.create(
            organization=organization,
            project=project,
            title='Created by a 0037 writer',
            body='must remain insertable after the expand migration',
            current_version=1,
        )
        version = memory_version_model.objects.create(
            organization=organization,
            project=project,
            memory=memory,
            version=1,
            body=memory.body,
            content_hash='f' * 64,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0038)

        document = retrieval_document_model.objects.create(
            organization=organization,
            project=project,
            memory=memory,
            memory_version=version,
            full_text=memory.body,
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, column_default, is_nullable
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'core_retrievaldocument'
                  AND column_name IN (
                      'exact_projection_hash',
                      'embedding_projection_hash'
                  )
                """,
            )
            columns = {name: (column_default, is_nullable) for name, column_default, is_nullable in cursor.fetchall()}

        assert set(columns) == {
            'exact_projection_hash',
            'embedding_projection_hash',
        }
        assert all(column_default is not None for column_default, _nullable in columns.values())
        assert all(nullable == 'NO' for _column_default, nullable in columns.values())

        new_apps = executor.loader.project_state(MIGRATE_0038).apps
        migrated = new_apps.get_model('core', 'RetrievalDocument').objects.get(id=document.id)
        assert migrated.projection_contract_version == 0
        assert migrated.exact_projection_hash == ''
        assert migrated.embedding_projection_hash == ''
        assert migrated.embedding_projected_at is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0035_reverse_refuses_v1_execution_history() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0035)
        apps_0035 = executor.loader.project_state(MIGRATE_0035).apps
        scope = _create_historical_0032b_scope(apps_0035)
        work_model = apps_0035.get_model('core', 'WorkflowWork')
        run_model = apps_0035.get_model('core', 'WorkflowRun')
        work = work_model.objects.create(
            **_execution_work_fields(
                scope,
                execution_state='terminal_failure',
                fencing_token=9,
                failure_streak=3,
            )
        )
        finished_at = timezone.now()
        run = run_model.objects.create(
            **_v1_run_fields(
                scope,
                work,
                'failed',
                origin='automatic',
                fencing_token=9,
                lease_owner='migration-test-worker',
                started_at=finished_at - timedelta(seconds=1),
                finished_at=finished_at,
                failure_class='provider_transient',
                failure_code='provider_timeout',
            )
        )

        with pytest.raises(RuntimeError, match='cannot reverse 0035.*v1'):
            MigrationExecutor(connection).migrate(MIGRATE_0034)

        preserved_work = work_model.objects.get(id=work.id)
        preserved_run = run_model.objects.get(id=run.id)
        assert preserved_work.execution_state == 'terminal_failure'
        assert preserved_work.fencing_token == 9
        assert preserved_work.failure_streak == 3
        assert preserved_run.execution_contract_version == 1
        assert preserved_run.fencing_token == 9
        assert preserved_run.failure_class == 'provider_transient'
        assert preserved_run.failure_code == 'provider_timeout'
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0036_reverse_refuses_populated_distillation_lineage() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0036)
        apps_0036 = executor.loader.project_state(MIGRATE_0036).apps
        _stage_model, stage = _create_historical_0036_stage_fixture(apps_0036, 'a')
        window = stage.window
        session = window.session
        scope = {
            'organization': window.organization,
            'project': window.project,
            'team': window.team,
            'agent': session.agent,
        }
        observation = _create_historical_observation(
            apps_0036,
            scope,
            session,
            'a' * 64,
            timezone.now(),
            session_sequence=1,
        )
        candidate = apps_0036.get_model('core', 'MemoryCandidate').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            source_observation=observation,
            title='Reverse guard candidate',
            body='Durable candidate provenance',
            content_hash='b' * 64,
            decision_work_contract_version=1,
        )
        coverage = apps_0036.get_model('core', 'DistillationObservationCoverage').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            window=window,
            observation=observation,
            session_sequence=1,
            observation_digest='c' * 64,
            outcome='signal',
            deciding_stage=stage,
        )
        source = apps_0036.get_model('core', 'MemoryCandidateSource').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            candidate=candidate,
            window=window,
            observation=observation,
            stage=stage,
            anchors={'schema': 'distillation_candidate_source.v1'},
            anchors_hash='d' * 64,
        )

        with pytest.raises(RuntimeError, match='cannot reverse 0036.*distillation'):
            MigrationExecutor(connection).migrate(MIGRATE_0035)

        assert apps_0036.get_model('core', 'DistillationWindow').objects.filter(id=window.id).exists()
        assert apps_0036.get_model('core', 'DistillationStage').objects.filter(id=stage.id).exists()
        assert apps_0036.get_model('core', 'DistillationObservationCoverage').objects.filter(id=coverage.id).exists()
        assert apps_0036.get_model('core', 'MemoryCandidateSource').objects.filter(id=source.id).exists()
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0040_reverse_refuses_populated_curation_decisions() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0040)
        apps_0040 = executor.loader.project_state(MIGRATE_0040).apps
        scope = _create_historical_0032b_scope(apps_0040)
        session = _create_historical_session(apps_0040, scope, 'migration-0040-reverse-guard')
        observation = _create_historical_observation(
            apps_0040,
            scope,
            session,
            'e' * 64,
            timezone.now(),
            session_sequence=1,
        )
        candidate = apps_0040.get_model('core', 'MemoryCandidate').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            source_observation=observation,
            title='Decision reverse guard candidate',
            body='The append-only decision must survive a rejected reverse.',
            content_hash='f' * 64,
            decision_work_contract_version=1,
        )
        work = apps_0040.get_model('core', 'WorkflowWork').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            work_type='candidate_decision',
            subject_type='memory_candidate',
            subject_id=candidate.id,
            contract_version=1,
            occurrence_key='',
            input_fingerprint='1' * 64,
            input_snapshot={'schema': 'candidate_decision_input/v1'},
            disposition='complete',
            resolution_reason='succeeded',
            resolved_at=timezone.now(),
            execution_state='settled',
        )
        decision = apps_0040.get_model('core', 'CurationDecision').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            work=work,
            candidate=candidate,
            contract_version=1,
            input_fingerprint=work.input_fingerprint,
            evidence_manifest_hash='2' * 64,
            comparison_manifest_hash='3' * 64,
            outcome='reject_candidate',
            reason_code='noise_empty',
            redacted_reason='empty after deterministic normalization',
            effective_visibility_scope='team',
            effective_team=scope['team'],
            evidence_tier='none',
            payload_hash='4' * 64,
        )

        target = MIGRATE_0039 + [('model_policy', '0004_alter_providercallrecord_result')]
        with pytest.raises(RuntimeError, match='cannot reverse 0040.*CurationDecision'):
            MigrationExecutor(connection).migrate(target)

        assert apps_0040.get_model('core', 'CurationDecision').objects.filter(id=decision.id).exists()
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_0043_accepts_curation_decision_insert_from_0042_model() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0042)
        old_apps = executor.loader.project_state(MIGRATE_0042).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'migration-0043-defaults')
        observation = _create_historical_observation(
            old_apps,
            scope,
            session,
            'e' * 64,
            timezone.now(),
            session_sequence=1,
        )
        candidate = old_apps.get_model('core', 'MemoryCandidate').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            source_observation=observation,
            title='Rolling defaults candidate',
            body='The 0042 writer omits the new decision columns.',
            content_hash='f' * 64,
            decision_work_contract_version=1,
        )
        work = old_apps.get_model('core', 'WorkflowWork').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            work_type='candidate_decision',
            subject_type='memory_candidate',
            subject_id=candidate.id,
            contract_version=1,
            occurrence_key='',
            input_fingerprint='1' * 64,
            input_snapshot={'schema': 'candidate_decision_input/v1'},
            disposition='complete',
            resolution_reason='succeeded',
            resolved_at=timezone.now(),
            execution_state='settled',
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0043)

        decision = old_apps.get_model('core', 'CurationDecision').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            work=work,
            candidate=candidate,
            contract_version=1,
            input_fingerprint=work.input_fingerprint,
            evidence_manifest_hash='2' * 64,
            comparison_manifest_hash='3' * 64,
            outcome='reject_candidate',
            reason_code='noise_empty',
            redacted_reason='empty after deterministic normalization',
            effective_visibility_scope='team',
            effective_team=scope['team'],
            evidence_tier='none',
            payload_hash='4' * 64,
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, column_default, is_nullable
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'core_curationdecision'
                  AND column_name IN (
                      'applicability',
                      'evidence_membership'
                  )
                """,
            )
            columns = {name: (column_default, is_nullable) for name, column_default, is_nullable in cursor.fetchall()}

        assert set(columns) == {
            'applicability',
            'evidence_membership',
        }
        assert all(column_default is not None for column_default, _nullable in columns.values())
        assert all(nullable == 'NO' for _column_default, nullable in columns.values())

        new_apps = executor.loader.project_state(MIGRATE_0043).apps
        migrated = new_apps.get_model('core', 'CurationDecision').objects.get(id=decision.id)
        assert migrated.applicability == ''
        assert migrated.evidence_membership == {}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)


def _create_historical_0044_memory(
    historical_apps: Apps,
    scope: dict[str, object],
    **overrides: object,
) -> models.Model:
    memory_model = historical_apps.get_model('core', 'Memory')
    kwargs: dict[str, object] = {
        'organization': scope['organization'],
        'project': scope['project'],
        'title': 'Reverse 0044 memory',
        'body': 'Reverse 0044 body',
    }
    kwargs.update(overrides)

    return memory_model.objects.create(**kwargs)


@pytest.mark.django_db(transaction=True)
def test_reverse_0044_allowed_when_no_confirmation_history() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    assert MIGRATION_0044_NODE in executor.loader.graph.nodes
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0044)
        apps_0044 = executor.loader.project_state(MIGRATE_0044).apps
        scope = _create_historical_0032b_scope(apps_0044)
        _create_historical_0044_memory(apps_0044, scope)
        migration = executor.loader.graph.nodes[MIGRATION_0044_NODE]
        assert migration.operations[-1].__class__.__name__ == 'RunPython'

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0040)
        apps_0040 = executor.loader.project_state(MIGRATE_0040).apps
        assert 'last_confirmed_at' not in {field.name for field in apps_0040.get_model('core', 'Memory')._meta.fields}
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_reverse_0044_blocked_when_last_confirmed_at_set() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    assert MIGRATION_0044_NODE in executor.loader.graph.nodes
    confirmed_at = timezone.now()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0044)
        apps_0044 = executor.loader.project_state(MIGRATE_0044).apps
        scope = _create_historical_0032b_scope(apps_0044)
        memory = _create_historical_0044_memory(apps_0044, scope, last_confirmed_at=confirmed_at)

        with pytest.raises(RuntimeError, match='0044'):
            MigrationExecutor(connection).migrate(MIGRATE_0040)

        apps_still_0044 = MigrationExecutor(connection).loader.project_state(MIGRATE_0044).apps
        assert 'last_confirmed_at' in {field.name for field in apps_still_0044.get_model('core', 'Memory')._meta.fields}
        reloaded = apps_still_0044.get_model('core', 'Memory').objects.get(id=memory.id)
        assert reloaded.last_confirmed_at == confirmed_at
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_reverse_0044_blocked_when_memoryconfirmed_ledger_exists() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    assert MIGRATION_0044_NODE in executor.loader.graph.nodes
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0044)
        apps_0044 = executor.loader.project_state(MIGRATE_0044).apps
        scope = _create_historical_0032b_scope(apps_0044)
        apps_0044.get_model('core', 'AuditEvent').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            event_type='MemoryConfirmed',
            actor_type='agent',
            actor_id='reverse-0044-actor',
            target_type='memory',
            target_id=str(uuid.uuid4()),
        )

        with pytest.raises(RuntimeError, match='0044'):
            MigrationExecutor(connection).migrate(MIGRATE_0040)
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


def test_0045_reverse_guard_is_last_and_refuses_agent_proposals() -> None:
    migration_module = importlib.import_module(MIGRATION_0045_MODULE)
    migration = migration_module.Migration
    operation = migration.operations[-1]
    assert operation.__class__.__name__ == 'RunPython'

    class _SourceManager:
        @staticmethod
        def filter(**kwargs: object) -> '_SourceManager':
            assert kwargs == {'source_kind': 'agent_proposal'}
            return _SourceManager()

        @staticmethod
        def exists() -> bool:
            return True

    class _SourceModel:
        objects = _SourceManager()

    class _Apps:
        @staticmethod
        def get_model(app_label: str, model_name: str) -> type[_SourceModel]:
            assert (app_label, model_name) == ('core', 'MemoryCandidateSource')
            return _SourceModel

    assert operation.reverse_code is not None
    with pytest.raises(RuntimeError, match='cannot reverse 0045'):
        operation.reverse_code(_Apps(), None)


@pytest.mark.django_db(transaction=True)
def test_0045_forward_preserves_rows_and_reverse_guards_agent_proposal() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    assert MIGRATION_0045_NODE in executor.loader.graph.nodes
    try:
        executor.migrate(MIGRATE_0044)
        old_apps = executor.loader.project_state(MIGRATE_0044).apps
        scope = _create_historical_0032b_scope(old_apps)
        session = _create_historical_session(old_apps, scope, 'migration-0045-session')
        observation = _create_historical_observation(
            old_apps,
            scope,
            session,
            'a' * 64,
            timezone.now(),
            session_sequence=1,
        )
        candidate_model = old_apps.get_model('core', 'MemoryCandidate')
        distill_candidate = candidate_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            source_observation=observation,
            title='Legacy distillation candidate',
            body='Legacy distillation body',
            content_hash='b' * 64,
            decision_work_contract_version=1,
        )
        _stage_model, stage = _create_historical_0036_stage_fixture(old_apps, '0045')
        source_model = old_apps.get_model('core', 'MemoryCandidateSource')
        distill_source = source_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            candidate=distill_candidate,
            window=stage.window,
            observation=observation,
            stage=stage,
            anchors={'schema': 'distillation'},
            anchors_hash='c' * 64,
        )
        observation_source = old_apps.get_model('core', 'ObservationSource').objects.create(
            organization=scope['organization'],
            project=scope['project'],
            observation=observation,
            source_type='claude_mem',
            source_id='migration-0045:import',
        )
        import_candidate = candidate_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            source_observation=observation,
            title='Legacy import candidate',
            body='Legacy import body',
            content_hash='d' * 64,
            decision_work_contract_version=1,
        )
        import_source = source_model.objects.create(
            organization=scope['organization'],
            project=scope['project'],
            team=scope['team'],
            candidate=import_candidate,
            observation=observation,
            source_kind='import',
            import_source=observation_source,
            anchors={'schema': 'import_candidate_source.v1'},
            anchors_hash='e' * 64,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0045)
        new_apps = executor.loader.project_state(MIGRATE_0045).apps
        new_source_model = new_apps.get_model('core', 'MemoryCandidateSource')
        assert new_source_model.objects.filter(id=distill_source.id).exists()
        assert new_source_model.objects.filter(id=import_source.id).exists()

        agent_candidate = new_apps.get_model('core', 'MemoryCandidate').objects.create(
            organization_id=scope['organization'].id,
            project_id=scope['project'].id,
            team_id=scope['team'].id,
            title='Agent candidate',
            body='Agent body',
            content_hash='f' * 64,
            decision_work_contract_version=1,
        )
        agent_source = new_source_model.objects.create(
            organization_id=scope['organization'].id,
            project_id=scope['project'].id,
            team_id=scope['team'].id,
            candidate=agent_candidate,
            source_kind='agent_proposal',
            anchors={'schema': 'agent_proposal_source.v1'},
            anchors_hash='1' * 64,
        )

        with pytest.raises(RuntimeError, match='cannot reverse 0045'):
            MigrationExecutor(connection).migrate(MIGRATE_0044)

        agent_source.delete()
        MigrationExecutor(connection).migrate(MIGRATE_0044)
        reverted_apps = MigrationExecutor(connection).loader.project_state(MIGRATE_0044).apps
        reverted_source_model = reverted_apps.get_model('core', 'MemoryCandidateSource')
        assert reverted_source_model.objects.filter(id=distill_source.id).exists()
        assert reverted_source_model.objects.filter(id=import_source.id).exists()
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


def _create_historical_reduce_scope(historical_apps: Apps, suffix: str) -> dict[str, object]:
    scope = _create_historical_0032b_scope(historical_apps)
    session = _create_historical_session(historical_apps, scope, f'distill-reduce-{suffix}')
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

    return {'scope': scope, 'policy': policy, 'window': window}


def _create_historical_reduce_stage(
    historical_apps: Apps,
    fixture: dict[str, object],
    *,
    prompt_contract: str,
) -> models.Model:
    stage_model = historical_apps.get_model('core', 'DistillationStage')
    scope = fixture['scope']
    window = fixture['window']
    policy = fixture['policy']

    return stage_model.objects.create(
        organization=scope['organization'],
        project=scope['project'],
        team=scope['team'],
        window=window,
        chunk=None,
        stage_kind='reduce',
        level=1,
        ordinal=0,
        target_key=uuid.uuid4().hex + uuid.uuid4().hex,
        stage_key=uuid.uuid4().hex + uuid.uuid4().hex,
        input_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        input_manifest={'schema': 'distillation_reduce_manifest.v1', 'level': 1, 'ordinal': 0, 'refs': []},
        prompt_contract=prompt_contract,
        policy=policy,
        policy_version=2,
        policy_role='primary',
        status='required',
        attempt_count=0,
    )


@pytest.mark.django_db(transaction=True)
def test_0047_coord_uniqueness_admits_distinct_prompt_contracts_at_one_coordinate() -> None:
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate(MIGRATE_0046)
        old_apps = executor.loader.project_state(MIGRATE_0046).apps
        fixture = _create_historical_reduce_scope(old_apps, 'a')
        _create_historical_reduce_stage(old_apps, fixture, prompt_contract='distill_reduce.v1')

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_reduce_stage(old_apps, fixture, prompt_contract='distill_reduce.v2')

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_0047)
        new_apps = executor.loader.project_state(MIGRATE_0047).apps
        new_fixture = _create_historical_reduce_scope(new_apps, 'b')
        v1 = _create_historical_reduce_stage(new_apps, new_fixture, prompt_contract='distill_reduce.v1')
        v2 = _create_historical_reduce_stage(new_apps, new_fixture, prompt_contract='distill_reduce.v2')

        stage_model = new_apps.get_model('core', 'DistillationStage')
        coexisting = stage_model.objects.filter(window=new_fixture['window'], stage_kind='reduce', level=1, ordinal=0)
        assert {row.id for row in coexisting} == {v1.id, v2.id}

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                _create_historical_reduce_stage(new_apps, new_fixture, prompt_contract='distill_reduce.v2')
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaf_nodes)
