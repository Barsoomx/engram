from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from django.db import connection

from engram.access.services import AccessDeniedError, EffectiveScope
from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    Observation,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.memory.memory_worker_tests import (
    create_embedding_policy,
    create_generation_policy,
    create_observation_recorded_scope,
    create_provenanced_memory_candidate,
    enable_auto_promote,
    execute_worker,
)
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    ProcessObservationRecorded,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    call_with_fallback,
    distillation_system_prompt,
    ensure_memory_visibility_scope,
    provider_prompt,
    realtime_generation_system_prompt,
    realtime_provider_prompt,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    FakeProviderGateway,
    ModelPolicyError,
    ProviderCallInput,
    ProviderCallResult,
    ResolvedModelPolicy,
)


@pytest.mark.django_db
def test_promote_memory_candidate_existing_result_skips_reindex_for_stale_memory() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_provenanced_memory_candidate(observation)
    first = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
    first.memory.stale = True
    first.memory.save(update_fields=['stale', 'updated_at'])
    candidate.refresh_from_db()

    second = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert second.duplicate is True
    assert second.retrieval_document.id == first.retrieval_document.id
    assert RetrievalDocument.objects.count() == 1


@pytest.mark.django_db
def test_promote_memory_candidate_writes_kind_into_memory_metadata_when_set() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_provenanced_memory_candidate(observation)
    candidate.kind = 'gotcha'
    candidate.save(update_fields=['kind', 'updated_at'])

    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert result.memory.metadata['kind'] == 'gotcha'
    assert result.memory.kind == 'gotcha'


@pytest.mark.django_db
def test_promote_memory_candidate_omits_kind_from_memory_metadata_when_unset() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_provenanced_memory_candidate(observation)

    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert 'kind' not in result.memory.metadata
    assert result.memory.kind == ''


@pytest.mark.django_db(transaction=True)
def test_generate_candidate_provider_call_has_no_open_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observed_in_atomic: list[bool] = []
    real_gateway = FakeProviderGateway()

    class _RecordingGateway(FakeProviderGateway):
        def call(self, data: object) -> object:
            observed_in_atomic.append(connection.in_atomic_block)

            return real_gateway.call(data)

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _RecordingGateway())

    execute_worker(observation)

    assert observed_in_atomic == [False]


@pytest.mark.django_db
def test_process_observation_generation_uses_fresh_provider_response_on_repeated_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    policy = create_generation_policy(organization, team, project)
    request_id = f'memory-worker:{observation.id}:generation'
    ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=request_id,
        trace_id='trace-preexisting-generation',
        redaction_state='clean',
        token_usage={'input_tokens': 1, 'output_tokens': 0},
        cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
        metadata={'prompt_retained': False},
    )

    class _ProviderResponseGateway(FakeProviderGateway):
        def call(self, data: object) -> ProviderCallResult:
            real = FakeProviderGateway.call(self, data)

            return ProviderCallResult(
                provider=real.provider,
                model=real.model,
                call_record_id=real.call_record_id,
                redaction_state=real.redaction_state,
                generated_title='Fresh provider title',
                generated_body='Fresh provider body distinct from prompt',
            )

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _ProviderResponseGateway())

    result = execute_worker(observation)

    assert result.candidate is not None
    assert result.candidate.body == 'Fresh provider body distinct from prompt'
    assert provider_prompt(observation) not in result.candidate.body
    assert ProviderCallRecord.objects.filter(request_id=request_id).count() == 2


@pytest.mark.django_db(transaction=True)
def test_promote_memory_candidate_defers_embedding_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_embedding_policy(organization, team, project)
    candidate = create_provenanced_memory_candidate(observation)
    observed_in_atomic: list[bool] = []
    real_gateway = FakeProviderGateway()

    class _RecordingGateway(FakeProviderGateway):
        def embed(self, data: object) -> object:
            observed_in_atomic.append(connection.in_atomic_block)

            return real_gateway.embed(data)

    monkeypatch.setattr('engram.context.services.get_provider_gateway', lambda *_, **__: _RecordingGateway())

    PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert observed_in_atomic == []


def test_provider_prompt_includes_facts_narrative_concepts() -> None:
    observation = Observation(
        title='T',
        body='B',
        facts=['fact one'],
        narrative='narrative text',
        concepts=['gotcha'],
        files_read=[],
        files_modified=[],
        source_metadata={},
    )

    prompt = provider_prompt(observation)

    assert 'Facts:' in prompt
    assert 'fact one' in prompt
    assert 'Narrative: narrative text' in prompt
    assert 'Concepts:' in prompt


def test_distillation_system_prompt_declares_skip_protocol() -> None:
    assert 'SKIP' in distillation_system_prompt()


