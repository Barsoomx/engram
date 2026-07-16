from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from engram.memory.distillation_provider_stage import ProviderStageOutputError
from engram.memory.distillation_reduction import (
    ReductionBatch,
    ReductionContractError,
    ReductionDraft,
    ReductionStageContract,
    build_reduction_batches,
    derive_final_reduction_drafts,
    derive_first_pending_reduction_target,
    parse_reduction_output,
    provider_stage_target,
    reduce_multilevel,
    resolve_reduction_stage,
    stable_draft_id,
)


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
