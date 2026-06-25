from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED_VALUE = '[REDACTED]'
SENSITIVE_KEY_MARKERS = (
    'apikey',
    'authorization',
    'accesskey',
    'password',
    'privatekey',
    'providerkey',
    'secret',
    'token',
)
SECRET_STRING_RE = re.compile(
    r'(?i)('
    r'sk-[a-z0-9][a-z0-9_-]{8,}'
    r'|egk_[a-z0-9][a-z0-9_-]{8,}'
    r'|bearer\s+[a-z0-9._~+/=-]{12,}'
    r'|AIza[a-z0-9_-]{20,}'
    r'|\b\d{6,}:[a-z0-9_-]{20,}\b'
    r'|xox[baprs]-[a-z0-9-]{20,}'
    r')',
)


@dataclass(frozen=True)
class RedactionResult:
    value: object
    redacted: bool


def redact_value(value: object) -> RedactionResult:
    if isinstance(value, dict):
        redacted = False
        cleaned = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                cleaned[key] = REDACTED_VALUE
                redacted = True
                continue

            item_result = redact_value(item)
            cleaned[key] = item_result.value
            redacted = redacted or item_result.redacted

        return RedactionResult(value=cleaned, redacted=redacted)

    if isinstance(value, list | tuple):
        redacted = False
        cleaned = []
        for item in value:
            item_result = redact_value(item)
            cleaned.append(item_result.value)
            redacted = redacted or item_result.redacted

        return RedactionResult(value=cleaned, redacted=redacted)

    if isinstance(value, str):
        cleaned = SECRET_STRING_RE.sub(REDACTED_VALUE, value)

        return RedactionResult(value=cleaned, redacted=cleaned != value)

    return RedactionResult(value=value, redacted=False)


def is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())

    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)
