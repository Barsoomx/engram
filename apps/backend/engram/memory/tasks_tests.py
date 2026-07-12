from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.db import transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

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
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory import tasks as tasks_module
from engram.memory.candidate_ttl import ExpireStaleCandidatesResult
from engram.memory.confidence_decay import DecayMemoryConfidenceResult
from engram.memory.services import MemoryWorkerError
from engram.memory.tasks import (
    decay_memory_confidence,
    distill_session,
    expire_stale_candidates,
    generate_daily_digest,
    generate_weekly_digest,
    process_observation_recorded,
    retry_failed_distillations,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
    resolve_work_no_input,
    resolve_work_succeeded,
)

_VERSIONED_WORK_TASKS = (
    (
        'process_observation_work_v1',
        'engram.memory.process_observation_work_v1',
        celeryconfig.QUEUE_NEAR_REALTIME,
    ),
    (
        'distill_session_work_v1',
        'engram.memory.distill_session_work_v1',
        celeryconfig.QUEUE_BATCH,
    ),
    (
        'generate_daily_digest_work_v1',
        'engram.memory.generate_daily_digest_work_v1',
        celeryconfig.QUEUE_BATCH,
    ),
    (
        'generate_weekly_digest_work_v1',
        'engram.memory.generate_weekly_digest_work_v1',
        celeryconfig.QUEUE_BATCH,
    ),
)

_VERSIONED_WORK_CASES = (
    (
        'process_observation_work_v1',
        WorkflowWorkType.OBSERVATION_PROCESSING,
        'engram.memory.tasks.ProcessObservationRecorded.execute',
    ),
    (
        'distill_session_work_v1',
        WorkflowWorkType.SESSION_DISTILLATION,
        'engram.memory.tasks.run_session_distillation_with_tracking',
    ),
    (
        'generate_daily_digest_work_v1',
        WorkflowWorkType.DAILY_DIGEST,
        'engram.memory.tasks.run_daily_digest_with_tracking',
    ),
    (
        'generate_weekly_digest_work_v1',
        WorkflowWorkType.WEEKLY_DIGEST,
        'engram.memory.tasks.run_weekly_digest_with_tracking',
    ),
)


def test_task_routes_send_ingest_tasks_to_near_realtime_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.process_observation_recorded']['queue'] == (
        celeryconfig.QUEUE_NEAR_REALTIME
    )


def test_task_routes_send_distill_and_digest_tasks_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.distill_session']['queue'] == celeryconfig.QUEUE_BATCH
    assert celeryconfig.task_routes['engram.memory.generate_daily_digest']['queue'] == celeryconfig.QUEUE_BATCH
    assert celeryconfig.task_routes['engram.memory.generate_weekly_digest']['queue'] == celeryconfig.QUEUE_BATCH


def test_versioned_work_tasks_are_registered_with_id_only_names_and_queues() -> None:
    for attribute, task_name, queue in _VERSIONED_WORK_TASKS:
        task = getattr(tasks_module, attribute)

        assert task.name == task_name
        assert task.max_retries == 3
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
        assert celeryconfig.task_routes[task_name]['queue'] == queue


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


def create_observation(
    session: AgentSession,
    *,
    suffix: str = '1',
    session_sequence: int | None = None,
) -> Observation:
    if session_sequence is None:
        session_sequence = Observation.objects.filter(session=session).count() + 1

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
        source_metadata={'event_type': 'post_tool_use'},
        session_sequence=session_sequence,
        observed_at=timezone.now(),
    )


