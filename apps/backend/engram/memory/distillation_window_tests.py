from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Agent,
    AgentSession,
    DistillationChunk,
    DistillationStage,
    DistillationWindow,
    Observation,
    Organization,
    Project,
    Runtime,
    Team,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory import distillation_window as dw
from engram.memory.distillation_provider_stage import extract_reuse_key
from engram.memory.services import MemoryWorkerError
from engram.memory.session_lifecycle import EndSession
from engram.memory.work_execution import claim_work, finish_work_claim
from engram.memory.work_failures import INVALID_INPUT, translate_failure
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope

Scope = tuple[Organization, Team, Project, Agent, AgentSession]

_LEASE = timedelta(seconds=720)
_STALE = timedelta(minutes=6)
_HEX_A = 'a' * 64
_HEX_B = 'b' * 64
POSTGRES = connection.vendor == 'postgresql'
requires_postgres = pytest.mark.skipif(not POSTGRES, reason='concurrency evidence requires PostgreSQL row locks')

_WINDOW_MANIFEST_SCHEMA = 'distillation_window_manifest.v1'
_CHUNK_MANIFEST_SCHEMA = 'distillation_chunk_manifest.v1'


def _scope(suffix: str, *, project_slug: str | None = None) -> Scope:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    project = Project.objects.create(
        organization=organization,
        name=f'Project {suffix}',
        slug=project_slug or f'project-{suffix}',
    )
    agent = Agent.objects.create(organization=organization, runtime=Runtime.CODEX, external_id=f'agent-{suffix}')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=0,
    )

    return organization, team, project, agent, session


def _observation(
    scope: Scope,
    *,
    sequence: int,
    event_type: str = 'post_tool_use',
    body: str = '',
    session: AgentSession | None = None,
) -> Observation:
    organization, team, project, agent, default_session = scope
    target = session or default_session

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=target,
        observation_type='tool_use',
        title=f'observation {sequence}',
        body=body,
        content_hash=f'content-{target.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': event_type},
    )


def _session_work(scope: Scope, *, upper: int) -> WorkflowWork:
    organization, _team, project, _agent, session = scope
    with transaction.atomic():
        work, _created = create_work(
            CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=session.id,
                input_snapshot={
                    'schema': 'session_distillation_input/v1',
                    'session_id': str(session.id),
                    'lower_sequence_exclusive': 0,
                    'upper_sequence_inclusive': upper,
                },
            )
        )

    return work


def _curation_policy(scope: Scope) -> ModelPolicy:
    organization, team, project, _agent, _session = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
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

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Curation policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=2,
    )


def _provider_call(scope: Scope, policy: ModelPolicy) -> ProviderCallRecord:
    organization, team, project, _agent, _session = scope

    return ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'distill-stage:{uuid.uuid4()}',
        redaction_state='redacted',
    )


def _complete_extraction_stage(
    scope: Scope,
    policy: ModelPolicy,
    window: DistillationWindow,
    chunk: DistillationChunk,
    now: datetime,
) -> object:
    organization, team, project, _agent, _session = scope
    call = _provider_call(scope, policy)

    return DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        chunk=chunk,
        stage_kind='extract',
        level=0,
        ordinal=chunk.ordinal,
        target_key=hashlib.sha256(f'target:{chunk.id}'.encode()).hexdigest(),
        stage_key=hashlib.sha256(f'stage:{chunk.id}'.encode()).hexdigest(),
        input_hash=chunk.input_hash,
        input_manifest={'chunk_ordinal': chunk.ordinal},
        prompt_contract='distill_extract.v1',
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='complete',
        attempt_count=1,
        accepted_provider_call=call,
        response_hash=_HEX_A,
        response_size=32,
        output_snapshot={'memories': [], 'no_signal_observation_ids': []},
        output_hash=_HEX_B,
        completed_at=now,
    )