@pytest.mark.django_db
def test_process_observation_skip_creates_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)

    class _SkipGateway(FakeProviderGateway):
        def call(self, data: object) -> ProviderCallResult:
            real = FakeProviderGateway.call(self, data)

            return ProviderCallResult(
                provider=real.provider,
                model=real.model,
                call_record_id=real.call_record_id,
                redaction_state=real.redaction_state,
                generated_title='',
                generated_body='{"memories": []}',
            )

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _SkipGateway())

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()
    assert AuditEvent.objects.filter(
        event_type='MemoryCandidateSkipped',
        target_id=str(observation.id),
    ).exists()


@pytest.mark.django_db
def test_process_observation_skip_is_sticky_across_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    second_run_calls: list[int] = []

    class _SkipGateway(FakeProviderGateway):
        def call(self, data: object) -> ProviderCallResult:
            real = FakeProviderGateway.call(self, data)

            return ProviderCallResult(
                provider=real.provider,
                model=real.model,
                call_record_id=real.call_record_id,
                redaction_state=real.redaction_state,
                generated_title='',
                generated_body='{"memories": []}',
            )

    class _CountingGateway(FakeProviderGateway):
        def call(self, data: object) -> ProviderCallResult:
            second_run_calls.append(1)

            return FakeProviderGateway.call(self, data)

    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _SkipGateway())
    ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: _CountingGateway())

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.duplicate is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()
    assert (
        AuditEvent.objects.filter(
            event_type='MemoryCandidateSkipped',
            target_id=str(observation.id),
        ).count()
        == 1
    )
    assert second_run_calls == []


class _CandidatesBodyGateway(FakeProviderGateway):
    def __init__(self, body: str) -> None:
        self._body = body

    def call(self, data: object) -> ProviderCallResult:
        real = FakeProviderGateway.call(self, data)

        return ProviderCallResult(
            provider=real.provider,
            model=real.model,
            call_record_id=real.call_record_id,
            redaction_state=real.redaction_state,
            generated_title='',
            generated_body=self._body,
        )


class _NoCallGateway(FakeProviderGateway):
    def __init__(self) -> None:
        self.calls = 0

    def call(self, data: object) -> ProviderCallResult:
        self.calls += 1

        return FakeProviderGateway.call(self, data)


def _single_candidate_body(*, title: str, body: str, confidence: float, kind: str | None = None) -> str:
    memory: dict[str, object] = {'title': title, 'body': body, 'confidence': confidence}
    if kind is not None:
        memory['kind'] = kind

    return json.dumps({'memories': [memory]})


@pytest.mark.django_db
def test_process_observation_uses_model_confidence_and_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    body = _single_candidate_body(
        title='pytest fails without the memory worker module',
        body='Import path was wrong; adding the module makes the suite exit 0.',
        confidence=0.85,
        kind='gotcha',
    )
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _CandidatesBodyGateway(body),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.title == 'pytest fails without the memory worker module'
    assert candidate.confidence == Decimal('0.850')
    assert candidate.kind == 'gotcha'


@pytest.mark.django_db
def test_process_observation_high_model_confidence_holds_legacy_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization, threshold='0.700')
    body = _single_candidate_body(
        title='Consumer acks before processing loses messages on restart',
        body='worker/queue.py acknowledges before processing; ack after processing instead.',
        confidence=0.9,
        kind='gotcha',
    )
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _CandidatesBodyGateway(body),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.confidence == Decimal('0.900')
    assert candidate.decision_work_contract_version == 0
    assert candidate.status == CandidateStatus.PROPOSED
    assert result.held_for_review is True
    assert result.memory is None