def frozen_input_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def create_required_work(
    session: AgentSession,
    *,
    work_type: str,
) -> WorkflowWork:
    if work_type == WorkflowWorkType.OBSERVATION_PROCESSING:
        observation = create_observation(session, suffix=f'work-{uuid.uuid4()}')
        data = CreateWorkflowWorkInput(
            organization_id=session.organization_id,
            project_id=session.project_id,
            work_type=work_type,
            subject_type=WorkflowSubjectType.OBSERVATION,
            subject_id=observation.id,
            input_snapshot={
                'schema': 'observation_processing_input/v1',
                'observation_id': str(observation.id),
                'observation_digest': observation_content_digest(observation),
                'policy': {
                    'schema': 'hook_work_policy/v1',
                    'realtime_candidates_enabled': True,
                    'legacy_policy_fallback': False,
                },
            },
        )
    elif work_type == WorkflowWorkType.SESSION_DISTILLATION:
        create_observation(
            session,
            suffix=f'session-work-{uuid.uuid4()}',
            session_sequence=1,
        )
        AgentSession.objects.filter(id=session.id).update(observation_sequence_cursor=1)
        data = CreateWorkflowWorkInput(
            organization_id=session.organization_id,
            project_id=session.project_id,
            work_type=work_type,
            subject_type=WorkflowSubjectType.AGENT_SESSION,
            subject_id=session.id,
            input_snapshot={
                'schema': 'session_distillation_input/v1',
                'session_id': str(session.id),
                'lower_sequence_exclusive': 0,
                'upper_sequence_inclusive': 1,
            },
        )
    elif work_type == WorkflowWorkType.DAILY_DIGEST:
        schedule_key = 'daily:2026-07-10'
        memory_id = uuid.uuid4()
        memory_version_id = uuid.uuid4()
        source = {
            'render_position': 0,
            'memory_id': str(memory_id),
            'memory_version_id': str(memory_version_id),
            'version': 1,
            'content_hash': 'legacy-source-hash',
            'server_body_digest': frozen_input_digest([str(memory_version_id), 1, 'Frozen daily body']),
            'visibility_scope': 'project',
            'team_id': None,
            'source_title': 'Frozen daily title',
        }
        data = CreateWorkflowWorkInput(
            organization_id=session.organization_id,
            project_id=session.project_id,
            work_type=work_type,
            subject_type=WorkflowSubjectType.PROJECT,
            subject_id=session.project_id,
            occurrence_key=schedule_key,
            input_snapshot={
                'schema': 'daily_digest_input/v1',
                'project_id': str(session.project_id),
                'schedule_key': schedule_key,
                'window_start': '2026-07-10T00:00:00Z',
                'window_end': '2026-07-11T00:00:00Z',
                'visibility_policy': 'digest_visibility/v1',
                'allowed_team_ids': [],
                'output_visibility_scope': 'project',
                'output_team_id': None,
                'eligible_source_count': 1,
                'max_sources': 200,
                'sources_truncated': False,
                'sources': [source],
                'input_digest': frozen_input_digest([source]),
            },
        )
    else:
        schedule_key = 'weekly:2026-W28'
        memory_id = uuid.uuid4()
        memory_version_id = uuid.uuid4()
        change = {
            'bucket': 'added',
            'memory_id': str(memory_id),
            'memory_version_id': str(memory_version_id),
            'version': 1,
            'content_hash': 'legacy-change-hash',
            'server_body_digest': frozen_input_digest([str(memory_version_id), 1, 'Frozen weekly body']),
            'visibility_scope': 'project',
            'team_id': None,
            'source_title': 'Frozen weekly title',
            'transition_ref': f'transition:{memory_id}',
            'occurred_at': '2026-07-09T12:00:00Z',
        }
        data = CreateWorkflowWorkInput(
            organization_id=session.organization_id,
            project_id=session.project_id,
            work_type=work_type,
            subject_type=WorkflowSubjectType.PROJECT,
            subject_id=session.project_id,
            occurrence_key=schedule_key,
            input_snapshot={
                'schema': 'weekly_digest_input/v1',
                'project_id': str(session.project_id),
                'team_id': None,
                'schedule_key': schedule_key,
                'window_start': '2026-07-06T00:00:00Z',
                'window_end': '2026-07-13T00:00:00Z',
                'visibility_policy': 'digest_visibility/v1',
                'allowed_team_ids': [],
                'output_visibility_scope': 'project',
                'output_team_id': None,
                'changes': [change],
                'input_digest': frozen_input_digest([change]),
            },
        )

    with transaction.atomic():
        work, created = create_work(data)

    assert created is True

    return work


def create_linked_run(
    work: WorkflowWork,
    *,
    status: str = WorkflowRunStatus.FAILED,
    created_at: object = None,
    finished_at: object = None,
    failure_reason: str = '',
) -> WorkflowRun:
    run = WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=status,
        input_snapshot=work.input_snapshot,
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
def test_retry_failed_distillations_signals_versioned_work_retry(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    create_linked_run(work, finished_at=timezone.now() - timedelta(minutes=40))

    result = retry_failed_distillations()

    assert result == {'retried': 1, 'unlinked': 0}

    queued = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED)
    assert queued.count() == 1
    new_run = queued.get()

    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session_work_v1').count() == 1
    outbox = CeleryOutbox.objects.get(task_name='engram.memory.distill_session_work_v1')
    assert outbox.args == [str(work.id), str(new_run.id)]
    assert outbox.task_id == f'workflow-work:{work.id}:run:{new_run.id}'
    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session').count() == 0


