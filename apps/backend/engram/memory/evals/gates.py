from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from engram.core.redaction import REDACTED_VALUE, SECRET_STRING_RE

_WHITESPACE_RE = re.compile(r'\s+')
_LIFECYCLE_TYPES = frozenset({'session_start', 'session_end', 'session_lifecycle'})


@dataclass(frozen=True, slots=True)
class GateDecision:
    disposition: str
    outcome: str | None = None
    reason_code: str | None = None
    target_memory_version_id: uuid.UUID | None = None


def _normalize(value: str) -> str:
    return _WHITESPACE_RE.sub(' ', value or '').strip()


def _redaction_only(body: str, redaction_codes: tuple[str, ...]) -> bool:
    if not redaction_codes:
        return False

    stripped = _normalize(body).replace(REDACTED_VALUE, '')

    return _normalize(stripped) == ''


def _identity_target(candidate: dict[str, object], entries: list[dict[str, object]]) -> dict[str, object] | None:
    content_hash = candidate.get('content_hash')
    for entry in entries:
        if content_hash and entry.get('body_hash') == content_hash:
            return entry

    return None


def classify_deterministic_gate(case_input: dict[str, object]) -> GateDecision:
    candidate = case_input['candidate']
    scope = case_input['effective_scope']
    entries = case_input['shortlist']['entries']

    body = str(candidate.get('body') or '')
    title = str(candidate.get('title') or '')
    redaction_codes = tuple(candidate.get('redaction_codes') or ())
    observation_type = candidate.get('observation_type')

    if scope['visibility_scope'] == 'session':
        return GateDecision('terminal', 'reject_candidate', 'non_durable_session_scope')

    if not _normalize(body):
        return GateDecision('terminal', 'reject_candidate', 'noise_empty')

    if _normalize(body) == _normalize(title):
        return GateDecision('terminal', 'reject_candidate', 'noise_title_echo')

    if _redaction_only(body, redaction_codes):
        return GateDecision('terminal', 'reject_candidate', 'noise_redaction_only')

    if observation_type in _LIFECYCLE_TYPES:
        return GateDecision('terminal', 'reject_candidate', 'noise_lifecycle_only')

    if SECRET_STRING_RE.search(body) or SECRET_STRING_RE.search(title):
        return GateDecision('terminal', 'reject_candidate', 'unsafe_content_after_redaction')

    target = _identity_target(candidate, entries)
    if target is not None:
        candidate_refs = set(candidate.get('evidence_refs') or ())
        target_refs = set(target.get('evidence_refs') or ())
        if candidate_refs - target_refs:
            return GateDecision(
                'terminal',
                'merge_evidence',
                'exact_identity',
                uuid.UUID(str(target['memory_version_id'])),
            )

        return GateDecision('terminal', 'reject_candidate', 'exact_duplicate_no_new_evidence')

    return GateDecision('continue')
