from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from django.db.models import F
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    DistillationChunk,
    DistillationObservationCoverage,
    DistillationStage,
    DistillationWindow,
    MemoryCandidate,
    Observation,
    Organization,
    Project,
    Runtime,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory import distillation_provider_stage as dps
from engram.memory import work_execution
from engram.memory.distillation_window import materialize_distillation_window
from engram.memory.work_execution import StaleWorkFenceError, WorkClaim, claim_work
from engram.memory.work_failures import CONFIGURATION, PROVIDER_TRANSIENT
from engram.memory.workflow_work import CreateWorkflowWorkInput, create_work
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import ProviderCallResult

Scope = tuple[Organization, Team, Project, Agent, AgentSession]

_LEASE = timedelta(seconds=720)
_HEX_C = 'c' * 64
_HEX_F = 'f' * 64
_GATEWAY_TARGET = 'engram.memory.distillation_provider_stage.get_provider_gateway'
_FENCE_TARGET = 'engram.memory.distillation_provider_stage.lock_work_fence'


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


def _scope(suffix: str) -> Scope:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')
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


def _observation(scope: Scope, *, sequence: int, body: str = '') -> Observation:
    organization, team, project, agent, session = scope

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {sequence}',
        body=body or f'body {sequence}',
        content_hash=f'content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': 'post_tool_use'},
    )


def _session_work(scope: Scope, *, upper: int) -> WorkflowWork:
    organization, _team, project, _agent, session = scope
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


def _policy(scope: Scope, *, task_type: str, name: str, version: int, fallback_enabled: bool = False) -> ModelPolicy:
    organization, team, project, _agent, _session = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'{name} secret',
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
        name=name,
        scope='project',
        task_type=task_type,
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=version,
        fallback_enabled=fallback_enabled,
    )


def _curation_policy(scope: Scope, *, fallback_enabled: bool = False) -> ModelPolicy:
    return _policy(scope, task_type='curation', name='Curation policy', version=2, fallback_enabled=fallback_enabled)


def _generation_policy(scope: Scope) -> ModelPolicy:
    return _policy(scope, task_type='generation', name='Generation policy', version=3)


def _claim(work: WorkflowWork, now: datetime) -> WorkClaim:
    result = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:{uuid.uuid4()}',
        now=now,
        lease_for=_LEASE,
    )
    assert result.claim is not None

    return result.claim


def _single_chunk(
    scope: Scope, *, sequences: tuple[int, ...], bodies: tuple[str, ...] | None = None
) -> tuple[WorkflowWork, DistillationWindow, DistillationChunk]:
    for index, sequence in enumerate(sequences):
        _observation(scope, sequence=sequence, body=bodies[index] if bodies else f'body {sequence}')
    work = _session_work(scope, upper=max(sequences))
    window = materialize_distillation_window(work)
    chunk = window.chunks.order_by('ordinal').first()
    assert chunk is not None
    assert window.chunks.count() == 1

    return work, window, chunk


def _chunk_observation_ids(chunk: DistillationChunk) -> list[str]:
    return [entry['observation_id'] for entry in chunk.input_manifest['observations']]


def _valid_body(chunk: DistillationChunk, *, kind: str | None = None) -> str:
    observation_ids = _chunk_observation_ids(chunk)
    memory: dict[str, object] = {
        'title': 'Durable engineering fact',
        'body': 'A runtime-neutral durable engineering memory.',
        'confidence': 0.9,
        'supporting_observation_ids': [observation_ids[0]],
    }
    if kind is not None:
        memory['kind'] = kind

    return json.dumps({'memories': [memory], 'no_signal_observation_ids': observation_ids[1:]})


class _StubGateway:
    def __init__(
        self,
        *,
        body: str = '',
        error: BaseException | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[object] = []
        self._body = body
        self._error = error
        self._on_call = on_call

    def call(self, data: object) -> ProviderCallResult:
        self.calls.append(data)
        policy = data.policy
        record = ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=getattr(data, 'trace_id', ''),
            redaction_state='redacted',
            metadata={'prompt_retained': False},
        )
        if self._on_call is not None:
            self._on_call()
        if self._error is not None:
            raise self._error

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state='redacted',
            generated_title='',
            generated_body=self._body,
        )