def _expected_window_manifest(work: WorkflowWork, observations: list[Observation]) -> dict[str, object]:
    ordered = sorted(observations, key=lambda item: item.session_sequence)

    return {
        'schema': _WINDOW_MANIFEST_SCHEMA,
        'work_id': str(work.id),
        'work_input_fingerprint': work.input_fingerprint,
        'lower_sequence_exclusive': 0,
        'upper_sequence_inclusive': work.input_snapshot['upper_sequence_inclusive'],
        'observations': [
            {
                'observation_id': str(observation.id),
                'session_sequence': observation.session_sequence,
                'content_digest': observation_content_digest(observation),
            }
            for observation in ordered
        ],
    }


def _manifest_observation_ids(window: object) -> set[str]:
    ids: set[str] = set()
    for chunk in window.chunks.all():
        for entry in chunk.input_manifest['observations']:
            ids.add(entry['observation_id'])

    return ids


@pytest.mark.django_db
def test_window_materialization_uses_exact_scoped_sequence_prefix() -> None:
    scope = _scope('window-prefix')
    _foreign = _scope('window-foreign')
    included_one = _observation(scope, sequence=1)
    _lifecycle = _observation(scope, sequence=2, event_type='session_end')
    included_three = _observation(scope, sequence=3)
    _observation(_foreign, sequence=1)
    work = _session_work(scope, upper=3)
    _late = _observation(scope, sequence=5)

    window = dw.materialize_distillation_window(work)

    assert window.observation_count == 2
    assert window.lower_sequence_exclusive == 0
    assert window.upper_sequence_inclusive == 3
    assert _manifest_observation_ids(window) == {str(included_one.id), str(included_three.id)}


@pytest.mark.django_db
def test_window_and_chunk_hashes_are_stable_across_query_order_and_replay() -> None:
    scope = _scope('window-stable')
    third = _observation(scope, sequence=3)
    first = _observation(scope, sequence=1)
    second = _observation(scope, sequence=2)
    work = _session_work(scope, upper=3)

    window = dw.materialize_distillation_window(work)
    expected = _expected_window_manifest(work, [first, second, third])
    expected_hash = hashlib.sha256(canonical_json_bytes(expected)).hexdigest()

    assert window.input_hash == expected_hash

    chunks = list(window.chunks.order_by('ordinal'))
    ordered_sequences = [
        entry['session_sequence'] for chunk in chunks for entry in chunk.input_manifest['observations']
    ]
    assert ordered_sequences == [1, 2, 3]
    for chunk in chunks:
        chunk_manifest = {
            'schema': _CHUNK_MANIFEST_SCHEMA,
            'window_input_hash': window.input_hash,
            'ordinal': chunk.ordinal,
            'observations': chunk.input_manifest['observations'],
        }
        assert chunk.input_manifest == chunk_manifest
        assert chunk.input_hash == hashlib.sha256(canonical_json_bytes(chunk_manifest)).hexdigest()

    replay = dw.materialize_distillation_window(work)

    assert replay.id == window.id
    assert replay.input_hash == window.input_hash
    assert [chunk.input_hash for chunk in replay.chunks.order_by('ordinal')] == [chunk.input_hash for chunk in chunks]


@requires_postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_window_materialization_converges_on_one_plan() -> None:
    scope = _scope('window-concurrent')
    _observation(scope, sequence=1)
    _observation(scope, sequence=2)
    work = _session_work(scope, upper=2)
    barrier = Barrier(2)
    results: list[uuid.UUID] = []

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            window = dw.materialize_distillation_window(work)
            results.append(window.id)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _index in range(2)]
        for future in futures:
            future.result(timeout=15)

    windows = list(dw.DistillationWindow.objects.filter(work=work))
    assert len(windows) == 1
    assert set(results) == {windows[0].id}
    assert windows[0].chunks.count() >= 1


