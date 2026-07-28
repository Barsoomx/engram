from __future__ import annotations

import os

FLEX_PROCESSING_CEILING_SECONDS = int(os.environ.get('ENGRAM_FLEX_PROCESSING_CEILING', '600'))
LADDER_MARGIN_SECONDS = int(os.environ.get('ENGRAM_TIMEOUT_LADDER_MARGIN', '60'))


def soft_time_limit_for(provider_calls: int) -> int:
    return FLEX_PROCESSING_CEILING_SECONDS * max(provider_calls, 1) + LADDER_MARGIN_SECONDS


def ladder_step_above(seconds: int) -> int:
    return seconds + LADDER_MARGIN_SECONDS


def flex_provider_call_capacity(soft_time_limit: int) -> int:
    return max(soft_time_limit - LADDER_MARGIN_SECONDS, 0) // FLEX_PROCESSING_CEILING_SECONDS