@pytest.mark.django_db
def test_retry_failed_distillations_is_a_no_op_when_nothing_is_eligible() -> None:
    result = retry_failed_distillations()

    assert result == {'retried': 0, 'unlinked': 0}
    assert WorkflowRun.objects.filter(status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0


def test_distill_session_uses_a_unique_request_id_but_stable_correlation_id_per_attempt() -> None:
    session_id = uuid.uuid4()

    def _run(**kwargs: object) -> object:
        result = mock.Mock()
        result.session.id = session_id

        return result

    with mock.patch(
        'engram.memory.tasks.run_session_distillation_with_tracking',
        side_effect=_run,
    ) as m_run:
        distill_session(str(session_id))
        distill_session(str(session_id))

    first_kwargs = m_run.call_args_list[0].kwargs
    second_kwargs = m_run.call_args_list[1].kwargs

    correlation_id = f'distill-session:{session_id}'

    assert first_kwargs['correlation_id'] == correlation_id
    assert second_kwargs['correlation_id'] == correlation_id
    assert first_kwargs['request_id'] != second_kwargs['request_id']
    assert first_kwargs['request_id'].startswith(f'{correlation_id}:')
    assert second_kwargs['request_id'].startswith(f'{correlation_id}:')


def test_distill_session_passes_existing_run_id_when_workflow_run_id_given() -> None:
    session_id = uuid.uuid4()
    workflow_run_id = uuid.uuid4()

    def _run(**kwargs: object) -> object:
        result = mock.Mock()
        result.session.id = session_id

        return result

    with mock.patch(
        'engram.memory.tasks.run_session_distillation_with_tracking',
        side_effect=_run,
    ) as m_run:
        distill_session(str(session_id), workflow_run_id=str(workflow_run_id))

    assert m_run.call_args.kwargs['existing_run_id'] == workflow_run_id


def test_distill_session_passes_none_existing_run_id_when_no_workflow_run_id_given() -> None:
    session_id = uuid.uuid4()

    def _run(**kwargs: object) -> object:
        result = mock.Mock()
        result.session.id = session_id

        return result

    with mock.patch(
        'engram.memory.tasks.run_session_distillation_with_tracking',
        side_effect=_run,
    ) as m_run:
        distill_session(str(session_id))

    assert m_run.call_args.kwargs['existing_run_id'] is None


@pytest.mark.django_db
def test_generate_weekly_digest_passes_existing_run_id_when_workflow_run_id_given(
    f_org: Organization,
    f_project: Project,
) -> None:
    workflow_run_id = uuid.uuid4()
    m_result = mock.Mock()

    with mock.patch(
        'engram.memory.tasks.run_weekly_digest_with_tracking',
        return_value=m_result,
    ) as m_run:
        generate_weekly_digest(str(f_org.id), str(f_project.id), workflow_run_id=str(workflow_run_id))

    assert m_run.call_args.kwargs['existing_run_id'] == workflow_run_id


def test_decay_memory_confidence_invokes_the_service() -> None:
    m_result = DecayMemoryConfidenceResult(organizations=2, projects=3, memories=5)

    with mock.patch('engram.memory.tasks.DecayMemoryConfidence.execute', return_value=m_result) as m_execute:
        result = decay_memory_confidence()

    m_execute.assert_called_once_with()
    assert result == {'organizations': 2, 'projects': 3, 'memories': 5}


def test_task_routes_send_expire_stale_candidates_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.expire_stale_candidates']['queue'] == celeryconfig.QUEUE_BATCH


def test_beat_schedule_registers_expire_stale_candidates() -> None:
    assert 'expire-stale-candidates' in celeryconfig.beat_schedule

    entry = celeryconfig.beat_schedule['expire-stale-candidates']

    assert entry['task'] == 'engram.memory.expire_stale_candidates'
    assert entry['schedule'] == timedelta(minutes=30)


def test_expire_stale_candidates_invokes_the_service() -> None:
    m_result = ExpireStaleCandidatesResult(scanned=7, rejected=4)

    with mock.patch('engram.memory.tasks.ExpireStaleCandidates.execute', return_value=m_result) as m_execute:
        result = expire_stale_candidates()

    m_execute.assert_called_once_with()
    assert result == {'scanned': 7, 'rejected': 4}


@pytest.mark.parametrize(
    'workflow_run_id',
    [None, uuid.UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')],
)
def test_dispatch_work_task_uses_deterministic_automatic_and_explicit_task_ids(
    workflow_run_id: uuid.UUID | None,
) -> None:
    work_id = uuid.UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    task = mock.Mock()
    expected_args = (str(work_id), str(workflow_run_id)) if workflow_run_id is not None else (str(work_id),)
    expected_task_id = (
        f'workflow-work:{work_id}:run:{workflow_run_id}' if workflow_run_id is not None else f'workflow-work:{work_id}'
    )

    tasks_module.dispatch_work_task(
        task,
        work_id,
        workflow_run_id=workflow_run_id,
    )

    task.apply_async.assert_called_once_with(
        args=expected_args,
        task_id=expected_task_id,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type', 'domain_target'),
    _VERSIONED_WORK_CASES,
)
def test_versioned_work_tasks_reject_malformed_ids_before_domain_access(
    task_attribute: str,
    work_type: str,
    domain_target: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    task = getattr(tasks_module, task_attribute)

    with mock.patch(domain_target) as m_execute:
        with pytest.raises(MemoryWorkerError, match='malformed work id'):
            task('not-a-uuid')
        with pytest.raises(MemoryWorkerError, match='malformed workflow run id'):
            task(str(work.id), workflow_run_id='not-a-uuid')

    m_execute.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'wrong_work_type', 'domain_target'),
    [
        (
            'process_observation_work_v1',
            WorkflowWorkType.SESSION_DISTILLATION,
            'engram.memory.tasks.ProcessObservationRecorded.execute',
        ),
        (
            'distill_session_work_v1',
            WorkflowWorkType.OBSERVATION_PROCESSING,
            'engram.memory.tasks.run_session_distillation_with_tracking',
        ),
        (
            'generate_daily_digest_work_v1',
            WorkflowWorkType.WEEKLY_DIGEST,
            'engram.memory.tasks.run_daily_digest_with_tracking',
        ),
        (
            'generate_weekly_digest_work_v1',
            WorkflowWorkType.DAILY_DIGEST,
            'engram.memory.tasks.run_weekly_digest_with_tracking',
        ),
    ],
)
def test_versioned_work_tasks_reject_mismatched_work_type_before_domain_access(
    task_attribute: str,
    wrong_work_type: str,
    domain_target: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=wrong_work_type)
    task = getattr(tasks_module, task_attribute)

    with mock.patch(domain_target) as m_domain:
        with pytest.raises(MemoryWorkerError, match='work type'):
            task(str(work.id))

    m_domain.assert_not_called()


@pytest.mark.django_db
def test_observation_work_task_rejects_subject_outside_persisted_work_scope_before_domain_access(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.OBSERVATION_PROCESSING)
    foreign_org = Organization.objects.create(name='Foreign Tasks Org', slug='foreign-tasks-org')
    foreign_project = Project.objects.create(
        organization=foreign_org,
        name='Foreign Backend',
        slug='foreign-backend',
    )
    WorkflowWork.objects.filter(id=work.id).update(project_id=foreign_project.id)
    task = tasks_module.process_observation_work_v1

    with mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute') as m_execute:
        with pytest.raises(MemoryWorkerError, match='scope'):
            task(str(work.id))

    m_execute.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type', 'domain_target'),
    _VERSIONED_WORK_CASES,
)
def test_versioned_work_tasks_reject_invalid_run_link_or_state_before_domain_access(
    task_attribute: str,
    work_type: str,
    domain_target: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    other_work = create_required_work(session, work_type=WorkflowWorkType.OBSERVATION_PROCESSING)
    foreign_org = Organization.objects.create(name='Foreign Run Org', slug='foreign-run-org')
    foreign_team = Team.objects.create(organization=foreign_org, name='Foreign Run Team', slug='foreign-run-team')
    foreign_project = Project.objects.create(
        organization=foreign_org,
        name='Foreign Run Project',
        slug='foreign-run-project',
    )
    wrong_run_type = (
        WorkflowRunType.OBSERVATION_PROCESSING
        if work_type != WorkflowWorkType.OBSERVATION_PROCESSING
        else WorkflowRunType.SESSION_DISTILLATION
    )
    invalid_updates = [
        {'work_id': other_work.id},
        {'status': WorkflowRunStatus.RUNNING},
        {'status': WorkflowRunStatus.FAILED},
        {'run_type': wrong_run_type},
        {
            'organization_id': foreign_org.id,
            'project_id': foreign_project.id,
            'team_id': foreign_team.id,
        },
    ]
    if task_attribute != 'process_observation_work_v1':
        invalid_updates.append({'status': WorkflowRunStatus.SUCCEEDED})
    task = getattr(tasks_module, task_attribute)

    with mock.patch(domain_target) as m_execute:
        for invalid_update in invalid_updates:
            invalid_run = WorkflowRun.objects.create(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                work=work,
                run_type=work_type,
                status=WorkflowRunStatus.QUEUED,
            )
            WorkflowRun.objects.filter(id=invalid_run.id).update(**invalid_update)
            with pytest.raises(MemoryWorkerError, match='workflow run'):
                task(str(work.id), workflow_run_id=str(invalid_run.id))
            invalid_run.delete()

    m_execute.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type', 'domain_target'),
    [
        (
            'generate_daily_digest_work_v1',
            WorkflowWorkType.DAILY_DIGEST,
            'engram.memory.tasks.run_daily_digest_with_tracking',
        ),
        (
            'generate_weekly_digest_work_v1',
            WorkflowWorkType.WEEKLY_DIGEST,
            'engram.memory.tasks.run_weekly_digest_with_tracking',
        ),
    ],
)
def test_unfinished_versioned_work_adapters_fail_closed_without_legacy_domain_execution(
    task_attribute: str,
    work_type: str,
    domain_target: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    task = getattr(tasks_module, task_attribute)

    with mock.patch(domain_target) as m_domain:
        with pytest.raises(MemoryWorkerError, match='not implemented'):
            task(str(work.id))

    m_domain.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type', 'domain_target'),
    [
        (
            'distill_session_work_v1',
            WorkflowWorkType.SESSION_DISTILLATION,
            'engram.memory.tasks.run_session_distillation_with_tracking',
        ),
        (
            'generate_daily_digest_work_v1',
            WorkflowWorkType.DAILY_DIGEST,
            'engram.memory.tasks.run_daily_digest_with_tracking',
        ),
        (
            'generate_weekly_digest_work_v1',
            WorkflowWorkType.WEEKLY_DIGEST,
            'engram.memory.tasks.run_weekly_digest_with_tracking',
        ),
    ],
)
def test_terminal_automatic_delivery_returns_before_unfinished_adapter(
    task_attribute: str,
    work_type: str,
    domain_target: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    resolve_work_succeeded(
        work.id,
        organization_id=work.organization_id,
        project_id=work.project_id,
    )
    task = getattr(tasks_module, task_attribute)

    with mock.patch(domain_target) as m_domain:
        task(str(work.id))

    m_domain.assert_not_called()


@pytest.mark.django_db
def test_distill_session_work_no_op_returns_without_creating_run_or_calling_provider(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    resolve_work_no_input(work.id, organization_id=work.organization_id, project_id=work.project_id)

    with mock.patch('engram.memory.tasks.run_session_distillation_with_tracking') as m_run:
        result = tasks_module.distill_session_work_v1(str(work.id))

    m_run.assert_not_called()
    assert result == str(work.id)
    assert WorkflowRun.objects.filter(work=work).count() == 0


@pytest.mark.django_db
def test_distill_session_work_automatic_delivery_does_not_retry_failed_initial_attempt(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    failed_run = WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=WorkflowRunType.SESSION_DISTILLATION,
        status=WorkflowRunStatus.QUEUED,
        input_snapshot={'session_id': str(session.id)},
    )
    WorkflowRun.objects.filter(id=failed_run.id).update(
        status=WorkflowRunStatus.FAILED,
        finished_at=timezone.now(),
    )

    with mock.patch('engram.memory.tasks.run_session_distillation_with_tracking') as m_run:
        tasks_module.distill_session_work_v1(str(work.id))

    m_run.assert_not_called()
    assert WorkflowRun.objects.filter(work=work).count() == 1
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_distill_session_work_rejects_altered_snapshot_before_run_or_provider(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    WorkflowWork.objects.filter(id=work.id).update(
        input_snapshot={
            'schema': 'session_distillation_input/v1',
            'session_id': str(session.id),
            'lower_sequence_exclusive': 0,
            'upper_sequence_inclusive': 999,
        },
    )

    with mock.patch('engram.memory.tasks.run_session_distillation_with_tracking') as m_run:
        with pytest.raises(MemoryWorkerError, match='fingerprint'):
            tasks_module.distill_session_work_v1(str(work.id))

    m_run.assert_not_called()
    assert WorkflowRun.objects.filter(work=work).count() == 0
