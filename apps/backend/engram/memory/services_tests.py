from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection

from engram.core.models import AuditEvent, MemoryCandidate, Observation, Organization, Project, RetrievalDocument, Team
from engram.memory.memory_worker_tests import (
    create_embedding_policy,
    create_generation_policy,
    create_memory_candidate,
    create_observation_recorded_scope,
    execute_worker,
)
from engram.memory.services import (
    MemoryCandidateWorkerInput,
    ProcessObservationRecorded,
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
    call_with_fallback,
    derive_observation_confidence,
    distillation_system_prompt,
    provider_prompt,
    strip_json_fence,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    FakeProviderGateway,
    ModelPolicyError,
    ProviderCallInput,
    ProviderCallResult,
    ResolvedModelPolicy,
)


class _ObservationStub:
    def __init__(
        self,
        *,
        observation_type: str,
        facts: list,
        files_read: list,
        files_modified: list,
        narrative: str,
        concepts: list,
    ) -> None:
        self.observation_type = observation_type
        self.facts = facts
        self.files_read = files_read
        self.files_modified = files_modified
        self.narrative = narrative
        self.concepts = concepts


def _thin() -> _ObservationStub:
    return _ObservationStub(
        observation_type='tool_use',
        facts=[],
        files_read=[],
        files_modified=[],
        narrative='',
        concepts=[],
    )


def test_derive_observation_confidence_thin_returns_base() -> None:
    obs = _thin()

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.500')


def test_derive_observation_confidence_rich_all_bonuses_returns_point_nine_five() -> None:
    obs = _ObservationStub(
        observation_type='decision',
        facts=['use postgres'],
        files_read=['schema.sql'],
        files_modified=[],
        narrative='We decided to use postgres for reliability.',
        concepts=['database'],
    )

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.950')