def _install_gateway(m_monkeypatch: pytest.MonkeyPatch, gateway: _StubGateway) -> None:
    m_monkeypatch.setattr(_GATEWAY_TARGET, lambda *_args, **_kwargs: gateway)


def _install_gateways(m_monkeypatch: pytest.MonkeyPatch, by_policy_id: dict[uuid.UUID, _StubGateway]) -> None:
    m_monkeypatch.setattr(_GATEWAY_TARGET, lambda policy, *_args, **_kwargs: by_policy_id[policy.id])


@pytest.mark.django_db
def test_stage_key_binds_work_snapshot_kind_chunk_input_and_policy_version() -> None:
    base_target = {
        'work_id': str(uuid.uuid4()),
        'work_input_fingerprint': _HEX_F,
        'window_input_hash': 'a' * 64,
        'stage_kind': 'extract',
        'level': 0,
        'ordinal': 0,
        'chunk_ordinal': 0,
        'input_hash': 'b' * 64,
        'prompt_contract': 'distill_extract.v1',
    }
    base_key = dps.stage_target_key(**base_target)
    assert base_key == dps.stage_target_key(**base_target)

    variations: dict[str, object] = {
        'work_id': str(uuid.uuid4()),
        'work_input_fingerprint': 'e' * 64,
        'window_input_hash': 'd' * 64,
        'stage_kind': 'reduce',
        'level': 1,
        'ordinal': 5,
        'chunk_ordinal': None,
        'input_hash': '9' * 64,
        'prompt_contract': 'distill_reduce.v1',
    }
    for name, value in variations.items():
        assert dps.stage_target_key(**{**base_target, name: value}) != base_key, name

    base_stage = {'target_key': base_key, 'policy_id': str(uuid.uuid4()), 'policy_version': 1, 'policy_role': 'primary'}
    base_stage_key = dps.stage_key(**base_stage)
    assert base_stage_key == dps.stage_key(**base_stage)
    for name, value in {'policy_id': str(uuid.uuid4()), 'policy_version': 2, 'policy_role': 'fallback'}.items():
        assert dps.stage_key(**{**base_stage, name: value}) != base_stage_key, name

    scope = _scope('stage-identity')
    policy = _curation_policy(scope)
    work, window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)

    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)
    replay = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    assert stage.id == replay.id
    assert stage.stage_key == replay.stage_key
    assert stage.policy_role == 'primary'
    assert stage.stage_kind == 'extract'
    assert stage.level == 0
    assert stage.prompt_contract == 'distill_extract.v1'
    assert stage.target_key == dps.stage_target_key(
        work_id=str(work.id),
        work_input_fingerprint=work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind='extract',
        level=0,
        ordinal=chunk.ordinal,
        chunk_ordinal=chunk.ordinal,
        input_hash=chunk.input_hash,
        prompt_contract='distill_extract.v1',
    )

    ModelPolicy.objects.filter(id=policy.id).update(version=policy.version + 5)
    rotated = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    assert rotated.id != stage.id
    assert rotated.stage_key != stage.stage_key
    assert rotated.target_key == stage.target_key
    assert rotated.policy_version == policy.version + 5
    assert DistillationStage.objects.get(id=stage.id).status == 'required'


@pytest.mark.django_db
def test_completed_stage_replay_uses_normalized_output_without_provider_call(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope('stage-replay')
    _curation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)
    gateway = _StubGateway(body=_valid_body(chunk))
    _install_gateway(m_monkeypatch, gateway)

    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)
    first = dps.execute_distillation_stage(stage, claim, now=now)

    assert first.status == 'completed'
    completed = DistillationStage.objects.get(id=stage.id)
    assert completed.status == 'complete'
    assert completed.output_snapshot is not None
    assert len(gateway.calls) == 1
    accepted_call_id = completed.accepted_provider_call_id
    output_snapshot = completed.output_snapshot
    output_hash = completed.output_hash
    attempt_count = completed.attempt_count

    later = dps.execute_distillation_stage(stage, claim, now=now + timedelta(seconds=30))

    assert later.status == 'completed'
    replayed = DistillationStage.objects.get(id=stage.id)
    assert len(gateway.calls) == 1
    assert replayed.accepted_provider_call_id == accepted_call_id
    assert replayed.output_snapshot == output_snapshot
    assert replayed.output_hash == output_hash
    assert replayed.attempt_count == attempt_count
    assert ProviderCallRecord.objects.filter(request_id=f'distill-stage:{stage.stage_key}').count() == 1


