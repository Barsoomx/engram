from __future__ import annotations

import json
from decimal import Decimal

from engram.memory.candidate_parsing import (
    parse_confidence,
    parse_synthesized_candidates,
    strip_json_fence,
    truncate_with_marker,
)


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


def test_truncate_with_marker_returns_short_text_unchanged() -> None:
    assert truncate_with_marker('abc', 10) == 'abc'


def test_truncate_with_marker_appends_marker_when_over_cap() -> None:
    result = truncate_with_marker('x' * 500, 100)

    assert len(result) <= 100 + len('\n[truncated 99999 chars]')
    assert '[truncated' in result


def test_parse_confidence_valid_number_is_not_fallback() -> None:
    confidence, fallback = parse_confidence(0.85)

    assert confidence == Decimal('0.850')
    assert fallback is False


def test_parse_confidence_clamps_out_of_range_without_fallback() -> None:
    assert parse_confidence(1.5) == (Decimal('1.000'), False)
    assert parse_confidence(-3) == (Decimal('0.000'), False)


def test_parse_confidence_unparseable_is_zero_and_fallback() -> None:
    confidence, fallback = parse_confidence('banana')

    assert confidence == Decimal('0.000')
    assert fallback is True


def test_parse_confidence_missing_is_zero_and_fallback() -> None:
    confidence, fallback = parse_confidence(None)

    assert confidence == Decimal('0.000')
    assert fallback is True


def test_parse_synthesized_candidates_invalid_json_yields_zero_confidence_fallback() -> None:
    candidates = parse_synthesized_candidates('not json at all')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True
    assert candidates[0].body == 'not json at all'


def test_parse_synthesized_candidates_object_without_memories_yields_zero_fallback() -> None:
    candidates = parse_synthesized_candidates('{"other": 1}')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True


def test_parse_synthesized_candidates_unparseable_confidence_marks_fallback() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 'high'}]})

    candidates = parse_synthesized_candidates(raw)

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True


def test_parse_synthesized_candidates_valid_confidence_not_fallback() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'kind': 'gotcha'}]})

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].confidence == Decimal('0.900')
    assert candidates[0].kind == 'gotcha'
    assert candidates[0].parse_fallback is False


def test_parse_synthesized_candidates_empty_memories_is_empty() -> None:
    assert parse_synthesized_candidates('{"memories": []}') == ()