@pytest.mark.django_db
def test_success_for_generation_n_does_not_cover_failed_generation_n_plus_1() -> None:
    scope = _scope('window-generations')
    _observation(scope, sequence=1)
    _observation(scope, sequence=2)
    _observation(scope, sequence=3)
    later_four = _observation(scope, sequence=4)
    later_five = _observation(scope, sequence=5)

    work_n = _session_work(scope, upper=3)
    work_next = _session_work(scope, upper=5)

    window_n = dw.materialize_distillation_window(work_n)
    window_next = dw.materialize_distillation_window(work_next)

    assert window_n.id != window_next.id
    assert window_n.input_hash != window_next.input_hash
    assert window_n.observation_count == 3
    assert window_next.observation_count == 5

    ids_n = _manifest_observation_ids(window_n)
    assert str(later_four.id) not in ids_n
    assert str(later_five.id) not in ids_n
    assert {str(later_four.id), str(later_five.id)}.issubset(_manifest_observation_ids(window_next))


@pytest.mark.django_db
def test_max_calls_per_attempt_continues_same_work_without_tail_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '2')
    scope = _scope('window-continuation')
    policy = _curation_policy(scope)
    chunk_count = 5
    oversized = 'x' * 130000
    for sequence in range(1, chunk_count + 1):
        _observation(scope, sequence=sequence, body=oversized)
    work = _session_work(scope, upper=chunk_count)

    window = dw.materialize_distillation_window(work)
    assert window.chunks.count() == chunk_count

    max_calls = dw.max_provider_calls_per_attempt()
    assert max_calls == 2

    now = timezone.now()
    supplied_run_id: uuid.UUID | None = None
    continuations = 0

    while dw.next_distillation_stage(window) is not None:
        result = claim_work(
            work_id=work.id,
            expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
            lease_owner=f'host:{continuations}:{uuid.uuid4()}',
            now=now,
            lease_for=_LEASE,
            workflow_run_id=supplied_run_id,
        )
        claim = result.claim
        assert claim is not None

        planned = 0
        chunk = dw.next_distillation_stage(window)
        while chunk is not None and planned < max_calls:
            _complete_extraction_stage(scope, policy, window, chunk, now)
            planned += 1
            chunk = dw.next_distillation_stage(window)

        if dw.next_distillation_stage(window) is not None:
            new_run = dw.continue_distillation_work(work=work, claim=claim, now=now)
            supplied_run_id = new_run.id
            continuations += 1
            now = now + timedelta(seconds=1)
        else:
            finish_work_claim(claim=claim, now=now, completion='product_succeeded')

    refreshed = WorkflowWork.objects.get(id=work.id)
    covered_chunk_ids = list(
        dw.DistillationStage.objects.filter(window=window, stage_kind='extract', status='complete')
        .order_by('chunk__ordinal')
        .values_list('chunk_id', flat=True)
    )
    assert len(covered_chunk_ids) == chunk_count
    assert len(set(covered_chunk_ids)) == chunk_count
    assert continuations == 2
    assert (
        WorkflowRun.objects.filter(
            work=work,
            origin=WorkflowRunOrigin.RECONCILIATION,
            status__in=(WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING, WorkflowRunStatus.SUCCEEDED),
        ).count()
        == continuations
    )
    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session_work_v1').count() == continuations
    assert refreshed.disposition == WorkflowWorkDisposition.COMPLETE


@pytest.mark.django_db
def test_max_provider_calls_per_attempt_config_defaults_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', raising=False)
    assert dw.max_provider_calls_per_attempt() == 8

    monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '1')
    assert dw.max_provider_calls_per_attempt() == 1
    monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '64')
    assert dw.max_provider_calls_per_attempt() == 64

    for invalid in ('0', '65', 'not-a-number'):
        monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', invalid)
        with pytest.raises(ValueError):
            dw.max_provider_calls_per_attempt()


