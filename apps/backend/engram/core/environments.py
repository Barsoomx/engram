from __future__ import annotations

NON_PRODUCTION_ENVIRONMENTS = frozenset({'dev', 'development', 'local', 'test'})


def is_non_production(environment: str) -> bool:
    return environment in NON_PRODUCTION_ENVIRONMENTS