@pytest.mark.django_db
def test_crash_after_provider_response_replays_to_one_durable_decision(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope('stage-crash')
    _curation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)
    gateway = _StubGateway(body=_valid_body(chunk))
    _install_gateway(m_monkeypatch, gateway)
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    real_lock = work_execution.lock_work_fence
    state = {'count': 0}

    def flaky_lock(*args: object, **kwargs: object) -> object:
        state['count'] += 1
        if state['count'] == 1:
            raise RuntimeError('injected crash after provider response')

        return real_lock(*args, **kwargs)

    m_monkeypatch.setattr(_FENCE_TARGET, flaky_lock)

    with pytest.raises(RuntimeError):
        dps.execute_distillation_stage(stage, claim, now=now)

    crashed = DistillationStage.objects.get(id=stage.id)
    assert crashed.status == 'required'
    assert crashed.output_snapshot is None
    assert crashed.accepted_provider_call_id is None
    assert len(gateway.calls) == 1

    result = dps.execute_distillation_stage(stage, claim, now=now + timedelta(seconds=5))

    assert result.status == 'completed'
    records = ProviderCallRecord.objects.filter(request_id=f'distill-stage:{stage.stage_key}')
    assert records.count() == 2
    assert len(gateway.calls) == 2
    settled = DistillationStage.objects.get(id=stage.id)
    assert settled.status == 'complete'
    assert settled.accepted_provider_call_id in set(records.values_list('id', flat=True))
    complete_targets = DistillationStage.objects.filter(
        window__work=work,
        target_key=stage.target_key,
        status='complete',
    )
    assert complete_targets.count() == 1


def _malformed_body(chunk: DistillationChunk, variant: str) -> str:
    observation_ids = _chunk_observation_ids(chunk)
    first, second = observation_ids[0], observation_ids[1]
    if variant == 'invalid_json':
        return 'this is definitely not json'

    if variant == 'missing_keys':
        return json.dumps(
            {'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'supporting_observation_ids': [first]}]}
        )

    if variant == 'invalid_confidence':
        return json.dumps(
            {
                'memories': [{'title': 'T', 'body': 'B', 'confidence': 1.5, 'supporting_observation_ids': [first]}],
                'no_signal_observation_ids': [second],
            }
        )

    if variant == 'unknown_ids':
        return json.dumps(
            {
                'memories': [
                    {'title': 'T', 'body': 'B', 'confidence': 0.9, 'supporting_observation_ids': [str(uuid.uuid4())]}
                ],
                'no_signal_observation_ids': [first, second],
            }
        )

    return json.dumps(
        {
            'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'supporting_observation_ids': [first]}],
            'no_signal_observation_ids': [],
        }
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'variant',
    ('invalid_json', 'missing_keys', 'invalid_confidence', 'unknown_ids', 'incomplete_coverage'),
)
def test_malformed_extraction_never_creates_candidate_or_coverage(
    m_monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    scope = _scope(f'stage-malformed-{variant}')
    _curation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1, 2))
    now = timezone.now()
    claim = _claim(work, now)
    gateway = _StubGateway(body=_malformed_body(chunk, variant))
    _install_gateway(m_monkeypatch, gateway)
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    result = dps.execute_distillation_stage(stage, claim, now=now)

    assert result.status == 'retry'
    assert result.failure is not None
    assert result.failure.failure_class == PROVIDER_TRANSIENT
    assert result.failure.code == dps.PROVIDER_OUTPUT_MALFORMED
    assert result.fallback_used is False

    refreshed = DistillationStage.objects.get(id=stage.id)
    assert refreshed.status == 'required'
    assert refreshed.output_snapshot is None
    assert refreshed.accepted_provider_call_id is None
    assert refreshed.response_hash == ''
    assert refreshed.response_size is None
    assert refreshed.last_failure_class == dps.PROVIDER_OUTPUT_MALFORMED
    assert refreshed.last_failure_at is not None
    assert MemoryCandidate.objects.filter(organization=scope[0]).count() == 0
    assert DistillationObservationCoverage.objects.filter(window__work=work).count() == 0