@pytest.mark.django_db
def test_continue_distillation_work_rejects_foreign_claim() -> None:
    scope = _scope('window-continue-guard')
    _observation(scope, sequence=1)
    work = _session_work(scope, upper=1)
    foreign_scope = _scope('window-continue-guard-foreign')
    _observation(foreign_scope, sequence=1)
    foreign_work = _session_work(foreign_scope, upper=1)

    now = timezone.now()
    result = claim_work(
        work_id=foreign_work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:{uuid.uuid4()}',
        now=now,
        lease_for=_LEASE,
    )
    assert result.claim is not None

    with pytest.raises(ValueError):
        dw.continue_distillation_work(work=work, claim=result.claim, now=now)


@pytest.mark.django_db
def test_next_distillation_stage_derives_first_uncovered_extraction_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('window-next-stage')
    policy = _curation_policy(scope)
    oversized = 'x' * 130000
    for sequence in range(1, 4):
        _observation(scope, sequence=sequence, body=oversized)
    work = _session_work(scope, upper=3)

    window = dw.materialize_distillation_window(work)
    ordinals = list(window.chunks.order_by('ordinal').values_list('ordinal', flat=True))
    assert ordinals == [0, 1, 2]

    now = timezone.now()
    seen: list[int] = []
    chunk = dw.next_distillation_stage(window)
    while chunk is not None:
        assert isinstance(chunk, DistillationChunk)
        seen.append(chunk.ordinal)
        _complete_extraction_stage(scope, policy, window, chunk, now)
        chunk = dw.next_distillation_stage(window)

    assert seen == [0, 1, 2]
    assert dw.next_distillation_stage(window) is None


@pytest.mark.django_db
@pytest.mark.parametrize('tamper', ('fingerprint', 'foreign_subject', 'snapshot_schema'))
def test_invalid_scope_or_content_digest_fails_before_provider_call(tamper: str) -> None:
    scope = _scope('window-invalid')
    _observation(scope, sequence=1)
    work = _session_work(scope, upper=1)

    if tamper == 'fingerprint':
        WorkflowWork.objects.filter(id=work.id).update(input_fingerprint='0' * 64)
    elif tamper == 'foreign_subject':
        foreign = _scope('window-invalid-foreign')
        WorkflowWork.objects.filter(id=work.id).update(subject_id=foreign[4].id)
    else:
        tampered = dict(work.input_snapshot)
        tampered['schema'] = 'session_distillation_input/v9'
        WorkflowWork.objects.filter(id=work.id).update(input_snapshot=tampered)

    reloaded = WorkflowWork.objects.get(id=work.id)
    with pytest.raises(MemoryWorkerError) as error:
        dw.materialize_distillation_window(reloaded)

    assert translate_failure(error.value).failure_class == INVALID_INPUT
    assert dw.DistillationWindow.objects.filter(work=work).count() == 0
    assert dw.DistillationStage.objects.filter(window__work=work).count() == 0


@pytest.mark.django_db
def test_replaying_window_rejects_tampered_observation_content() -> None:
    scope = _scope('window-tamper-replay')
    observation = _observation(scope, sequence=1, body='original body')
    work = _session_work(scope, upper=1)

    window = dw.materialize_distillation_window(work)
    Observation.objects.filter(id=observation.id).update(body='forged body')

    with pytest.raises(MemoryWorkerError) as error:
        dw.materialize_distillation_window(work)

    assert translate_failure(error.value).failure_class == INVALID_INPUT
    assert dw.DistillationWindow.objects.filter(work=work).count() == 1
    assert dw.DistillationWindow.objects.get(work=work).id == window.id


