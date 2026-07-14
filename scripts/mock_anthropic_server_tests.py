from __future__ import annotations

import json

from scripts.mock_anthropic_server import SESSION_TITLE_PREFIX, generation_content


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


def test_generation_content_emits_exact_distill_reduce_v1_sources() -> None:
    prompt = json.dumps(
        {
            'drafts': [
                {'id': 'draft-a', 'title': 'A', 'body': 'A', 'confidence': '0.9'},
                {'id': 'draft-b', 'title': 'B', 'body': 'B', 'confidence': '0.8'},
            ]
        }
    )

    payload = json.loads(
        generation_content(
            'Return exactly a JSON object with the memories key following distill_reduce.v1.',
            prompt,
        )
    )

    assert set(payload) == {'memories'}
    assert len(payload['memories']) == 1
    assert payload['memories'][0]['source_ids'] == ['draft-a', 'draft-b']
