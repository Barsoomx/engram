from __future__ import annotations

import hashlib
import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.db import transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram import celeryconfig
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryVersion,
    Observation,
    Organization,
    Project,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    Team,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory import c53_orchestrator_test_support as orch
from engram.memory import tasks as tasks_module
from engram.memory.candidate_ttl import ExpireStaleCandidatesResult
from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWorkResult
from engram.memory.confidence_decay import DecayMemoryConfidenceResult
from engram.memory.services import MemoryCandidateWorkerResult, MemoryWorkerError, ProcessObservationRecorded
from engram.memory.tasks import (
    decay_memory_confidence,
    distill_session,
    expire_stale_candidates,
    generate_daily_digest,
    generate_weekly_digest,
    process_observation_recorded,
    process_observation_work_v1,
    reconcile_candidate_decision_work,
    retry_failed_distillations,
)
from engram.memory.work_execution import (
    StaleWorkFenceError,
    claim_work,
    execution_configuration_fingerprint,
    fail_work_claim,
    finish_work_claim,
    lock_work_fence,
)
from engram.memory.work_failures import (
    CONFIGURATION,
    INVALID_INPUT,
    PROVIDER_TRANSIENT,
    WORKER_LOST,
    ClassifiedWorkFailure,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
    resolve_work_no_input,
    resolve_work_succeeded,
)
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope

_OBS_LEASE = timedelta(seconds=120)
_OWNER_RE = re.compile(r'^[^:]+:[0-9]+:[0-9a-f-]{36}$')

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
        'process_candidate_decision_work_v1',
        'engram.memory.process_candidate_decision_work_v1',
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
        'engram.memory.tasks.run_complete_distillation_attempt',
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


def test_candidate_decision_worker_uses_explicit_time_limits_within_its_lease() -> None:
    task = tasks_module.process_candidate_decision_work_v1

    assert task.soft_time_limit == tasks_module._CANDIDATE_DECISION_SOFT_TIME_LIMIT == 240
    assert task.time_limit == tasks_module._CANDIDATE_DECISION_TIME_LIMIT == 270
    assert tasks_module._CANDIDATE_DECISION_LEASE == timedelta(seconds=300)


def test_task_routes_send_retry_failed_distillations_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.retry_failed_distillations']['queue'] == celeryconfig.QUEUE_BATCH


def test_embedding_projection_worker_is_registered_on_batch_queue() -> None:
    task = tasks_module.embed_memory_projection_work_v1
    task_name = 'engram.memory.embed_memory_projection_work_v1'

    assert task.name == task_name
    assert task.acks_late is True
    assert task.reject_on_worker_lost is True
    assert celeryconfig.task_routes[task_name]['queue'] == celeryconfig.QUEUE_BATCH


def test_embedding_projection_worker_uses_explicit_embedding_time_limits() -> None:
    task = tasks_module.embed_memory_projection_work_v1

    assert task.soft_time_limit == tasks_module._EMBEDDING_SOFT_TIME_LIMIT == 180
    assert task.time_limit == tasks_module._EMBEDDING_TIME_LIMIT == 210


def test_embedding_work_uses_embedding_policy_task_type_for_configuration_scope() -> None:
    from engram.memory import work_execution

    embedding_type = getattr(WorkflowWorkType, 'MEMORY_EMBEDDING', 'memory_embedding')

    assert work_execution._TASK_TYPE_BY_WORK[embedding_type] == 'embedding'


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

    assert result == {'retried': 1, 'reconciled': 0, 'unlinked': 0}

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

    assert result == {'retried': 0, 'reconciled': 0, 'unlinked': 0}
    assert WorkflowRun.objects.filter(status=WorkflowRunStatus.QUEUED).count() == 0
    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_retry_failed_distillations_does_not_invoke_candidate_reconciliation() -> None:
    with (
        mock.patch('engram.memory.tasks.reconcile_scheduled_session_work', return_value=2),
    ):
        result = retry_failed_distillations()

    assert not hasattr(tasks_module, 'reconcile_scheduled_candidate_work')
    assert result == {
        'retried': 0,
        'reconciled': 2,
        'unlinked': 0,
    }


@pytest.mark.django_db
def test_legacy_distill_session_delivery_bridges_to_exact_v1_work(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    AgentSession.objects.filter(id=session.id).update(end_work_contract_version=1)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)

    result = distill_session(str(session.id), workflow_run_id=str(uuid.uuid4()))

    assert not hasattr(tasks_module, 'run_session_distillation_with_tracking')
    assert result == str(work.id)
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.origin == WorkflowRunOrigin.RECONCILIATION
    outbox = CeleryOutbox.objects.get(task_name='engram.memory.distill_session_work_v1')
    assert outbox.args == [str(work.id), str(run.id)]


@pytest.mark.django_db
def test_legacy_distill_session_delivery_fails_closed_without_v1_work(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    AgentSession.objects.filter(id=session.id).update(end_work_contract_version=1)

    with pytest.raises(MemoryWorkerError, match='no versioned session work') as captured:
        distill_session(str(session.id))

    assert captured.value.retryable is False
    assert captured.value.code == 'legacy_distillation_work_missing'


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


def test_task_routes_send_candidate_reconciliation_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.reconcile_candidate_decision_work']['queue'] == (
        celeryconfig.QUEUE_BATCH
    )


