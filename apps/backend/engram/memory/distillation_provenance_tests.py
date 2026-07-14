from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from engram.memory.distillation_provenance import (
    ProvenanceContractError,
    build_finalization_plan,
    candidate_source_anchors,
    canonical_source_manifest,
)
from engram.memory.workflow_work import canonical_json_bytes

_DIGEST = 'a' * 64


def _observation(observation_id: str, sequence: int, *, files: list[str] | None = None) -> dict[str, object]:
    return {
        'observation_id': observation_id,
        'session_sequence': sequence,
        'observation_digest': _DIGEST,
        'organization_id': 'org',
        'project_id': 'project',
        'team_id': None,
        'files_read': files or [],
        'files_modified': [],
        'source_metadata': {
            'symbols': ['module.fn', 'module.fn'],
            'commands': ['pytest -q', 'pytest -q'],
            'errors': ['E123'],
            'commits': ['abc123'],
        },
    }


def _extract_stage(
    *,
    stage_key: str,
    target_key: str,
    output_hash: str,
    outputs: list[dict[str, object]],
    no_signal: list[str] = None,
) -> dict[str, object]:
    return {
        'stage_key': stage_key,
        'target_key': target_key,
        'status': 'complete',
        'output_hash': output_hash,
        'output_snapshot': {
            'memories': outputs,
            'no_signal_observation_ids': no_signal or [],
        },
    }


def test_anchor_snapshot_is_redacted_persisted_and_stable() -> None:
    observation = _observation('obs-1', 3, files=['b.py', 'a.py', 'a.py'])

    first = candidate_source_anchors(observation)
    second = candidate_source_anchors({**observation, 'files_read': ['a.py', 'b.py']})

    assert first == second
    assert first == {
        'schema': 'candidate_source_anchors.v1',
        'observation_id': 'obs-1',
        'session_sequence': 3,
        'observation_digest': _DIGEST,
        'file_paths': ['a.py', 'b.py'],
        'symbols': ['module.fn'],
        'commands': ['pytest -q'],
        'errors': ['E123'],
        'commits': ['abc123'],
    }
    assert canonical_source_manifest(first) == canonical_source_manifest(second)


def test_finalization_plan_recursively_unions_support_and_preserves_lineage() -> None:
    observations = [_observation('obs-1', 1, files=['one.py']), _observation('obs-2', 2, files=['two.py'])]
    extraction = [
        _extract_stage(
            stage_key='extract-a',
            target_key='target-a',
            output_hash='b' * 64,
            outputs=[
                {
                    'draft_id': 'draft-a',
                    'title': 'One',
                    'body': 'Body one',
                    'confidence': '0.8',
                    'supporting_observation_ids': ['obs-1'],
                },
            ],
        ),
        _extract_stage(
            stage_key='extract-b',
            target_key='target-b',
            output_hash='c' * 64,
            outputs=[
                {
                    'draft_id': 'draft-b',
                    'title': 'Two',
                    'body': 'Body two',
                    'confidence': '0.9',
                    'supporting_observation_ids': ['obs-2'],
                },
            ],
        ),
    ]
    reduction = [
        {
            'stage_key': 'reduce-1',
            'status': 'complete',
            'output_hash': 'd' * 64,
            'output_snapshot': {
                'memories': [
                    {
                        'draft_id': 'final-draft',
                        'title': 'Merged',
                        'body': 'Merged body',
                        'confidence': '0.95',
                        'source_ids': ['draft-a', 'draft-b'],
                    }
                ],
            },
        }
    ]

    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=observations,
        extraction_stages=extraction,
        reduction_stages=reduction,
        final_stage_key='reduce-1',
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert {source.observation_id for source in candidate.sources} == {'obs-1', 'obs-2'}
    assert {source.deciding_stage_key for source in candidate.sources} == {'reduce-1'}
    assert {item.observation_id for item in plan.coverage} == {'obs-1', 'obs-2'}
    assert {item.outcome for item in plan.coverage} == {'signal'}


def test_shared_signal_uses_first_final_deciding_stage_for_coverage_but_source_keeps_lineage() -> None:
    observations = [_observation('obs-1', 1)]
    extraction = [
        _extract_stage(
            stage_key='extract-a',
            target_key='target-a',
            output_hash='b' * 64,
            outputs=[
                {
                    'draft_id': 'draft-a',
                    'title': 'A',
                    'body': 'A',
                    'confidence': '0.8',
                    'supporting_observation_ids': ['obs-1'],
                }
            ],
        )
    ]
    reduction = [
        {
            'stage_key': 'reduce-1',
            'status': 'complete',
            'output_hash': 'c' * 64,
            'output_snapshot': {
                'memories': [
                    {
                        'draft_id': 'draft-1',
                        'title': 'One',
                        'body': 'One',
                        'confidence': '0.7',
                        'source_ids': ['draft-a'],
                    },
                ]
            },
        },
        {
            'stage_key': 'reduce-2',
            'status': 'complete',
            'output_hash': 'd' * 64,
            'output_snapshot': {
                'memories': [
                    {
                        'draft_id': 'draft-2',
                        'title': 'Two',
                        'body': 'Two',
                        'confidence': '0.6',
                        'source_ids': ['draft-a'],
                    },
                ]
            },
        },
    ]

    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=observations,
        extraction_stages=extraction,
        reduction_stages=reduction,
        final_drafts=[
            {
                'draft_id': 'draft-1',
                'source_stage_key': 'reduce-1',
                'title': 'One',
                'body': 'One',
                'confidence': '0.7',
                'source_ids': ['draft-a'],
            },
            {
                'draft_id': 'draft-2',
                'source_stage_key': 'reduce-2',
                'title': 'Two',
                'body': 'Two',
                'confidence': '0.6',
                'source_ids': ['draft-a'],
            },
        ],
    )

    assert plan.coverage[0].deciding_stage_key == 'reduce-1'
    assert {source.deciding_stage_key for candidate in plan.candidates for source in candidate.sources} == {
        'reduce-1',
        'reduce-2',
    }