@pytest.mark.django_db
def test_process_observation_handles_fenced_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    inner = _single_candidate_body(
        title='Retrieval ranks by cosine similarity',
        body='Documents are ranked by cosine similarity over pgvector embeddings.',
        confidence=0.8,
    )
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _CandidatesBodyGateway(f'```json\n{inner}\n```'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.title == 'Retrieval ranks by cosine similarity'
    assert candidate.confidence == Decimal('0.800')
    assert not candidate.title.startswith('```')


@pytest.mark.django_db
def test_process_observation_empty_memories_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _CandidatesBodyGateway('{"memories": []}'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()


@pytest.mark.django_db
def test_process_observation_parse_failure_yields_zero_confidence_never_promotable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    enable_auto_promote(organization, threshold='0.000')
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _CandidatesBodyGateway('not json and not fenced either'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.confidence == Decimal('0.000')
    assert candidate.evidence[0]['parse_fallback'] is True
    assert result.held_for_review is True
    assert result.memory is None


@pytest.mark.django_db
def test_process_observation_short_content_skipped_without_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observation.title = 'tiny'
    observation.body = 'note'
    observation.save(update_fields=['title', 'body', 'updated_at'])
    gateway = _NoCallGateway()
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: gateway)

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert gateway.calls == 0
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()
    skip_audit = AuditEvent.objects.get(event_type='MemoryCandidateSkipped', target_id=str(observation.id))
    assert skip_audit.metadata['reason'] == 'content_below_min'


@pytest.mark.django_db
def test_process_observation_session_start_skipped_without_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    observation.observation_type = 'session_start'
    observation.save(update_fields=['observation_type', 'updated_at'])
    gateway = _NoCallGateway()
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_, **__: gateway)

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert gateway.calls == 0
    skip_audit = AuditEvent.objects.get(event_type='MemoryCandidateSkipped', target_id=str(observation.id))
    assert skip_audit.metadata['reason'] == 'lifecycle_event'


def test_realtime_generation_system_prompt_declares_memories_contract() -> None:
    prompt = realtime_generation_system_prompt()

    assert '"memories"' in prompt
    assert '{"memories": []}' in prompt
    assert 'confidence' in prompt


def test_realtime_provider_prompt_truncates_huge_body() -> None:
    observation = Observation(
        title='T',
        body='x' * 5000,
        facts=[],
        narrative='',
        concepts=[],
        files_read=[],
        files_modified=[],
        source_metadata={},
    )

    prompt = realtime_provider_prompt(observation, 200)

    assert len(prompt) <= 200 + len('\n[truncated 99999 chars]')
    assert '[truncated' in prompt


def create_fallback_scope() -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name='Fallback Org', slug='fallback-org')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )

    return organization, team, project


def create_fallback_policy(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    task_type: str,
    fallback_enabled: bool = False,
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'{task_type} secret',
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
        name=f'{task_type} policy',
        scope='project',
        task_type=task_type,
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
        fallback_enabled=fallback_enabled,
    )


def fallback_provider_call_input(
    organization: Organization,
    team: Team,
    project: Project,
    policy: ModelPolicy,
) -> ProviderCallInput:
    return ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=team.id,
        policy=policy,
        request_id='fallback-request',
        trace_id='fallback-trace',
        prompt='prompt text',
        system_prompt='system prompt',
        response_kind='candidates',
    )


def fallback_provider_call_result(policy: ModelPolicy) -> ProviderCallResult:
    return ProviderCallResult(
        provider=policy.provider,
        model=policy.model,
        call_record_id=uuid.uuid4(),
        redaction_state='clean',
        generated_title='title',
        generated_body='body',
    )


class _RaisingGateway:
    def __init__(self, error: ModelPolicyError) -> None:
        self._error = error
        self.calls: list[ProviderCallInput] = []

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls.append(data)
        raise self._error


class _StubGateway:
    def __init__(self, *, result: ProviderCallResult) -> None:
        self._result = result
        self.calls: list[ProviderCallInput] = []

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls.append(data)

        return self._result


@pytest.mark.django_db
def test_call_with_fallback_uses_fallback_gateway_on_model_policy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_fallback_scope()
    primary_policy = create_fallback_policy(organization, team, project, task_type='curation', fallback_enabled=True)
    generation_policy = create_fallback_policy(organization, team, project, task_type='generation')
    resolved = ResolvedModelPolicy(policy=primary_policy)
    error = ModelPolicyError('provider_http_error', 'provider returned 400', retryable=False, http_status=400)
    primary_gateway = _RaisingGateway(error)
    fallback_result = fallback_provider_call_result(generation_policy)
    fallback_gateway = _StubGateway(result=fallback_result)
    monkeypatch.setattr('engram.memory.services.get_provider_gateway', lambda *_args, **_kwargs: fallback_gateway)
    data = fallback_provider_call_input(organization, team, project, primary_policy)

    result, used_resolved = call_with_fallback(resolved, primary_gateway, data)

    assert result is fallback_result
    assert used_resolved.policy == generation_policy
    assert primary_gateway.calls == [data]
    assert fallback_gateway.calls[0].policy == generation_policy
    audit = AuditEvent.objects.get(event_type='ProviderFallbackUsed')
    assert audit.metadata['primary_policy_id'] == str(primary_policy.id)
    assert audit.metadata['fallback_policy_id'] == str(generation_policy.id)
    assert audit.metadata['task_type'] == 'curation'
    assert audit.metadata['error_code'] == 'provider_http_error'


