from __future__ import annotations

import json

from scripts.mock_anthropic_server import (
    SESSION_TITLE_PREFIX,
    curation_decision_content,
    generation_content,
)


def test_generation_content_emits_exact_distill_extract_v1_coverage() -> None:
    first = '00000000-0000-4000-8000-000000000001'
    second = '00000000-0000-4000-8000-000000000002'
    prompt = f'Observation: {first}\nTitle: first\n\nObservation: {second}\nTitle: second'

    payload = json.loads(
        generation_content(
            'Return exactly memories and no_signal_observation_ids following distill_extract.v1.',
            prompt,
        )
    )

    assert set(payload) == {'memories', 'no_signal_observation_ids'}
    assert payload['no_signal_observation_ids'] == []
    assert len(payload['memories']) == 1
    memory = payload['memories'][0]
    assert memory['title'].startswith(SESSION_TITLE_PREFIX)
    assert memory['supporting_observation_ids'] == [first, second]


def test_generation_content_emits_exact_distill_reduce_v2_source_refs() -> None:
    prompt = json.dumps(
        {
            'drafts': [
                {'index': 1, 'title': 'A', 'body': 'A', 'confidence': '0.9'},
                {'index': 2, 'title': 'B', 'body': 'B', 'confidence': '0.8'},
            ]
        }
    )

    payload = json.loads(
        generation_content(
            'You consolidate engineering-memory drafts under the distill_reduce.v2 contract.',
            prompt,
        )
    )

    assert set(payload) == {'memories'}
    assert len(payload['memories']) == 1
    memory = payload['memories'][0]
    assert memory['source_refs'] == [1, 2]
    assert all(isinstance(source_ref, int) for source_ref in memory['source_refs'])


def test_generation_content_emits_curation_decision_v2_for_empty_shortlist() -> None:
    schema_prefix = 'Return exactly one JSON object and nothing else following curation_decision_v2.'
    envelope = {
        'schema': 'curation_judge_input.v2',
        'candidate': {'evidence_tier': 'supported'},
        'comparisons': [],
    }
    prompt = f'{schema_prefix}\n\n{json.dumps(envelope, sort_keys=True, separators=(",", ":"))}'

    raw = generation_content('', prompt)
    assert raw == curation_decision_content(prompt)

    payload = json.loads(raw)
    assert set(payload) == {'schema_version', 'comparisons', 'reason'}
    assert payload['schema_version'] == 2
    assert payload['comparisons'] == []
    assert 1 <= len(payload['reason']) <= 500


def test_curation_decision_content_echoes_shortlist_comparison_indices() -> None:
    envelope = {
        'schema': 'curation_judge_input.v2',
        'candidate': {'evidence_tier': 'supported'},
        'comparisons': [{'index': 1}, {'index': 2}],
    }
    prompt = json.dumps(envelope)

    payload = json.loads(curation_decision_content(prompt))

    assert [comparison['index'] for comparison in payload['comparisons']] == [1, 2]
    assert all(comparison['relation'] == 'unrelated' for comparison in payload['comparisons'])
    assert all(comparison['applicability'] == 'different' for comparison in payload['comparisons'])
