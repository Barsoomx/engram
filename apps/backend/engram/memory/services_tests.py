from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.db import connection

from engram.core.models import AuditEvent, MemoryCandidate, Observation, RetrievalDocument
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
    derive_observation_confidence,
    distillation_system_prompt,
    provider_prompt,
    strip_json_fence,
)
from engram.model_policy.services import FakeProviderGateway, ProviderCallResult


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


def test_strip_json_fence_removes_json_language_tagged_fence() -> None:
    raw = '```json\n{"a": 1}\n```'

    assert json.loads(strip_json_fence(raw)) == {'a': 1}


def test_strip_json_fence_removes_bare_fence() -> None:
    raw = '```\n{"a": 1}\n```'

    assert json.loads(strip_json_fence(raw)) == {'a': 1}


def test_strip_json_fence_returns_plain_json_unchanged() -> None:
    raw = '{"a": 1}'

    assert strip_json_fence(raw) == raw
    assert json.loads(strip_json_fence(raw)) == {'a': 1}


def test_strip_json_fence_handles_surrounding_whitespace() -> None:
    raw = '  \n```json\n{"a": 1}\n```\n  '

    assert json.loads(strip_json_fence(raw)) == {'a': 1}


def test_strip_json_fence_ignores_mid_text_backticks_without_leading_fence() -> None:
    raw = 'here is some ``` inline text that is not a fence'

    assert strip_json_fence(raw) == raw