def test_beat_schedule_registers_reconcile_candidate_decision_work() -> None:
    assert 'reconcile-candidate-decision-work' in celeryconfig.beat_schedule
    assert 'expire-stale-candidates' not in celeryconfig.beat_schedule

    entry = celeryconfig.beat_schedule['reconcile-candidate-decision-work']

    assert entry['task'] == 'engram.memory.reconcile_candidate_decision_work'
    assert entry['schedule'] == timedelta(minutes=30)


def test_expire_stale_candidates_invokes_the_service() -> None:
    m_result = ExpireStaleCandidatesResult(7, 0)

    with mock.patch('engram.memory.tasks.ExpireStaleCandidates.execute', return_value=m_result) as m_execute:
        result = expire_stale_candidates()

    m_execute.assert_called_once_with()
    assert result == {'scanned': 7, 'rejected': 0}


def test_expire_stale_candidates_result_preserves_legacy_shape() -> None:
    result = ExpireStaleCandidatesResult(7, 3)

    assert (result.scanned, result.rejected) == (7, 3)
    assert not hasattr(result, 'queued')


def test_reconcile_candidate_decision_work_invokes_the_service() -> None:
    m_result = ReconcileCandidateDecisionWorkResult(scanned=7, queued=4)

    with mock.patch('engram.memory.tasks.ReconcileCandidateDecisionWork.execute', return_value=m_result) as m_execute:
        result = reconcile_candidate_decision_work()

    m_execute.assert_called_once()
    assert result == {'scanned': 7, 'queued': 4}


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
            'engram.memory.tasks.run_complete_distillation_attempt',
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
    if task_attribute not in ('process_observation_work_v1', 'distill_session_work_v1'):
        invalid_updates.append({'status': WorkflowRunStatus.SUCCEEDED})
    task = getattr(tasks_module, task_attribute)

    # CONVERTED: the supplied-run link/state guard moves from _load_workflow_run/_claim_workflow_run
    # into claim_work, which only leases a QUEUED v1 attempt linked to this exact work. These rows are
    # execution_contract_version 0 (legacy), so the registry rejects every one of them as 'not a v1
    # attempt' before any domain execution; the rejection surfaces as ValueError from claim_work rather
    # than the old MemoryWorkerError. Domain execution must still never be reached.
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
            with pytest.raises((MemoryWorkerError, ValueError)):
                task(str(work.id), workflow_run_id=str(invalid_run.id))
            invalid_run.delete()

    m_execute.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type'),
    [
        ('generate_daily_digest_work_v1', WorkflowWorkType.DAILY_DIGEST),
        ('generate_weekly_digest_work_v1', WorkflowWorkType.WEEKLY_DIGEST),
    ],
)
def test_digest_work_adapter_rejects_altered_snapshot_before_provider(
    task_attribute: str,
    work_type: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    tampered = dict(work.input_snapshot)
    tampered['schedule_key'] = 'tampered-occurrence'
    WorkflowWork.objects.filter(id=work.id).update(input_snapshot=tampered)
    task = getattr(tasks_module, task_attribute)

    with mock.patch('engram.memory.services.get_provider_gateway') as m_gateway:
        result = task(str(work.id))

    m_gateway.assert_not_called()
    assert result == str(work.id)
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_fingerprint_mismatch'
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert work.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('task_attribute', 'work_type', 'domain_target'),
    [
        (
            'distill_session_work_v1',
            WorkflowWorkType.SESSION_DISTILLATION,
            'engram.memory.tasks.run_complete_distillation_attempt',
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

    with mock.patch('engram.memory.tasks.run_complete_distillation_attempt') as m_run:
        result = tasks_module.distill_session_work_v1(str(work.id))

    m_run.assert_not_called()
    assert result == str(work.id)
    assert WorkflowRun.objects.filter(work=work).count() == 0


@pytest.mark.django_db
def test_distill_session_work_automatic_delivery_starts_v1_after_failed_legacy_attempt(
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

    with mock.patch('engram.memory.tasks.run_complete_distillation_attempt') as m_run:
        tasks_module.distill_session_work_v1(str(work.id))

    m_run.assert_called_once()
    assert WorkflowRun.objects.filter(work=work).count() == 2
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 1


# CONVERTED from test_distill_session_work_succeeded_explicit_run_is_absorbed_on_redelivery.
# Old idiom: an explicit already-SUCCEEDED (v0) run redelivered on settled work was absorbed by
# _succeeded_workflow_run_result, emitting 'workflow_run_duplicate_delivery_absorbed' via='claim'.
# That helper is superseded by the execution registry. Under the cutover a redelivered settled
# work is absorbed at the claim boundary (claim_work -> terminal) with NO domain execution and NO
# new run; the ad-hoc duplicate-delivery log path no longer runs.
@pytest.mark.django_db
def test_distill_session_work_v1_settled_work_is_absorbed_as_terminal_without_execution(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    resolve_work_succeeded(
        work.id,
        organization_id=work.organization_id,
        project_id=work.project_id,
    )
    WorkflowWork.objects.filter(id=work.id).update(execution_state=WorkflowWorkExecutionState.SETTLED)

    with mock.patch('engram.memory.tasks.run_complete_distillation_attempt') as m_execute:
        result = tasks_module.distill_session_work_v1(str(work.id))

    m_execute.assert_not_called()
    assert result == str(work.id)
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 0


@pytest.mark.django_db
def test_distill_session_work_v1_explicit_redelivery_of_succeeded_run_absorbs_as_terminal(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=WorkflowWorkType.SESSION_DISTILLATION)
    now = timezone.now()
    claimed = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner='redeliver:worker',
        now=now,
        lease_for=timedelta(seconds=720),
    )
    run_id = claimed.claim.workflow_run_id
    lock_work_fence(claim=claimed.claim, now=now)
    finish_work_claim(claim=claimed.claim, now=now, completion='product_succeeded')

    with mock.patch('engram.memory.tasks.run_complete_distillation_attempt') as m_execute:
        result = tasks_module.distill_session_work_v1(str(work.id), str(run_id))

    m_execute.assert_not_called()
    assert result == str(work.id)
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.RUNNING).count() == 0


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

    with mock.patch('engram.memory.tasks.run_complete_distillation_attempt') as m_execute:
        result = tasks_module.distill_session_work_v1(str(work.id))

    m_execute.assert_not_called()
    assert result == str(work.id)
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.failure_streak == 1
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'work_fingerprint_mismatch'


# ---------------------------------------------------------------------------
# C2.1 Zone-C task-level execution-registry cutover (RED)
#
# These specify the NEW registry-backed behavior of the versioned adapters: each
# automatic delivery must go through claim_work (fenced lease + append-only v1
# WorkflowRun), execute the domain, then commit the durable semantic outcome in
# the same short transaction as lock_work_fence + finish_work_claim/fail_work_claim.
# The old idioms (no run on automatic observation delivery, resolve_work_* without
# touching execution_state, self.retry on retryable failures) fail these tests.
# ---------------------------------------------------------------------------


def _observation_work(session: AgentSession) -> WorkflowWork:
    return create_required_work(session, work_type=WorkflowWorkType.OBSERVATION_PROCESSING)


def _no_signal_result() -> MemoryCandidateWorkerResult:
    return MemoryCandidateWorkerResult(candidate=None, duplicate=False, memory=None)


@pytest.mark.django_db
def test_observation_work_v1_automatic_delivery_leases_and_settles_with_run_evidence(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)

    with mock.patch(
        'engram.memory.tasks.ProcessObservationRecorded.execute',
        return_value=_no_signal_result(),
    ):
        process_observation_work_v1(str(work.id))

    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.origin == WorkflowRunOrigin.AUTOMATIC
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.fencing_token == 1
    assert run.started_at is not None
    assert run.finished_at is not None
    assert _OWNER_RE.match(run.lease_owner) is not None

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason in (
        WorkflowWorkResolutionReason.SUCCEEDED,
        WorkflowWorkResolutionReason.NO_SIGNAL,
    )
    assert work.fencing_token == 1
    assert work.lease_owner == ''
    assert work.lease_expires_at is None
    assert work.heartbeat_at is None
    assert work.next_retry_at is None


@pytest.mark.django_db
def test_observation_work_required_ready_work_with_no_run_stays_signal_eligible_f7(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)

    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state == WorkflowWorkExecutionState.READY
    assert work.fencing_token == 0
    assert work.lease_owner == ''
    assert work.lease_expires_at is None
    assert work.heartbeat_at is None
    assert work.next_retry_at is None
    assert WorkflowRun.objects.filter(work=work).count() == 0

    result = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='host:1:00000000-0000-4000-8000-000000000000',
        now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        lease_for=_OBS_LEASE,
    )

    assert result.outcome == 'claimed'


@pytest.mark.django_db
def test_observation_work_v1_expired_lease_is_reclaimed_and_stale_owner_is_fenced_f8(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    past = timezone.now() - timedelta(hours=1)
    stale = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='ghost:1:11111111-1111-4111-8111-111111111111',
        now=past,
        lease_for=_OBS_LEASE,
    )
    stale_claim = stale.claim
    old_run_id = stale_claim.workflow_run_id

    with mock.patch(
        'engram.memory.tasks.ProcessObservationRecorded.execute',
        return_value=_no_signal_result(),
    ):
        process_observation_work_v1(str(work.id))

    old_run = WorkflowRun.objects.get(id=old_run_id)
    assert old_run.status == WorkflowRunStatus.FAILED
    assert old_run.failure_class == WORKER_LOST
    assert old_run.failure_code == 'lease_expired'

    fresh = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.SUCCEEDED).get()
    assert fresh.fencing_token == 2
    assert fresh.origin == WorkflowRunOrigin.AUTOMATIC
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.fencing_token == 2

    with pytest.raises(StaleWorkFenceError):
        with transaction.atomic():
            lock_work_fence(claim=stale_claim, now=timezone.now())


@pytest.mark.django_db
def test_observation_work_v1_busy_foreign_live_lease_returns_without_execution(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    foreign = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='rival:1:22222222-2222-4222-8222-222222222222',
        now=timezone.now(),
        lease_for=_OBS_LEASE,
    )
    foreign_run_id = foreign.claim.workflow_run_id

    with mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute') as m_execute:
        process_observation_work_v1(str(work.id))

    m_execute.assert_not_called()
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.LEASED
    assert work.lease_owner == foreign.claim.lease_owner
    assert work.fencing_token == foreign.claim.fencing_token
    foreign_run = WorkflowRun.objects.get(id=foreign_run_id)
    assert foreign_run.status == WorkflowRunStatus.RUNNING
    assert WorkflowRun.objects.filter(work=work).count() == 1


@pytest.mark.django_db
def test_observation_work_v1_provider_transient_records_retry_wait_without_self_retry(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    error = ModelPolicyError('provider_http_error', 'rate limited', retryable=True, http_status=429)
    m_retry = mock.Mock(side_effect=AssertionError('self.retry must not be scheduled for domain failures'))

    with (
        mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute', side_effect=error),
        mock.patch.object(process_observation_work_v1, 'retry', m_retry),
    ):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            process_observation_work_v1(str(work.id))

    m_retry.assert_not_called()
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == PROVIDER_TRANSIENT
    assert run.failure_code == 'provider_rate_limited'

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.failure_streak == 1
    assert work.next_retry_at is not None
    delay = (work.next_retry_at - run.finished_at).total_seconds()
    assert 29 <= delay <= 31


@pytest.mark.django_db
def test_observation_work_v1_configuration_failure_blocks_with_fingerprint(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    error = ModelPolicyError('model_policy_not_found', 'no policy', http_status=404)
    expected_fingerprint = execution_configuration_fingerprint(work)
    m_retry = mock.Mock(side_effect=AssertionError('configuration failure must not self.retry'))

    with (
        mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute', side_effect=error),
        mock.patch.object(process_observation_work_v1, 'retry', m_retry),
    ):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            process_observation_work_v1(str(work.id))

    m_retry.assert_not_called()
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == CONFIGURATION
    assert run.failure_code == 'model_policy_unavailable'
    assert run.configuration_fingerprint == expected_fingerprint

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.blocked_configuration_fingerprint == expected_fingerprint
    assert work.next_retry_at is None


@pytest.mark.django_db
def test_observation_work_v1_invalid_input_is_terminal_and_keeps_work_required(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    error = ModelPolicyError('provider_http_error', 'bad request', http_status=400)
    m_retry = mock.Mock(side_effect=AssertionError('invalid input must not self.retry'))

    with (
        mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute', side_effect=error),
        mock.patch.object(process_observation_work_v1, 'retry', m_retry),
    ):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            process_observation_work_v1(str(work.id))

    m_retry.assert_not_called()
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == INVALID_INPUT
    assert run.failure_code == 'provider_request_invalid'

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.next_retry_at is None


@pytest.mark.django_db
def test_observation_work_v1_stores_claim_time_configuration_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    error = ModelPolicyError('model_policy_not_found', 'no policy', http_status=404)
    phase = {'value': 'claim'}
    monkeypatch.setattr(
        'engram.memory.tasks.execution_configuration_fingerprint',
        lambda _work: ('a' * 64) if phase['value'] == 'claim' else ('b' * 64),
    )

    def _fail(_input: object) -> object:
        phase['value'] = 'failure'

        raise error

    with mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute', side_effect=_fail):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            process_observation_work_v1(str(work.id))

    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.configuration_fingerprint == 'a' * 64
    work.refresh_from_db()
    assert work.blocked_configuration_fingerprint == 'a' * 64


@pytest.mark.django_db
def test_observation_work_v1_blocked_redelivery_skips_provider_until_config_changes(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    config_error = ModelPolicyError('model_policy_not_found', 'no policy', http_status=404)

    with mock.patch(
        'engram.memory.tasks.ProcessObservationRecorded.execute',
        side_effect=config_error,
    ):
        with pytest.raises((ModelPolicyError, MemoryWorkerError)):
            process_observation_work_v1(str(work.id))

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    blocked_fingerprint = work.blocked_configuration_fingerprint

    with mock.patch('engram.memory.tasks.ProcessObservationRecorded.execute') as m_execute:
        process_observation_work_v1(str(work.id))

    m_execute.assert_not_called()
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert work.blocked_configuration_fingerprint == blocked_fingerprint

    ModelPolicy.objects.create(
        organization=f_org,
        team=f_team,
        project=f_project,
        name='generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=_provider_secret(f_org, f_team),
        version=1,
    )
    assert execution_configuration_fingerprint(work) != blocked_fingerprint

    with mock.patch(
        'engram.memory.tasks.ProcessObservationRecorded.execute',
        return_value=_no_signal_result(),
    ) as m_execute:
        process_observation_work_v1(str(work.id))

    m_execute.assert_called_once()
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE


def _provider_secret(organization: Organization, team: Team) -> ProviderSecret:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='resume secret',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )

    return secret


# ---------------------------------------------------------------------------
# C2.1 Zone-D digest adapter claim short-circuits (RED)
#
# The digest adapters must route automatic delivery through claim_work before any
# execute_frozen_digest_work call: a live foreign lease must return 'busy' and
# skip the domain entirely, never re-entering publication.
# ---------------------------------------------------------------------------

_DIGEST_LEASE = timedelta(seconds=240)

_DIGEST_BUSY_CASES = (
    ('generate_daily_digest_work_v1', WorkflowWorkType.DAILY_DIGEST),
    ('generate_weekly_digest_work_v1', WorkflowWorkType.WEEKLY_DIGEST),
)


@pytest.mark.django_db
@pytest.mark.parametrize(('task_attribute', 'work_type'), _DIGEST_BUSY_CASES)
def test_digest_work_v1_busy_foreign_live_lease_returns_without_execution(
    task_attribute: str,
    work_type: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = create_required_work(session, work_type=work_type)
    foreign = claim_work(
        work_id=work.id,
        expected_work_type=work_type,
        lease_owner='rival:1:22222222-2222-4222-8222-222222222222',
        now=timezone.now(),
        lease_for=_DIGEST_LEASE,
    )
    foreign_run_id = foreign.claim.workflow_run_id
    task = getattr(tasks_module, task_attribute)

    with mock.patch('engram.memory.digest_work.execute_frozen_digest_work') as m_execute:
        task(str(work.id))

    m_execute.assert_not_called()
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.LEASED
    assert work.lease_owner == foreign.claim.lease_owner
    assert work.fencing_token == foreign.claim.fencing_token
    foreign_run = WorkflowRun.objects.get(id=foreign_run_id)
    assert foreign_run.status == WorkflowRunStatus.RUNNING
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(('task_attribute', 'work_type'), _DIGEST_BUSY_CASES)
def test_digest_work_v1_settled_work_is_absorbed_without_execution(
    task_attribute: str,
    work_type: str,
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
    WorkflowWork.objects.filter(id=work.id).update(execution_state=WorkflowWorkExecutionState.SETTLED)
    task = getattr(tasks_module, task_attribute)

    with mock.patch('engram.memory.digest_work.execute_frozen_digest_work') as m_execute:
        task(str(work.id))

    m_execute.assert_not_called()
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 0


def _end_v1_session_with_useful_observation(suffix: str) -> tuple[Organization, Project, AgentSession, WorkflowWork]:
    from engram.memory.observation_work_tests import create_scope as create_session_scope
    from engram.memory.session_lifecycle import EndSession

    organization, project, session = create_session_scope(suffix)
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title='useful observation',
        content_hash=f'content-{session.id}-1',
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )
    result = EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    work = WorkflowWork.objects.get(id=result.work_id)

    return organization, project, session, work


@pytest.mark.django_db
def test_retry_failed_distillations_routes_required_work_through_reconciler() -> None:
    _organization, _project, session, work = _end_v1_session_with_useful_observation('beat-retire-required')
    past_grace = timezone.now() - timedelta(minutes=6)
    WorkflowWork.objects.filter(id=work.id).update(created_at=past_grace)
    AgentSession.objects.filter(id=session.id).update(ended_at=past_grace)
    CeleryOutbox.objects.all().delete()

    retry_failed_distillations()

    queued = WorkflowRun.objects.filter(
        work_id=work.id,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    )
    assert queued.count() == 1
    assert queued.get().origin == WorkflowRunOrigin.RECONCILIATION
    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session_work_v1').count() == 1


@pytest.mark.django_db
def test_retry_failed_distillations_leaves_v1_retry_wait_work_to_reconciler() -> None:
    _organization, _project, session, work = _end_v1_session_with_useful_observation('beat-retire-retrywait')

    failed_at = timezone.now() - timedelta(minutes=40)
    claimed = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner='host:beat:worker',
        now=failed_at,
        lease_for=timedelta(seconds=720),
    )
    fail_work_claim(
        claim=claimed.claim,
        now=failed_at,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )
    AgentSession.objects.filter(id=session.id).update(ended_at=failed_at)
    CeleryOutbox.objects.all().delete()

    result = retry_failed_distillations()

    queued = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED)
    assert queued.count() == 1
    assert queued.get().execution_contract_version == 1
    assert WorkflowRun.objects.filter(work=work, execution_contract_version=0).count() == 0
    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session_work_v1').count() == 1
    assert result == {'retried': 0, 'reconciled': 1, 'unlinked': 0}


def _embedding_source(version: MemoryVersion) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_kind='memory_version',
        source_type='memory_version',
        kind='memory_version',
        source_content_hash=version.content_hash,
        content_hash=version.content_hash,
        candidate_source_id=None,
        source_memory_version_id=version.id,
        memory_version_id=version.id,
    )


def _activate_v1_embedding_chain(
    *,
    memory: Memory,
    version: MemoryVersion,
    document: RetrievalDocument,
    work: WorkflowWork,
    transition_id: uuid.UUID,
) -> None:
    transition_model = __import__('engram.core.models', fromlist=['MemoryTransition']).MemoryTransition
    audit = AuditEvent.objects.create(
        organization=memory.organization,
        project=memory.project,
        team=memory.team,
        event_type='MemoryTransitionCommitted',
        actor_type='test',
        target_type='memory',
        target_id=str(memory.id),
        capability='memories:write',
        result=AuditResult.RECORDED,
        metadata={'schema': 'memory_transition/v1', 'transition_id': str(transition_id)},
    )
    transition_model.objects.create(
        id=transition_id,
        organization=memory.organization,
        project=memory.project,
        team=memory.team,
        transition_type='publish_digest',
        idempotency_key=f'test:{transition_id}:publish:v1',
        request_fingerprint='6' * 64,
        memory=memory,
        from_version=version,
        to_version=version,
        result_memory=memory,
        result_version=version,
        exact_document=document,
        result_exact_document=document,
        embedding_work=work,
        audit_event=audit,
        provenance_hash='7' * 64,
    )
    Memory.objects.filter(id=memory.id).update(
        transition_contract_version=1,
        current_transition_id=transition_id,
    )


@pytest.mark.django_db(transaction=True)
def test_embedding_completion_recovers_after_failure_without_repromotion(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    from engram.memory import projections

    memory = Memory.objects.create(
        organization=f_org,
        project=f_project,
        team=f_team,
        title='Embedding recovery memory',
        body='Embedding recovery body',
    )
    version = MemoryVersion.objects.create(
        organization=f_org,
        project=f_project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='9' * 64,
    )
    transition_id = uuid.uuid4()
    with mock.patch('engram.memory.work_dispatch.app.send_task'):
        with transaction.atomic():
            document = projections.write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=[_embedding_source(version)],
            )
            work, created = projections.create_embedding_work_and_signal(document=document)
    assert created is True
    _activate_v1_embedding_chain(
        memory=memory,
        version=version,
        document=document,
        work=work,
        transition_id=transition_id,
    )

    claim_result = claim_work(
        work_id=work.id,
        expected_work_type='memory_embedding',
        lease_owner='embedding:test:00000000-0000-4000-8000-000000000000',
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert claim_result.claim is not None
    claim = claim_result.claim

    failed = ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout')
    fail_work_claim(claim=claim, now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC), failure=failed)
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert RetrievalDocument.objects.get(id=document.id).embedding_vector == []

    retry_claim_result = claim_work(
        work_id=work.id,
        expected_work_type='memory_embedding',
        lease_owner='embedding:test:00000000-0000-4000-8000-000000000001',
        now=datetime(2026, 7, 14, 12, 2, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert retry_claim_result.claim is not None
    embedding = [0.1, 0.2, *([0.0] * 1534)]
    result = projections.complete_embedding_projection(
        claim=retry_claim_result.claim,
        expected_projection_hash=document.exact_projection_hash,
        embedding=embedding,
        provider_call_id=uuid.uuid4(),
        now=datetime(2026, 7, 14, 12, 3, tzinfo=UTC),
    )

    assert result is not None
    document.refresh_from_db()
    assert document.embedding_vector == embedding
    assert document.embedding_projection_hash == document.exact_projection_hash
    assert document.embedding_projected_at is not None
    assert WorkflowWork.objects.filter(subject_id=document.id, work_type='memory_embedding').count() == 1


@pytest.mark.django_db(transaction=True)
def test_stale_embedding_completion_is_discarded_without_vector_or_repromotion(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    from engram.memory import projections

    memory = Memory.objects.create(
        organization=f_org,
        project=f_project,
        team=f_team,
        title='Fenced memory',
        body='Fenced body',
    )
    version = MemoryVersion.objects.create(
        organization=f_org,
        project=f_project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='8' * 64,
    )
    transition_id = uuid.uuid4()
    with mock.patch('engram.memory.work_dispatch.app.send_task'):
        with transaction.atomic():
            document = projections.write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=[_embedding_source(version)],
            )
            work, _created = projections.create_embedding_work_and_signal(document=document)
    _activate_v1_embedding_chain(
        memory=memory,
        version=version,
        document=document,
        work=work,
        transition_id=transition_id,
    )
    claim_result = claim_work(
        work_id=work.id,
        expected_work_type='memory_embedding',
        lease_owner='embedding:test:00000000-0000-4000-8000-000000000002',
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert claim_result.claim is not None
    stale_hash = 'a' * 64
    result = projections.complete_embedding_projection(
        claim=claim_result.claim,
        expected_projection_hash=stale_hash,
        embedding=[0.9, 0.8, *([0.0] * 1534)],
        provider_call_id=uuid.uuid4(),
        now=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
    )

    assert result is None
    document.refresh_from_db()
    assert document.embedding_vector == []
    assert document.embedding_projection_hash == ''
    work.refresh_from_db()
    assert work.resolution_reason == WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED
    assert WorkflowWork.objects.filter(subject_id=document.id, work_type='memory_embedding').count() == 1


@pytest.mark.django_db(transaction=True)
def test_embedding_worker_calls_provider_outside_transaction_and_records_policy(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    from engram.memory import projections

    memory = Memory.objects.create(
        organization=f_org,
        project=f_project,
        team=f_team,
        title='Worker embedding memory',
        body='Worker embedding body',
    )
    version = MemoryVersion.objects.create(
        organization=f_org,
        project=f_project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='d' * 64,
    )
    transition_id = uuid.uuid4()
    with mock.patch('engram.memory.work_dispatch.app.send_task'):
        with transaction.atomic():
            document = projections.write_exact_memory_projection(
                memory=memory,
                version=version,
                transition_id=transition_id,
                sources=[_embedding_source(version)],
            )
            work, _created = projections.create_embedding_work_and_signal(document=document)
    _activate_v1_embedding_chain(
        memory=memory,
        version=version,
        document=document,
        work=work,
        transition_id=transition_id,
    )
    policy = ModelPolicy.objects.create(
        organization=f_org,
        team=f_team,
        project=f_project,
        name='embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=_provider_secret(f_org, f_team),
        version=1,
    )
    embedding = [0.3, 0.4, *([0.0] * 1534)]

    class RecordingGateway:
        def embed(self, data: object) -> SimpleNamespace:
            assert transaction.get_connection().in_atomic_block is False
            assert data.policy.id == policy.id
            record = ProviderCallRecord.objects.create(
                organization=f_org,
                project=f_project,
                team=f_team,
                policy=policy,
                secret=policy.secret,
                provider=policy.provider,
                model=policy.model,
                task_type=policy.task_type,
                policy_version=policy.version,
                request_id=data.request_id,
                trace_id=data.trace_id,
                redaction_state='redacted',
                result=AuditResult.RECORDED,
                metadata={'schema': 'embedding_call/v1'},
            )
            return SimpleNamespace(embedding=embedding, call_record_id=record.id)

    with mock.patch('engram.model_policy.services.get_provider_gateway', return_value=RecordingGateway()):
        tasks_module.embed_memory_projection_work_v1(str(work.id))

    document.refresh_from_db()
    work.refresh_from_db()
    provider_call = ProviderCallRecord.objects.get()
    assert provider_call.policy_id == policy.id
    assert provider_call.task_type == 'embedding'
    assert document.embedding_vector == embedding
    assert document.embedding_projection_hash == document.exact_projection_hash
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert Memory.objects.filter(id=memory.id).count() == 1


# ---------------------------------------------------------------------------
# C5.3 - task handler cutover: process_candidate_decision_work_v1 runs the
# DecideMemoryCandidate orchestrator when the rollout is enabled, and only
# capability-blocks (rollout_not_enabled) when it is disabled. The observation
# path no longer curates synchronously.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_candidate_decision_handler_runs_orchestrator_when_rollout_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('cutover-on')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='cutover-on')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=orch.EMBEDDING_1536, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert len(orch.curation_decisions_for(candidate)) == 1
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_candidate_decision_handler_blocks_with_rollout_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('cutover-off')
    candidate, work, run = orch.subject_candidate(scope, suffix='cutover-off')
    orch.disable_rollout(monkeypatch)

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert orch.curation_decisions_for(candidate) == []
    work.refresh_from_db()
    run.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == CONFIGURATION
    assert run.failure_code == 'rollout_not_enabled'
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED


# ---------------------------------------------------------------------------
# C5 - default-on cutover: with ENGRAM_CANDIDATE_DECISION_ENABLED unset the
# handler autonomously runs the orchestrator; only the explicit off-switch
# (=0 and the usual falsy spellings) capability-blocks with rollout_not_enabled.
# These exercise the REAL env-reading candidate_decision_enabled (not patched).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_candidate_decision_handler_runs_orchestrator_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ENGRAM_CANDIDATE_DECISION_ENABLED', raising=False)
    scope = orch.orchestrator_scope('env-default-on')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='env-default-on')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_decision_services(
        monkeypatch, embedding=orch.EMBEDDING_1536, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert len(orch.curation_decisions_for(candidate)) == 1
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
@pytest.mark.parametrize('falsy', ['0', 'false', 'FALSE', 'no', 'off'])
def test_candidate_decision_handler_blocks_when_env_disabled(monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_DECISION_ENABLED', falsy)
    scope = orch.orchestrator_scope(f'env-off-{falsy.lower()}')
    candidate, work, run = orch.subject_candidate(scope, suffix=f'env-off-{falsy.lower()}')

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert orch.curation_decisions_for(candidate) == []
    work.refresh_from_db()
    run.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class == CONFIGURATION
    assert run.failure_code == 'rollout_not_enabled'
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED


def test_observation_processing_does_not_curate_synchronously() -> None:
    source = inspect.getsource(ProcessObservationRecorded.execute)

    assert 'CurateMemoryCandidate' not in source
    assert 'DecideMemoryCandidate' not in source


def _unreferenced_embedding_work(
    organization: Organization,
    team: Team,
    project: Project,
) -> tuple[RetrievalDocument, WorkflowWork, WorkflowRun]:
    from engram.memory.projections import create_embedding_work_and_signal

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Subject validation memory',
        body='Subject validation body',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='a' * 64,
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        full_text='Subject validation memory\n\nSubject validation body',
        projection_contract_version=1,
        exact_projection_hash='b' * 64,
    )
    with mock.patch('engram.memory.work_dispatch.app.send_task'):
        with transaction.atomic():
            work, created = create_embedding_work_and_signal(document=document)
    assert created is True
    queued_run = WorkflowRun.objects.get(
        work=work,
        execution_contract_version=1,
        status=WorkflowRunStatus.QUEUED,
    )

    return document, work, queued_run


def _settle_for_late_delivery(
    work: WorkflowWork,
    *,
    work_type: str,
    workflow_run_id: uuid.UUID | None = None,
) -> None:
    from engram.memory.work_execution import finish_work_claim

    now = timezone.now()
    claimed = claim_work(
        work_id=work.id,
        expected_work_type=work_type,
        lease_owner=f'subject-validation:{uuid.uuid4()}',
        now=now,
        lease_for=timedelta(minutes=5),
        workflow_run_id=workflow_run_id,
    )
    assert claimed.claim is not None
    finish_work_claim(
        claim=claimed.claim,
        now=now,
        completion='product_succeeded',
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('subject_failure', 'expected_code'),
    [
        ('missing', 'work_scope_invalid'),
        ('out_of_scope', 'work_scope_invalid'),
        ('frozen_mismatch', 'work_fingerprint_mismatch'),
    ],
)
def test_observation_subject_failure_records_typed_terminal_attempt(
    subject_failure: str,
    expected_code: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    observation = Observation.objects.get(id=work.subject_id)

    if subject_failure == 'missing':
        observation.delete()
    elif subject_failure == 'out_of_scope':
        other_project = Project.objects.create(
            organization=f_org,
            name='Other subject project',
            slug='other-subject-project',
        )
        Observation.objects.filter(id=observation.id).update(project_id=other_project.id)
    else:
        Observation.objects.filter(id=observation.id).update(body='drifted after work creation')

    with pytest.raises(MemoryWorkerError):
        process_observation_work_v1(str(work.id))

    work.refresh_from_db()
    failed = WorkflowRun.objects.filter(
        work=work,
        execution_contract_version=1,
        status=WorkflowRunStatus.FAILED,
        failure_class=INVALID_INPUT,
    ).first()
    assert (
        work.disposition,
        work.execution_state,
        failed.failure_code if failed is not None else None,
    ) == (
        WorkflowWorkDisposition.REQUIRED,
        WorkflowWorkExecutionState.TERMINAL_FAILURE,
        expected_code,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('subject_failure', 'expected_code'),
    [
        ('missing', 'work_scope_invalid'),
        ('out_of_scope', 'work_scope_invalid'),
        ('frozen_mismatch', 'work_fingerprint_mismatch'),
    ],
)
def test_embedding_subject_failure_records_typed_terminal_attempt(
    subject_failure: str,
    expected_code: str,
    f_org: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    document, work, queued_run = _unreferenced_embedding_work(f_org, f_team, f_project)

    if subject_failure == 'missing':
        document.delete()
    elif subject_failure == 'out_of_scope':
        other_project = Project.objects.create(
            organization=f_org,
            name='Other embedding project',
            slug='other-embedding-project',
        )
        RetrievalDocument.objects.filter(id=document.id).update(project_id=other_project.id)
    else:
        next_version = MemoryVersion.objects.create(
            organization=f_org,
            project=f_project,
            memory=document.memory,
            version=2,
            body='Drifted version',
            content_hash='c' * 64,
        )
        RetrievalDocument.objects.filter(id=document.id).update(memory_version_id=next_version.id)

    with pytest.raises(MemoryWorkerError):
        tasks_module.embed_memory_projection_work_v1(
            str(work.id),
            str(queued_run.id),
        )

    work.refresh_from_db()
    failed = WorkflowRun.objects.filter(
        work=work,
        execution_contract_version=1,
        status=WorkflowRunStatus.FAILED,
        failure_class=INVALID_INPUT,
    ).first()
    assert (
        work.disposition,
        work.execution_state,
        failed.failure_code if failed is not None else None,
    ) == (
        WorkflowWorkDisposition.REQUIRED,
        WorkflowWorkExecutionState.TERMINAL_FAILURE,
        expected_code,
    )


@pytest.mark.django_db
def test_late_settled_observation_delivery_is_absorbed_before_subject_lookup(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
    f_agent: Agent,
) -> None:
    session = create_session(f_org, f_team, f_project, f_agent)
    work = _observation_work(session)
    _settle_for_late_delivery(
        work,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
    )
    Observation.objects.get(id=work.subject_id).delete()
    run_count = WorkflowRun.objects.filter(work=work).count()

    assert process_observation_work_v1(str(work.id)) == str(work.id)

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert WorkflowRun.objects.filter(work=work).count() == run_count


@pytest.mark.django_db
def test_late_settled_embedding_delivery_is_absorbed_before_subject_lookup(
    f_org: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    document, work, queued_run = _unreferenced_embedding_work(f_org, f_team, f_project)
    _settle_for_late_delivery(
        work,
        work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        workflow_run_id=queued_run.id,
    )
    document.delete()
    run_count = WorkflowRun.objects.filter(work=work).count()

    assert tasks_module.embed_memory_projection_work_v1(str(work.id)) == str(work.id)

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert WorkflowRun.objects.filter(work=work).count() == run_count


@pytest.mark.django_db
def test_candidate_decision_off_switch_recovery_requeues_blocked_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork

    monkeypatch.setenv('ENGRAM_CANDIDATE_DECISION_ENABLED', 'off')
    scope = orch.orchestrator_scope('env-off-recovery')
    candidate, work, run = orch.subject_candidate(scope, suffix='env-off-recovery')

    _result, error = orch.run_decision(work, run)

    assert error is None
    work.refresh_from_db()
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state == WorkflowWorkExecutionState.BLOCKED
    blocked_fingerprint = work.blocked_configuration_fingerprint

    monkeypatch.delenv('ENGRAM_CANDIDATE_DECISION_ENABLED')
    enabled_fingerprint = execution_configuration_fingerprint(work)
    sent: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda task_name, *, args, **_kwargs: sent.append((task_name, tuple(args))),
    )

    result = ReconcileCandidateDecisionWork().execute(as_of=timezone.now())
    reconciliation_runs = WorkflowRun.objects.filter(
        work=work,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.RECONCILIATION,
    )

    assert (
        enabled_fingerprint != blocked_fingerprint,
        result.queued,
        reconciliation_runs.count(),
        len(sent),
    ) == (True, 1, 1, 1)


@pytest.mark.django_db
def test_candidate_configuration_fingerprint_tracks_primary_curation_policy() -> None:
    scope = orch.orchestrator_scope('candidate-primary-fingerprint')
    _candidate, work, _run = orch.subject_candidate(
        scope,
        suffix='candidate-primary-fingerprint',
    )
    before = execution_configuration_fingerprint(work)

    orch.curation_policy(scope)

    assert execution_configuration_fingerprint(work) != before