@pytest.mark.django_db
def test_malformed_primary_uses_one_safe_fallback_with_distinct_stage_key(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope('stage-fallback')
    primary_policy = _curation_policy(scope, fallback_enabled=True)
    fallback_policy = _generation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)
    primary_gateway = _StubGateway(body='not valid extraction json')
    fallback_gateway = _StubGateway(body=_valid_body(chunk))
    _install_gateways(m_monkeypatch, {primary_policy.id: primary_gateway, fallback_policy.id: fallback_gateway})
    primary_stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    result = dps.execute_distillation_stage(primary_stage, claim, now=now)

    assert result.status == 'completed'
    assert result.fallback_used is True
    assert len(primary_gateway.calls) == 1
    assert len(fallback_gateway.calls) == 1

    refreshed_primary = DistillationStage.objects.get(id=primary_stage.id)
    assert refreshed_primary.status == 'required'
    assert refreshed_primary.policy_role == 'primary'
    assert refreshed_primary.last_failure_class == dps.PROVIDER_OUTPUT_MALFORMED

    fallback_stage = DistillationStage.objects.get(
        window__work=work,
        target_key=primary_stage.target_key,
        policy_role='fallback',
    )
    assert fallback_stage.status == 'complete'
    assert fallback_stage.policy_id == fallback_policy.id
    assert fallback_stage.stage_key != primary_stage.stage_key
    assert fallback_stage.target_key == primary_stage.target_key
    assert result.stage.id == fallback_stage.id
    assert (
        DistillationStage.objects.filter(
            window__work=work,
            target_key=primary_stage.target_key,
            status='complete',
        ).count()
        == 1
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'label,error,fallback_enabled,expected_status,expected_class,expected_code',
    (
        (
            'timeout',
            ModelPolicyError('provider_timeout', 'provider timed out', retryable=True),
            False,
            'retry',
            PROVIDER_TRANSIENT,
            'provider_timeout',
        ),
        (
            'rate_limited',
            ModelPolicyError('provider_http_error', 'provider returned 429', retryable=True, http_status=429),
            False,
            'retry',
            PROVIDER_TRANSIENT,
            'provider_rate_limited',
        ),
        (
            'server_error',
            ModelPolicyError('provider_http_error', 'provider returned 503', retryable=True, http_status=503),
            False,
            'retry',
            PROVIDER_TRANSIENT,
            'provider_unavailable',
        ),
        (
            'scope',
            ModelPolicyError('policy_scope_mismatch', 'policy scope invalid'),
            True,
            'blocked',
            CONFIGURATION,
            'policy_scope_invalid',
        ),
        (
            'secret',
            ProviderSecretError('provider secret is disabled'),
            True,
            'blocked',
            CONFIGURATION,
            'provider_secret_unavailable',
        ),
    ),
)
def test_timeout_429_and_5xx_retry_but_scope_and_configuration_fail_closed(
    m_monkeypatch: pytest.MonkeyPatch,
    label: str,
    error: BaseException,
    fallback_enabled: bool,
    expected_status: str,
    expected_class: str,
    expected_code: str,
) -> None:
    scope = _scope(f'stage-outcome-{label}')
    primary_policy = _curation_policy(scope, fallback_enabled=fallback_enabled)
    fallback_policy = _generation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)
    primary_gateway = _StubGateway(error=error)
    fallback_gateway = _StubGateway(body=_valid_body(chunk))
    _install_gateways(m_monkeypatch, {primary_policy.id: primary_gateway, fallback_policy.id: fallback_gateway})
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    result = dps.execute_distillation_stage(stage, claim, now=now)

    assert result.status == expected_status
    assert result.failure is not None
    assert result.failure.failure_class == expected_class
    assert result.failure.code == expected_code
    assert len(fallback_gateway.calls) == 0

    refreshed = DistillationStage.objects.get(id=stage.id)
    assert refreshed.status == 'required'
    assert refreshed.output_snapshot is None