def test_plan_rejects_missing_coverage_and_provider_invented_anchor() -> None:
    observation = _observation('obs-1', 1)
    stage = _extract_stage(
        stage_key='extract-a',
        target_key='target-a',
        output_hash='b' * 64,
        outputs=[
            {
                'draft_id': 'draft-a',
                'title': 'A',
                'body': 'A',
                'confidence': '0.8',
                'supporting_observation_ids': ['obs-1'],
                'files': ['forged.py'],
            }
        ],
    )

    with pytest.raises(ProvenanceContractError, match='anchor|coverage'):
        build_finalization_plan(
            scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
            window_input_hash=_DIGEST,
            observations=[observation],
            extraction_stages=[stage],
        )


def test_dataclasses_are_immutable() -> None:
    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=[_observation('obs-1', 1)],
        extraction_stages=[
            _extract_stage(
                stage_key='extract-a',
                target_key='target-a',
                output_hash='b' * 64,
                outputs=[],
                no_signal=['obs-1'],
            )
        ],
    )
    with pytest.raises(FrozenInstanceError):
        plan.intent = 'signal'


def test_final_draft_source_ids_can_be_observation_ids() -> None:
    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=[_observation('obs-1', 1)],
        accepted_stages=[
            _extract_stage(
                stage_key='extract-a',
                target_key='target-a',
                output_hash='b' * 64,
                outputs=[
                    {
                        'draft_id': 'draft-a',
                        'title': 'A',
                        'body': 'A',
                        'confidence': '0.8',
                        'supporting_observation_ids': ['obs-1'],
                    }
                ],
            )
        ],
        final_drafts=[
            {
                'draft_id': 'final-a',
                'source_stage_key': 'extract-a',
                'title': 'A',
                'body': 'A',
                'confidence': '0.8',
                'source_ids': ['obs-1'],
            }
        ],
    )
    assert plan.candidates[0].sources[0].observation_id == 'obs-1'


def test_anchor_hash_uses_cp1_canonical_json_and_uuid_ids_are_stringified() -> None:
    observation = _observation(str(UUID('00000000-0000-0000-0000-000000000001')), 1)
    observation['observation_id'] = UUID('00000000-0000-0000-0000-000000000001')
    anchors = candidate_source_anchors(observation)

    assert anchors['observation_id'] == '00000000-0000-0000-0000-000000000001'
    expected = hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()
    assert canonical_source_manifest(anchors) == expected


def test_missing_draft_ids_use_stable_stage_target_identity_recursively() -> None:
    observations = [_observation('obs-1', 1)]
    output_hash = 'b' * 64
    target_key = 'target-a'
    stable_id = hashlib.sha256(
        canonical_json_bytes(
            {
                'target_key': target_key,
                'output_hash': output_hash,
                'output_index': 0,
            }
        )
    ).hexdigest()
    extraction = [
        _extract_stage(
            stage_key='extract-a',
            target_key=target_key,
            output_hash=output_hash,
            outputs=[
                {
                    'title': 'A',
                    'body': 'A',
                    'confidence': '0.8',
                    'supporting_observation_ids': ['obs-1'],
                }
            ],
        )
    ]
    reduction = [
        _extract_stage(
            stage_key='reduce-a',
            target_key='reduce-target',
            output_hash='c' * 64,
            outputs=[
                {
                    'title': 'Final',
                    'body': 'Final',
                    'confidence': '0.9',
                    'source_ids': [stable_id],
                }
            ],
        )
    ]

    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=observations,
        extraction_stages=extraction,
        reduction_stages=reduction,
        final_stage_key='reduce-a',
    )
    assert plan.candidates[0].sources[0].observation_id == 'obs-1'


def test_no_signal_coverage_retains_declaring_extraction_stage() -> None:
    stages = [
        _extract_stage(
            stage_key='extract-a',
            target_key='target-a',
            output_hash='b' * 64,
            outputs=[],
            no_signal=['obs-1'],
        ),
        _extract_stage(
            stage_key='extract-b',
            target_key='target-b',
            output_hash='c' * 64,
            outputs=[],
            no_signal=['obs-2'],
        ),
    ]
    observations = [_observation('obs-1', 1), _observation('obs-2', 2)]

    plan = build_finalization_plan(
        scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
        window_input_hash=_DIGEST,
        observations=observations,
        extraction_stages=stages,
    )
    assert [item.deciding_stage_key for item in plan.coverage] == ['extract-a', 'extract-b']


def test_tampered_final_snapshot_rejects_size_and_unknown_kind() -> None:
    observation = _observation('obs-1', 1)
    stage = _extract_stage(
        stage_key='extract-a',
        target_key='target-a',
        output_hash='b' * 64,
        outputs=[
            {
                'draft_id': 'draft-a',
                'title': 'A' * 256,
                'body': 'A',
                'confidence': '0.8',
                'supporting_observation_ids': ['obs-1'],
                'kind': 'not-a-kind',
            }
        ],
    )
    with pytest.raises(ProvenanceContractError, match='title|kind'):
        build_finalization_plan(
            scope={'organization_id': 'org', 'project_id': 'project', 'team_id': None, 'session_id': 'session'},
            window_input_hash=_DIGEST,
            observations=[observation],
            extraction_stages=[stage],
        )