@pytest.mark.django_db
def test_call_with_fallback_reraises_when_fallback_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_fallback_scope()
    primary_policy = create_fallback_policy(organization, team, project, task_type='curation', fallback_enabled=False)
    create_fallback_policy(organization, team, project, task_type='generation')
    resolved = ResolvedModelPolicy(policy=primary_policy)
    error = ModelPolicyError('provider_http_error', 'provider returned 400', retryable=False, http_status=400)
    primary_gateway = _RaisingGateway(error)
    fallback_calls: list[ModelPolicy] = []
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda policy, **_kwargs: fallback_calls.append(policy) or FakeProviderGateway(),
    )
    data = fallback_provider_call_input(organization, team, project, primary_policy)

    with pytest.raises(ModelPolicyError) as exc_info:
        call_with_fallback(resolved, primary_gateway, data)

    assert exc_info.value is error
    assert fallback_calls == []
    assert not AuditEvent.objects.filter(event_type='ProviderFallbackUsed').exists()


@pytest.mark.django_db
def test_call_with_fallback_reraises_when_fallback_resolves_to_same_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_fallback_scope()
    primary_policy = create_fallback_policy(
        organization,
        team,
        project,
        task_type='generation',
        fallback_enabled=True,
    )
    resolved = ResolvedModelPolicy(policy=primary_policy)
    error = ModelPolicyError('provider_http_error', 'provider returned 400', retryable=False, http_status=400)
    primary_gateway = _RaisingGateway(error)
    fallback_calls: list[ModelPolicy] = []
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda policy, **_kwargs: fallback_calls.append(policy) or FakeProviderGateway(),
    )
    data = fallback_provider_call_input(organization, team, project, primary_policy)

    with pytest.raises(ModelPolicyError) as exc_info:
        call_with_fallback(resolved, primary_gateway, data)

    assert exc_info.value is error
    assert fallback_calls == []
    assert not AuditEvent.objects.filter(event_type='ProviderFallbackUsed').exists()


@pytest.mark.django_db
def test_call_with_fallback_returns_primary_result_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_fallback_scope()
    primary_policy = create_fallback_policy(organization, team, project, task_type='curation', fallback_enabled=True)
    resolved = ResolvedModelPolicy(policy=primary_policy)
    primary_result = fallback_provider_call_result(primary_policy)
    primary_gateway = _StubGateway(result=primary_result)
    fallback_calls: list[ModelPolicy] = []
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda policy, **_kwargs: fallback_calls.append(policy) or FakeProviderGateway(),
    )
    data = fallback_provider_call_input(organization, team, project, primary_policy)

    result, used_resolved = call_with_fallback(resolved, primary_gateway, data)

    assert result is primary_result
    assert used_resolved is resolved
    assert fallback_calls == []
    assert not AuditEvent.objects.filter(event_type='ProviderFallbackUsed').exists()


def _visibility_scope(team_ids: tuple[uuid.UUID, ...]) -> EffectiveScope:
    return EffectiveScope(
        organization_id=uuid.uuid4(),
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=team_ids,
        capabilities=(),
        actor_type='api_key',
        actor_id='svc-visibility-test',
        project_bound=False,
    )


def test_ensure_memory_visibility_scope_admits_project() -> None:
    scope = _visibility_scope(())
    memory = Memory(visibility_scope=VisibilityScope.PROJECT, team_id=None)

    ensure_memory_visibility_scope(memory, scope)


def test_ensure_memory_visibility_scope_admits_authorized_team() -> None:
    team_id = uuid.uuid4()
    scope = _visibility_scope((team_id,))
    memory = Memory(visibility_scope=VisibilityScope.TEAM, team_id=team_id)

    ensure_memory_visibility_scope(memory, scope)


def test_ensure_memory_visibility_scope_denies_foreign_team() -> None:
    scope = _visibility_scope((uuid.uuid4(),))
    memory = Memory(visibility_scope=VisibilityScope.TEAM, team_id=uuid.uuid4())

    with pytest.raises(AccessDeniedError) as excinfo:
        ensure_memory_visibility_scope(memory, scope)

    assert excinfo.value.code == 'team_scope_denied'


def test_ensure_memory_visibility_scope_denies_null_team() -> None:
    scope = _visibility_scope((uuid.uuid4(),))
    memory = Memory(visibility_scope=VisibilityScope.TEAM, team_id=None)

    with pytest.raises(AccessDeniedError) as excinfo:
        ensure_memory_visibility_scope(memory, scope)

    assert excinfo.value.code == 'team_scope_denied'


def test_ensure_memory_visibility_scope_denies_session_and_organization() -> None:
    scope = _visibility_scope(())
    for visibility in (VisibilityScope.SESSION, VisibilityScope.ORGANIZATION):
        memory = Memory(visibility_scope=visibility, team_id=None)
        with pytest.raises(AccessDeniedError) as excinfo:
            ensure_memory_visibility_scope(memory, scope)

        assert excinfo.value.code == 'team_scope_denied'
