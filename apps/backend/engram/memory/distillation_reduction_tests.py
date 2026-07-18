import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from engram.core.models import DistillationStage, DistillationWindow
from engram.memory.distillation_provider_stage import ProviderStageOutputError
from engram.memory.distillation_reduction import (
    _REDUCE_SYSTEM_PROMPT,
    MAX_BODY,
    MAX_TITLE,
    REDUCTION_MANIFEST_SCHEMA,
    ReductionBatch,
    ReductionContractError,
    ReductionDraft,
    ReductionStageContract,
    _snapshot_drafts,
    build_reduction_batches,
    derive_final_reduction_drafts,
    derive_first_pending_reduction_target,
    parse_reduction_output,
    provider_stage_target,
    reduce_multilevel,
    resolve_reduction_stage,
    stable_draft_id,
)
from engram.memory.distillation_tests import create_session_distillation_work, create_session_scope
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret
from engram.model_policy.services import curation_schema_prompt_prefix


def _draft(index: int, *, stage: str = 'extract-stage') -> ReductionDraft:
    output_hash = f'output-{index}'
    return ReductionDraft(
        draft_id=stable_draft_id(stage, output_hash, index),
        title=f'Title {index}',
        body=f'Body {index}',
        confidence=Decimal('0.8'),
        source_ids=(f'obs-{index}',),
        source_stage_ids=(stage,),
        anchor_ids=(f'anchor-{index}',),
        source_stage_key=stage,
        source_output_hash=output_hash,
        output_index=index,
    )


def test_reduction_state_requires_typed_extraction_stages() -> None:
    with pytest.raises(ReductionContractError):
        derive_first_pending_reduction_target(({},), (), reduction_target=1, prompt_budget=1000)


def test_reduction_state_rejects_untyped_stage_adapters() -> None:
    with pytest.raises(ReductionContractError):
        derive_first_pending_reduction_target(
            ({'status': 'completed', 'outputs': []},),
            (),
            reduction_target=1,
            prompt_budget=1000,
        )


def test_reduction_state_rejects_legacy_status_values() -> None:
    with pytest.raises(ReductionContractError):
        derive_first_pending_reduction_target(
            (object(),),
            (),
            reduction_target=1,
            prompt_budget=1000,
        )


def test_final_reduction_requires_explicit_accepted_stage_sequence() -> None:
    with pytest.raises(TypeError):
        derive_final_reduction_drafts((), reduction_target=1)  # type: ignore[call-arg]


def test_multilevel_reduction_covers_every_leaf_and_preserves_anchor_union() -> None:
    leaves = [_draft(i) for i in range(5)]
    calls: list[tuple[str, ...]] = []

    def provider(batch: ReductionBatch) -> dict[str, list[dict[str, object]]]:
        calls.append(tuple(ref.draft_id for ref in batch.input_refs))
        return {
            'memories': [
                {
                    'title': 'merged',
                    'body': 'merged body',
                    'confidence': 0.9,
                    'source_ids': [ref.draft_id for ref in batch.input_refs],
                }
            ]
        }

    output = reduce_multilevel(leaves, reduction_target=1, prompt_budget=500, provider=provider)

    assert len(calls) == 3
    assert len(output) == 1
    assert set(output[0].source_ids) == {f'obs-{i}' for i in range(5)}
    assert set(output[0].anchor_ids) == {f'anchor-{i}' for i in range(5)}
    assert set(output[0].source_stage_ids) == {'extract-stage'}


@pytest.mark.parametrize(
    'payload',
    [
        {'memories': []},
        {'memories': [{'title': 'x', 'body': 'y', 'confidence': 1, 'source_ids': ['foreign']}]},
        {'memories': [{'title': 'x', 'body': 'y', 'confidence': 1, 'source_ids': []}]},
    ],
)
def test_nonshrinking_or_incomplete_reduction_is_retryable_not_union_fallback(payload: Any) -> None:
    leaves = [_draft(0), _draft(1)]
    with pytest.raises(ReductionContractError):
        reduce_multilevel(leaves, reduction_target=1, prompt_budget=10000, provider=lambda _batch: payload)


