import json
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


def test_reduction_waits_for_complete_extraction_coverage() -> None:
    extraction = [
        {'target_key': 'a', 'status': 'completed', 'output_hash': 'ha', 'outputs': []},
        {'target_key': 'b', 'status': 'required', 'output_hash': None, 'outputs': []},
    ]

    assert derive_first_pending_reduction_target(extraction, (), reduction_target=1, prompt_budget=1000) is None


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


def test_final_drafts_wait_until_all_targets_accepted() -> None:
    assert derive_final_reduction_drafts([], reduction_target=2) == ()
    drafts = [_draft(0), _draft(1)]
    accepted = [{'level': 0, 'status': 'completed', 'drafts': drafts}]
    assert derive_final_reduction_drafts(accepted, reduction_target=2) == tuple(drafts)


def test_reduction_levels_start_at_one_and_snapshot_adapter_reads_memories() -> None:
    draft = _draft(0)
    target = {
        'target_key': 'extract',
        'status': 'completed',
        'output_hash': 'hash',
        'output_snapshot': {
            'memories': [
                {
                    'title': draft.title,
                    'body': draft.body,
                    'confidence': 0.8,
                    'source_ids': list(draft.source_ids),
                    'draft_id': draft.draft_id,
                    'source_stage_key': draft.source_stage_key,
                    'source_output_hash': draft.source_output_hash,
                }
            ]
        },
    }
    assert derive_first_pending_reduction_target([target], (), reduction_target=1, prompt_budget=1000) is None
    batch = build_reduction_batches([draft, _draft(1)], reduction_target=1, prompt_budget=1000)
    assert batch[0].level == 1


def test_reduction_contract_exposes_provider_seam_methods() -> None:
    contract = ReductionStageContract()
    assert contract.stage_kind == 'reduce'
    assert contract.prompt_contract == 'distill_reduce.v1'
    assert contract.response_kind == 'distill_reduce.v1'
    with pytest.raises(ProviderStageOutputError):
        contract.normalize_output('{"memories": []}', stage=object())


def test_prepare_call_hydrates_same_window_complete_stage_refs_and_renders_exact_drafts() -> None:
    leaf = _draft(0)
    source_stage = {
        'stage_key': 'extract-stage',
        'status': 'complete',
        'window_id': 'window-1',
        'output_hash': leaf.source_output_hash,
        'output_snapshot': {
            'memories': [
                {
                    'draft_id': leaf.draft_id,
                    'title': leaf.title,
                    'body': leaf.body,
                    'confidence': '0.8',
                    'supporting_observation_ids': list(leaf.source_ids),
                    'source_stage_key': leaf.source_stage_key,
                    'source_output_hash': leaf.source_output_hash,
                    'output_index': leaf.output_index,
                }
            ]
        },
    }
    stage = {
        'stage_key': 'reduce-stage',
        'window_id': 'window-1',
        'organization_id': 'org',
        'project_id': 'project',
        'team_id': None,
        'input_manifest': {'refs': [leaf.ref.as_manifest()]},
        'window': {'reduction_target': 1, 'stages': [source_stage]},
    }
    call = ReductionStageContract().prepare_call(stage)
    assert call.response_kind == 'distill_reduce.v1'
    assert call.prompt == json.dumps(
        {'drafts': [{'id': leaf.draft_id, 'title': leaf.title, 'body': leaf.body, 'confidence': '0.8'}]},
        ensure_ascii=False,
        separators=(',', ':'),
    )


def test_normalize_output_strictly_parses_and_canonicalizes_confidence() -> None:
    leaf = _draft(0)
    source_stage = {
        'stage_key': 'extract-stage',
        'status': 'complete',
        'window_id': 'window-1',
        'output_hash': leaf.source_output_hash,
        'output_snapshot': {
            'memories': [
                {
                    'draft_id': leaf.draft_id,
                    'title': leaf.title,
                    'body': leaf.body,
                    'confidence': '0.8',
                    'supporting_observation_ids': list(leaf.source_ids),
                    'source_stage_key': leaf.source_stage_key,
                    'source_output_hash': leaf.source_output_hash,
                    'output_index': leaf.output_index,
                }
            ]
        },
    }
    stage = {
        'window_id': 'window-1',
        'input_manifest': {'refs': [leaf.ref.as_manifest()]},
        'window': {'reduction_target': 1, 'stages': [source_stage]},
    }
    normalized = ReductionStageContract().normalize_output(
        f'{{"memories":[{{"title":"merged","body":"body","confidence":0.9,"source_ids":["{leaf.draft_id}"]}}]}}',
        stage=stage,
    )
    assert normalized == {
        'memories': [{'title': 'merged', 'body': 'body', 'confidence': '0.9', 'source_ids': [leaf.draft_id]}],
    }


def test_accepted_replay_uses_recomputed_batch_identity_not_durable_target_key() -> None:
    leaves = [_draft(0), _draft(1)]
    batch = build_reduction_batches(leaves, reduction_target=1, prompt_budget=10000, level=1)[0]
    accepted = [
        {
            'level': batch.level,
            'ordinal': batch.ordinal,
            'input_hash': batch.input_hash,
            'target_key': 'durable-engine-stage-key',
            'status': 'complete',
            'output_snapshot': {
                'memories': [
                    {
                        'draft_id': 'accepted',
                        'title': 'merged',
                        'body': 'body',
                        'confidence': 0.9,
                        'source_ids': [leaves[0].draft_id, leaves[1].draft_id],
                    }
                ]
            },
        }
    ]
    assert (
        derive_first_pending_reduction_target(
            [{'status': 'complete', 'outputs': [leaf]} for leaf in leaves],
            accepted,
            reduction_target=1,
            prompt_budget=10000,
        )
        is None
    )


def test_final_derivation_replays_singleton_carry_between_accepted_levels() -> None:
    leaves = [_draft(0), _draft(1), _draft(2)]
    batches = build_reduction_batches(leaves, reduction_target=1, prompt_budget=350, level=1)
    first = batches[0]
    accepted_first = {
        'level': first.level,
        'ordinal': first.ordinal,
        'input_hash': first.input_hash,
        'status': 'complete',
        'output_snapshot': {
            'memories': [
                {
                    'title': 'merged',
                    'body': 'body',
                    'confidence': 0.9,
                    'source_ids': [leaves[0].draft_id, leaves[1].draft_id],
                }
            ]
        },
    }
    carried = leaves[2]
    final = derive_final_reduction_drafts(
        [{'status': 'complete', 'outputs': [leaf]} for leaf in leaves],
        [accepted_first],
        reduction_target=2,
        prompt_budget=350,
    )
    assert len(final) == 2
    assert any(draft.source_ids == ('obs-2',) for draft in final)
    assert any(set(draft.source_ids) == {'obs-0', 'obs-1'} for draft in final)
    assert carried.source_ids == ('obs-2',)


def test_provider_stage_target_has_reduction_shape_and_manifest() -> None:
    leaves = [_draft(0), _draft(1)]
    batch = build_reduction_batches(leaves, reduction_target=1, prompt_budget=10000, level=1)[0]
    target = provider_stage_target({'id': UUID('00000000-0000-0000-0000-000000000001')}, batch)
    assert target.level == 1
    assert target.chunk_id is None
    assert target.input_hash == batch.input_hash
    assert target.input_manifest == batch.manifest
    assert target.prompt_contract == 'distill_reduce.v1'


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