@requires_postgres
@pytest.mark.django_db
def test_window_owning_required_work_restores_signal_through_reconciler() -> None:
    scope = _scope('window-reconciler')
    organization, _team, project, _agent, session = scope
    _observation(scope, sequence=1)
    result = EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    work = WorkflowWork.objects.get(id=result.work_id)
    dw.materialize_distillation_window(work)
    CeleryOutbox.objects.all().delete()

    now = timezone.now()
    WorkflowWork.objects.filter(id=work.id).update(created_at=now - _STALE)
    AgentSession.objects.filter(id=session.id).update(ended_at=now - _STALE)

    from engram.memory.session_work_reconciler import reconcile_session_work

    reconciliation = reconcile_session_work(organization_id=organization.id, project_id=project.id, as_of=now)

    assert reconciliation.queued == 1
    refreshed = WorkflowWork.objects.get(id=work.id)
    assert refreshed.disposition == WorkflowWorkDisposition.REQUIRED
    assert refreshed.execution_state != WorkflowWorkExecutionState.SETTLED
    assert (
        WorkflowRun.objects.filter(
            work=work,
            status=WorkflowRunStatus.QUEUED,
            origin=WorkflowRunOrigin.RECONCILIATION,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name='engram.memory.distill_session_work_v1').count() == 1
    assert dw.DistillationWindow.objects.filter(work=work).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('changed_env', 'invalid_value'),
    (
        ('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '7999'),
        ('ENGRAM_DISTILL_REDUCE_TARGET', '0'),
    ),
)
def test_existing_window_replay_uses_frozen_planner_configuration(
    changed_env: str,
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    suffix = 'frozen-replay-budget' if changed_env.endswith('BUDGET') else 'frozen-replay-target'
    scope = _scope(suffix)
    _observation(scope, sequence=1)
    work = _session_work(scope, upper=1)

    window = dw.materialize_distillation_window(work)
    assert window.chunk_char_budget == 8000
    assert window.reduction_target == 1

    monkeypatch.setenv(changed_env, invalid_value)

    replay = dw.materialize_distillation_window(work)

    assert replay.id == window.id
    assert replay.chunk_char_budget == 8000
    assert replay.reduction_target == 1


@pytest.mark.django_db
def test_extract_reuse_key_stable_across_windows_for_identical_prefix_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    oversized_body = 'x' * 9000
    scope = _scope('reuse-key-prefix-stable')
    _observation(scope, sequence=1, body=oversized_body)
    work_a = _session_work(scope, upper=1)
    window_a = dw.materialize_distillation_window(work_a)
    chunk_a = window_a.chunks.get(ordinal=0)

    _observation(scope, sequence=2, body=oversized_body)
    work_b = _session_work(scope, upper=2)
    window_b = dw.materialize_distillation_window(work_b)
    chunk_b = window_b.chunks.get(ordinal=0)

    assert window_a.id != window_b.id
    assert window_a.input_hash != window_b.input_hash
    assert chunk_a.input_manifest['observations'] == chunk_b.input_manifest['observations']
    assert extract_reuse_key(chunk_a) == extract_reuse_key(chunk_b)


def test_extract_reuse_key_changes_when_observation_content_digest_changes() -> None:
    window = SimpleNamespace(chunk_char_budget=8000)
    chunk_a = SimpleNamespace(
        window=window,
        input_manifest={'observations': [{'observation_id': 'obs-1', 'content_digest': _HEX_A}]},
    )
    chunk_b = SimpleNamespace(
        window=window,
        input_manifest={'observations': [{'observation_id': 'obs-1', 'content_digest': _HEX_B}]},
    )

    assert extract_reuse_key(chunk_a) != extract_reuse_key(chunk_b)


@pytest.mark.django_db
def test_plan_chunks_full_chunks_are_prefix_stable_under_append() -> None:
    budget = 8000
    oversized_body = 'x' * (budget + 500)
    scope = _scope('plan-chunks-prefix-stable')
    observations = [_observation(scope, sequence=sequence, body=oversized_body) for sequence in range(1, 5)]
    entries = dw._manifest_entries(observations)

    shorter = dw._plan_chunks(entries[:2], budget)
    longer = dw._plan_chunks(entries[:4], budget)

    for ordinal in range(len(shorter) - 1):
        shorter_ids = [entry.payload['observation_id'] for entry in shorter[ordinal]]
        longer_ids = [entry.payload['observation_id'] for entry in longer[ordinal]]
        assert shorter_ids == longer_ids
    assert len(longer) >= len(shorter)