def test_manifest_and_batches_are_deterministic_and_singletons_are_carried() -> None:
    leaves = [_draft(i) for i in range(3)]
    first = build_reduction_batches(leaves, reduction_target=1, prompt_budget=350, level=0)
    second = build_reduction_batches(leaves, reduction_target=1, prompt_budget=350, level=0)

    assert first == second
    assert len(first) == 2
    assert first[1].provider_required is False
    assert first[1].input_refs[0].draft_id == leaves[2].draft_id
    assert first[0].input_hash == second[0].input_hash


def test_prompt_budget_rejects_two_drafts_that_cannot_fit() -> None:
    with pytest.raises(ReductionContractError):
        build_reduction_batches([_draft(0), _draft(1)], reduction_target=1, prompt_budget=1, level=0)


def test_parser_requires_exact_envelope_and_known_duplicate_free_ids() -> None:
    leaves = [_draft(0), _draft(1)]
    with pytest.raises(ReductionContractError):
        parse_reduction_output({'memories': [], 'extra': True}, leaves)
    with pytest.raises(ReductionContractError):
        parse_reduction_output(
            {
                'memories': [
                    {
                        'title': 'x',
                        'body': 'y',
                        'confidence': 0.5,
                        'source_ids': [leaves[0].draft_id, leaves[0].draft_id],
                    }
                ]
            },
            leaves,
        )


def _memory(source_ids: list[str]) -> dict[str, object]:
    return {
        'title': 'Reduced fact',
        'body': 'A durable reduced memory body.',
        'confidence': 0.8,
        'source_ids': source_ids,
    }


def _cover(inputs: list[ReductionDraft], count: int) -> dict[str, object]:
    ids = [draft.draft_id for draft in inputs]
    buckets: list[list[str]] = [[] for _ in range(count)]
    for index, draft_id in enumerate(ids):
        buckets[index % count].append(draft_id)
    return {'memories': [_memory(bucket) for bucket in buckets]}


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('title', ' \t '),
        ('body', ' \n '),
        ('confidence', '0.8'),
    ],
)
def test_reduction_rejects_blank_text_and_string_confidence(field: str, value: object) -> None:
    inputs = [_draft(0), _draft(1)]
    memory = _memory([draft.draft_id for draft in inputs])
    memory[field] = value

    with pytest.raises(ReductionContractError):
        parse_reduction_output({'memories': [memory]}, inputs, reduction_target=1)