def test_derive_observation_confidence_facts_only_adds_point_one() -> None:
    obs = _thin()
    obs.facts = ['a fact']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_read_only_adds_point_one() -> None:
    obs = _thin()
    obs.files_read = ['a.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_modified_only_adds_point_one() -> None:
    obs = _thin()
    obs.files_modified = ['b.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_files_read_and_modified_counts_as_single_bonus() -> None:
    obs = _thin()
    obs.files_read = ['a.py']
    obs.files_modified = ['b.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_narrative_whitespace_only_no_bonus() -> None:
    obs = _thin()
    obs.narrative = '   \n  '

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.500')


def test_derive_observation_confidence_narrative_nonempty_adds_point_one() -> None:
    obs = _thin()
    obs.narrative = 'We chose this approach.'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_concepts_nonempty_adds_point_zero_five() -> None:
    obs = _thin()
    obs.concepts = ['reliability']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.550')


def test_derive_observation_confidence_durable_type_decision_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'decision'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_architecture_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'architecture'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_convention_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'convention'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_durable_type_gotcha_adds_point_one() -> None:
    obs = _thin()
    obs.observation_type = 'gotcha'

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.600')


def test_derive_observation_confidence_non_durable_types_no_bonus() -> None:
    for obs_type in ('tool_use', 'session_summary', 'error', 'unknown'):
        obs = _thin()
        obs.observation_type = obs_type

        result = derive_observation_confidence(obs)  # type: ignore[arg-type]

        assert result == Decimal('0.500'), f'expected 0.500 for type {obs_type!r}'


def test_derive_observation_confidence_result_quantized_to_three_decimals() -> None:
    obs = _thin()

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == result.quantize(Decimal('0.001'))


def test_derive_observation_confidence_clamps_to_one() -> None:
    obs = _ObservationStub(
        observation_type='decision',
        facts=['f'],
        files_read=['a.py'],
        files_modified=[],
        narrative='text',
        concepts=['c'],
    )

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert Decimal('0') <= result <= Decimal('1')


def test_derive_observation_confidence_facts_plus_files_produces_point_seven() -> None:
    obs = _thin()
    obs.facts = ['a fact']
    obs.files_read = ['a.py']

    result = derive_observation_confidence(obs)  # type: ignore[arg-type]

    assert result == Decimal('0.700')


@pytest.mark.django_db
def test_promote_memory_candidate_existing_result_skips_reindex_for_stale_memory() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)
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
    candidate = create_memory_candidate(observation)
    candidate.kind = 'gotcha'
    candidate.save(update_fields=['kind', 'updated_at'])

    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert result.memory.metadata['kind'] == 'gotcha'
    assert result.memory.kind == 'gotcha'


@pytest.mark.django_db
def test_promote_memory_candidate_omits_kind_from_memory_metadata_when_unset() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    candidate = create_memory_candidate(observation)

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
def test_promote_memory_candidate_index_embed_has_no_open_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_embedding_policy(organization, team, project)
    candidate = create_memory_candidate(observation)
    observed_in_atomic: list[bool] = []
    real_gateway = FakeProviderGateway()

    class _RecordingGateway(FakeProviderGateway):
        def embed(self, data: object) -> object:
            observed_in_atomic.append(connection.in_atomic_block)

            return real_gateway.embed(data)

    monkeypatch.setattr('engram.context.services.get_provider_gateway', lambda *_, **__: _RecordingGateway())

    PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    assert observed_in_atomic == [False]


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
                generated_title='SKIP',
                generated_body='',
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
                generated_title='SKIP',
                generated_body='',
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


class _TitleBodyGateway(FakeProviderGateway):
    def __init__(self, *, title: str, body: str) -> None:
        self._title = title
        self._body = body

    def call(self, data: object) -> ProviderCallResult:
        real = FakeProviderGateway.call(self, data)

        return ProviderCallResult(
            provider=real.provider,
            model=real.model,
            call_record_id=real.call_record_id,
            redaction_state=real.redaction_state,
            generated_title=self._title,
            generated_body=self._body,
        )


@pytest.mark.django_db
def test_process_observation_empty_body_falls_back_to_title(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='Config lives in settings.py', body=''),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidates = MemoryCandidate.objects.filter(source_observation=observation)
    assert candidates.count() == 1
    candidate = candidates.get()
    assert candidate.title == 'Config lives in settings.py'
    assert candidate.body == 'Config lives in settings.py'


@pytest.mark.django_db
def test_process_observation_empty_title_falls_back_to_body(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='', body='Uses pgvector cosine.'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.title == 'Uses pgvector cosine.'
    assert candidate.body == 'Uses pgvector cosine.'


@pytest.mark.django_db
def test_process_observation_both_title_and_body_empty_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='', body=''),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()
    assert AuditEvent.objects.filter(
        event_type='MemoryCandidateSkipped',
        target_id=str(observation.id),
    ).exists()


@pytest.mark.django_db
def test_process_observation_skip_title_with_empty_body_still_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='SKIP', body=''),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is True
    assert result.candidate is None
    assert not MemoryCandidate.objects.filter(source_observation=observation).exists()


@pytest.mark.django_db
def test_process_observation_normal_title_and_body_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='Config lives in settings.py', body='Uses pgvector cosine.'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.title == 'Config lives in settings.py'
    assert candidate.body == 'Uses pgvector cosine.'


@pytest.mark.django_db
def test_process_observation_durable_type_sets_candidate_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    observation.observation_type = 'gotcha'
    observation.save(update_fields=['observation_type', 'updated_at'])
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='Config lives in settings.py', body='Uses pgvector cosine.'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.kind == 'gotcha'


@pytest.mark.django_db
def test_process_observation_non_durable_type_leaves_candidate_kind_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _session, _raw_event, observation = create_observation_recorded_scope()
    observation.observation_type = 'other'
    observation.save(update_fields=['observation_type', 'updated_at'])
    create_generation_policy(organization, team, project)
    monkeypatch.setattr(
        'engram.memory.services.get_provider_gateway',
        lambda *_, **__: _TitleBodyGateway(title='Config lives in settings.py', body='Uses pgvector cosine.'),
    )

    result = ProcessObservationRecorded().execute(MemoryCandidateWorkerInput(observation_id=observation.id))

    assert result.skipped is False
    candidate = MemoryCandidate.objects.get(source_observation=observation)
    assert candidate.kind == ''


def test_strip_json_fence_strips_json_tagged_fence() -> None:
    fenced = '```json\n{"memories": []}\n```'

    assert strip_json_fence(fenced) == '{"memories": []}'


def test_strip_json_fence_strips_bare_fence() -> None:
    fenced = '```\n{"memories": []}\n```'

    assert strip_json_fence(fenced) == '{"memories": []}'


def test_strip_json_fence_strips_uppercase_json_tag() -> None:
    fenced = '```JSON\n{"memories": []}\n```'

    assert strip_json_fence(fenced) == '{"memories": []}'


def test_strip_json_fence_tolerates_trailing_whitespace_and_newlines() -> None:
    fenced = '```json\n{"memories": []}\n```\n\n   '

    assert strip_json_fence(fenced) == '{"memories": []}'


def test_strip_json_fence_returns_unfenced_json_unchanged() -> None:
    unfenced = '{"memories": []}'

    assert strip_json_fence(unfenced) == unfenced


def test_strip_json_fence_returns_non_fence_text_unchanged() -> None:
    text = 'not json at all'

    assert strip_json_fence(text) == text


def test_strip_json_fence_returns_non_str_input_unchanged() -> None:
    assert strip_json_fence(None) is None  # type: ignore[arg-type]


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
