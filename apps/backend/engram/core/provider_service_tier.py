from __future__ import annotations

import os
from collections.abc import Mapping

SERVICE_TIER_METADATA_KEY = 'service_tier'
SERVICE_TIER_FLEX = 'flex'
SUPPORTED_SERVICE_TIERS = frozenset({'auto', 'default', SERVICE_TIER_FLEX, 'priority'})
DEFAULT_ATTEMPT_BUDGET = int(os.environ.get('ENGRAM_SERVICE_TIER_ATTEMPT_BUDGET', '2'))


def resolve_service_tier(metadata: Mapping[str, object] | None, *, attempt: int) -> str | None:
    config = metadata.get(SERVICE_TIER_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(config, Mapping):
        return None

    tier = config.get('tier')
    if not isinstance(tier, str) or tier not in SUPPORTED_SERVICE_TIERS:
        return None

    index = _attempt_index(attempt)
    if index is None or index >= _attempt_budget(config):
        return None

    return tier


def _attempt_index(attempt: int) -> int | None:
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        return None

    return max(attempt, 0)


def _attempt_budget(config: Mapping[str, object]) -> int:
    budget = config.get('attempt_budget')
    if isinstance(budget, bool) or not isinstance(budget, int):
        return DEFAULT_ATTEMPT_BUDGET

    return max(budget, 0)