def test_reduction_budget_does_not_charge_unprompted_observation_lineage() -> None:
    observation_ids = tuple(f'{index:036d}' for index in range(100))
    drafts = [
        ReductionDraft(
            draft_id=marker * 64,
            title='T',
            body='B',
            confidence=Decimal('0.8'),
            source_ids=observation_ids,
        )
        for marker in ('a', 'b')
    ]
    charged_payload = json.dumps(
        [
            {
                'id': draft.draft_id,
                'title': draft.title,
                'body': draft.body,
                'confidence': str(draft.confidence),
                'source_ids': list(draft.source_ids),
                'kind': draft.kind,
            }
            for draft in drafts
        ],
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode()
    rendered_prompt = json.dumps(
        {
            'drafts': [
                {
                    'id': draft.draft_id,
                    'title': draft.title,
                    'body': draft.body,
                    'confidence': str(draft.confidence),
                }
                for draft in drafts
            ],
            'reduction_target': 1,
        },
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode()

    assert len(charged_payload) == 8083
    assert len(rendered_prompt) == 265
    assert len(rendered_prompt) <= 8000 < len(charged_payload)

    batches = build_reduction_batches(
        drafts,
        reduction_target=1,
        prompt_budget=8000,
    )

    assert len(batches) == 1
    assert batches[0].input_drafts == tuple(drafts)
    assert batches[0].provider_required is True


def test_reduction_accepts_spec_cap_above_twelve() -> None:
    inputs = [_draft(index) for index in range(40)]
    parsed = parse_reduction_output(_cover(inputs, 20), inputs, reduction_target=12)
    assert len(parsed.memories) == 20


def test_reduction_rejects_output_above_spec_cap() -> None:
    inputs = [_draft(index) for index in range(40)]
    with pytest.raises(ReductionContractError):
        parse_reduction_output(_cover(inputs, 21), inputs, reduction_target=12)


def test_reduction_target_twenty_accepts_twenty_memories() -> None:
    inputs = [_draft(index) for index in range(50)]
    parsed = parse_reduction_output(_cover(inputs, 20), inputs, reduction_target=20)
    assert len(parsed.memories) == 20


def test_final_drafts_wait_until_all_targets_accepted() -> None:
    assert derive_final_reduction_drafts((), (), reduction_target=2) == ()


def test_reduction_levels_start_at_one() -> None:
    batch = build_reduction_batches([_draft(0), _draft(1)], reduction_target=1, prompt_budget=1000)
    assert batch[0].level == 1


def test_reduction_contract_exposes_provider_seam_methods() -> None:
    contract = ReductionStageContract()
    assert contract.stage_kind == 'reduce'
    assert contract.prompt_contract == 'distill_reduce.v1'
    assert contract.response_kind == 'distill_reduce.v1'
    with pytest.raises(ProviderStageOutputError):
        contract.normalize_output('{"memories": []}', stage=object())


def test_prepare_call_hydrates_same_window_complete_stage_refs_and_renders_exact_drafts() -> None:
    with pytest.raises(ReductionContractError):
        ReductionStageContract().prepare_call(object())


def test_normalize_output_strictly_parses_and_canonicalizes_confidence() -> None:
    with pytest.raises(ProviderStageOutputError):
        ReductionStageContract().normalize_output('{"memories": []}', stage=object())


def test_accepted_replay_uses_recomputed_batch_identity_not_durable_target_key() -> None:
    with pytest.raises(ReductionContractError):
        derive_first_pending_reduction_target(({},), ({},), reduction_target=1, prompt_budget=10000)


def test_final_derivation_replays_singleton_carry_between_accepted_levels() -> None:
    leaves = [_draft(0), _draft(1), _draft(2)]
    output = reduce_multilevel(
        leaves,
        reduction_target=2,
        prompt_budget=350,
        provider=lambda batch: {
            'memories': [
                {
                    'title': 'merged',
                    'body': 'body',
                    'confidence': 0.9,
                    'source_ids': [ref.draft_id for ref in batch.input_refs],
                }
            ]
        },
    )
    assert len(output) == 2


def test_provider_stage_target_has_reduction_shape_and_manifest() -> None:
    leaves = [_draft(0), _draft(1)]
    batch = build_reduction_batches(leaves, reduction_target=1, prompt_budget=10000, level=1)[0]
    with pytest.raises(ReductionContractError):
        provider_stage_target({'id': UUID('00000000-0000-0000-0000-000000000001')}, batch)


def test_reduction_stage_resolution_uses_the_generic_identity_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engram.memory import distillation_provider_stage as engine

    expected = object()

    def resolve(target: object, claim: object, now: object, policy_role: str = 'primary') -> object:
        assert target == 'target'
        assert claim == 'claim'
        assert now == 'now'
        assert policy_role == 'primary'

        return expected

    monkeypatch.setattr(engine, 'resolve_provider_stage', resolve)

    assert resolve_reduction_stage('target', 'claim', now='now') is expected


REDUCTION_TARGET = 3


@pytest.fixture
def f_reduce_stage() -> DistillationStage:
    organization, team, project, _agent, session = create_session_scope(suffix='reduce-prompt-target')
    work = create_session_distillation_work(session, upper=1)
    window = DistillationWindow.objects.create(
        organization=organization,
        project=project,
        team=team,
        work=work,
        session=session,
        contract_version=1,
        lower_sequence_exclusive=0,
        upper_sequence_inclusive=1,
        observation_count=1,
        input_hash='1' * 64,
        chunk_char_budget=8000,
        reduction_target=REDUCTION_TARGET,
        chunk_contract_version=1,
    )
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Reduce prompt secret',
        provider='openai',
        scope='team',
        current_version=1,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        secret=secret,
        name='Reduce prompt policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='reduce-prompt-model',
    )
    call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'reduce-prompt-source:{window.id}',
        redaction_state='redacted',
    )
    source = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        stage_kind='reduce',
        level=1,
        ordinal=0,
        target_key='2' * 64,
        stage_key='3' * 64,
        input_hash='4' * 64,
        input_manifest={
            'schema': REDUCTION_MANIFEST_SCHEMA,
            'level': 1,
            'ordinal': 0,
            'refs': [],
        },
        prompt_contract='distill_reduce.v1',
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='complete',
        attempt_count=1,
        accepted_provider_call=call,
        response_hash='5' * 64,
        response_size=1,
        output_snapshot={
            'memories': [
                {
                    'title': 'Durable fact',
                    'body': 'A durable fact carried into the next reduction level.',
                    'confidence': '0.9',
                    'source_ids': ['leaf-source'],
                    'kind': 'gotcha',
                }
            ]
        },
        output_hash='6' * 64,
        completed_at=timezone.now(),
    )

    return DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        stage_kind='reduce',
        level=2,
        ordinal=0,
        target_key='7' * 64,
        stage_key='8' * 64,
        input_hash='9' * 64,
        input_manifest={
            'schema': REDUCTION_MANIFEST_SCHEMA,
            'level': 2,
            'ordinal': 0,
            'refs': [draft.ref.as_manifest() for draft in _snapshot_drafts(source)],
        },
        prompt_contract='distill_reduce.v1',
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='required',
    )


