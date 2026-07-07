from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from engram.core.models import clamp_memory_kind

_FALLBACK_CONFIDENCE = Decimal('0.000')
_CONFIDENCE_QUANTUM = Decimal('0.001')


def strip_json_fence(raw_body: str) -> str:
    if not isinstance(raw_body, str):
        return raw_body

    stripped = raw_body.strip()
    if not stripped.startswith('```'):
        return raw_body

    body_lines = stripped.splitlines()[1:]
    closing_index = next(
        (index for index in range(len(body_lines) - 1, -1, -1) if body_lines[index].strip() == '```'),
        None,
    )
    if closing_index is None:
        return raw_body

    return '\n'.join(body_lines[:closing_index]).strip()


def truncate_with_marker(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text

    marker = f'\n[truncated {len(text) - cap} chars]'
    head = text[: max(cap - len(marker), 0)]

    return head + marker


@dataclass(frozen=True)
class SynthesizedCandidate:
    title: str
    body: str
    confidence: Decimal
    supporting_observation_ids: tuple[str, ...]
    kind: str = ''
    parse_fallback: bool = False


def parse_confidence(value: object) -> tuple[Decimal, bool]:
    try:
        confidence = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return _FALLBACK_CONFIDENCE, True

    confidence = max(Decimal('0'), min(Decimal('1'), confidence))

    return confidence.quantize(_CONFIDENCE_QUANTUM), False


def _clamp_confidence(value: object) -> Decimal:
    return parse_confidence(value)[0]


def _fallback_candidate(raw_body: str) -> SynthesizedCandidate:
    text = raw_body.strip()
    title = text.splitlines()[0][:255] if text else 'Session distillation'

    return SynthesizedCandidate(
        title=title,
        body=text or title,
        confidence=_FALLBACK_CONFIDENCE,
        supporting_observation_ids=(),
        parse_fallback=True,
    )


def parse_synthesized_candidates(raw_body: str) -> tuple[SynthesizedCandidate, ...]:
    try:
        parsed = json.loads(strip_json_fence(raw_body))
    except (json.JSONDecodeError, TypeError):
        return (_fallback_candidate(raw_body),)

    if isinstance(parsed, dict):
        items = parsed.get('memories')
        if not isinstance(items, list):
            return (_fallback_candidate(raw_body),)
    elif isinstance(parsed, list):
        items = parsed
    else:
        return (_fallback_candidate(raw_body),)

    candidates: list[SynthesizedCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip()
        body = str(item.get('body') or '').strip()
        if not title and not body:
            continue
        supporting = tuple(str(value) for value in (item.get('supporting_observation_ids') or []))
        confidence, confidence_fallback = parse_confidence(item.get('confidence'))
        candidates.append(
            SynthesizedCandidate(
                title=(title or body)[:255],
                body=body or title,
                confidence=confidence,
                supporting_observation_ids=supporting,
                kind=clamp_memory_kind(item.get('kind')),
                parse_fallback=confidence_fallback,
            ),
        )

    return tuple(candidates)
