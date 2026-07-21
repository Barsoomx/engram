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


def test_generation_content_emits_curation_decision_v1_for_empty_shortlist() -> None:
    candidate_ref = 'a' * 32
    schema_prefix = 'Return exactly one JSON object and nothing else following curation_decision_v1.'
    envelope = {
        'schema': 'curation_judge_input.v1',
        'candidate': {'evidence_refs': [candidate_ref]},
        'comparisons': [],
    }
    prompt = f'{schema_prefix}\n\n{json.dumps(envelope, sort_keys=True, separators=(",", ":"))}'

    raw = generation_content('', prompt)
    assert raw == curation_decision_content(prompt)

    payload = json.loads(raw)
    assert set(payload) == {
        'schema_version',
        'outcome',
        'relation',
        'target_memory_version_id',
        'candidate_evidence_refs',
        'comparisons',
        'applicability',
        'temporal_order',
        'reason_code',
        'reason',
    }
    assert payload['schema_version'] == 1
    assert payload['outcome'] == 'publish_new'
    assert payload['relation'] == 'unrelated'
    assert payload['target_memory_version_id'] is None
    assert payload['candidate_evidence_refs'] == [candidate_ref]
    assert payload['comparisons'] == []
    assert payload['applicability'] == 'same'
    assert payload['temporal_order'] == 'not_applicable'
    assert payload['reason_code'] == 'distinct_claim'
    assert 1 <= len(payload['reason']) <= 500


def test_curation_decision_content_echoes_shortlist_comparisons_in_order() -> None:
    version_a = '11111111-1111-4111-8111-111111111111'
    version_b = '22222222-2222-4222-8222-222222222222'
    envelope = {
        'schema': 'curation_judge_input.v1',
        'candidate': {'evidence_refs': ['ref-c']},
        'comparisons': [
            {'memory_version_id': version_a, 'evidence_refs': ['ref-a']},
            {'memory_version_id': version_b, 'evidence_refs': []},
        ],
    }
    prompt = json.dumps(envelope)

    payload = json.loads(curation_decision_content(prompt))

    assert [c['memory_version_id'] for c in payload['comparisons']] == [version_a, version_b]
    assert all(c['relation'] == 'unrelated' for c in payload['comparisons'])
    assert payload['comparisons'][0]['target_evidence_refs'] == ['ref-a']
    assert payload['comparisons'][1]['target_evidence_refs'] == []