@pytest.mark.django_db
def test_stale_fence_cannot_commit_returned_provider_output(m_monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('stage-stale-fence')
    _curation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))
    now = timezone.now()
    claim = _claim(work, now)

    def steal_fence() -> None:
        WorkflowWork.objects.filter(id=work.id).update(fencing_token=F('fencing_token') + 1)

    gateway = _StubGateway(body=_valid_body(chunk), on_call=steal_fence)
    _install_gateway(m_monkeypatch, gateway)
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    with pytest.raises(StaleWorkFenceError):
        dps.execute_distillation_stage(stage, claim, now=now)

    assert len(gateway.calls) == 1
    refreshed = DistillationStage.objects.get(id=stage.id)
    assert refreshed.status == 'required'
    assert refreshed.output_snapshot is None
    assert refreshed.accepted_provider_call_id is None
    complete_targets = DistillationStage.objects.filter(
        window__work=work,
        target_key=stage.target_key,
        status='complete',
    )
    assert complete_targets.count() == 0


@pytest.mark.django_db
def test_worker_rejects_cross_scope_subject_before_provider_call(m_monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('stage-scope-owner')
    _curation_policy(scope)
    work, _window, chunk = _single_chunk(scope, sequences=(1,))

    foreign_scope = _scope('stage-scope-foreign')
    _observation(foreign_scope, sequence=1)
    foreign_work = _session_work(foreign_scope, upper=1)

    now = timezone.now()
    owner_claim = _claim(work, now)
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=owner_claim, now=now)

    foreign_claim = _claim(foreign_work, now)
    gateway = _StubGateway(body=_valid_body(chunk))
    _install_gateway(m_monkeypatch, gateway)

    with pytest.raises((StaleWorkFenceError, ValueError, dps.ExtractionContractError)):
        dps.execute_distillation_stage(stage, foreign_claim, now=now)

    assert len(gateway.calls) == 0
    assert ProviderCallRecord.objects.filter(request_id=f'distill-stage:{stage.stage_key}').count() == 0
    refreshed = DistillationStage.objects.get(id=stage.id)
    assert refreshed.status == 'required'
    assert refreshed.attempt_count == 0


@pytest.mark.django_db
def test_stage_audit_retains_hashes_not_prompt_or_response_content(m_monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('stage-audit')
    _curation_policy(scope)
    prompt_marker = 'PROMPTONLYMARKER7f3a9'
    work, _window, chunk = _single_chunk(scope, sequences=(1,), bodies=(f'observation body {prompt_marker}',))
    now = timezone.now()
    claim = _claim(work, now)
    raw_body = '```json\n' + _valid_body(chunk) + '\n```'
    gateway = _StubGateway(body=raw_body)
    _install_gateway(m_monkeypatch, gateway)
    stage = dps.resolve_extraction_stage(chunk=chunk, claim=claim, now=now)

    result = dps.execute_distillation_stage(stage, claim, now=now)

    assert result.status == 'completed'
    completed = DistillationStage.objects.get(id=stage.id)
    assert completed.response_hash == hashlib.sha256(raw_body.encode('utf-8')).hexdigest()
    assert completed.response_size == len(raw_body.encode('utf-8'))
    assert len(completed.output_hash) == 64
    assert int(completed.output_hash, 16) >= 0
    assert set(completed.output_snapshot.keys()) == {'memories', 'no_signal_observation_ids'}

    serialized_stage = json.dumps(
        {
            'output_snapshot': completed.output_snapshot,
            'input_manifest': completed.input_manifest,
            'response_hash': completed.response_hash,
            'output_hash': completed.output_hash,
            'last_failure_class': completed.last_failure_class,
        }
    )
    assert prompt_marker not in serialized_stage
    assert '```' not in serialized_stage
    assert raw_body not in serialized_stage