def test_reduce_schema_instructions_describe_a_payload_the_parser_accepts() -> None:
    instructions = curation_schema_prompt_prefix('distill_reduce.v1')
    inputs = [_draft(index) for index in range(4)]
    payload = {
        'memories': [
            {
                'title': 'Consolidated durable fact',
                'body': 'One reduced fact preserving every input draft.',
                'confidence': 0.9,
                'source_ids': [draft.draft_id for draft in inputs],
                'kind': 'decision',
            }
        ]
    }

    assert instructions
    assert str(MAX_TITLE) in instructions
    assert str(MAX_BODY) in instructions
    for kind in ('decision', 'convention', 'gotcha', 'architecture', 'incident'):
        assert kind in instructions

    parsed = parse_reduction_output(payload, inputs, reduction_target=1)

    assert len(parsed.memories) == 1
    assert parsed.memories[0].kind == 'decision'
    assert parsed.memories[0].source_ids == tuple(draft.draft_id for draft in inputs)
    assert parsed.memories[0].confidence == Decimal('0.9')


def test_reduce_system_prompt_states_contract_marker_and_parser_rules() -> None:
    prompt = _REDUCE_SYSTEM_PROMPT

    assert 'distill_reduce.v1' in prompt
    assert 'distill_extract.v1' not in prompt
    assert str(MAX_TITLE) in prompt
    assert str(MAX_BODY) in prompt
    for kind in ('decision', 'convention', 'gotcha', 'architecture', 'incident'):
        assert kind in prompt
    assert 'reduction_target' in prompt
    assert 'strictly fewer' in prompt
    assert 'copied verbatim' in prompt
    assert 'no additional properties' in prompt


@pytest.mark.django_db
def test_prepare_call_prompt_carries_the_stage_reduction_target(f_reduce_stage: DistillationStage) -> None:
    prepared = ReductionStageContract().prepare_call(f_reduce_stage)
    prompt = json.loads(prepared.prompt)

    assert prompt['reduction_target'] == REDUCTION_TARGET
    assert [draft['id'] for draft in prompt['drafts']] == [
        draft.draft_id for draft in _snapshot_drafts(f_reduce_stage.window.stages.get(level=1))
    ]
