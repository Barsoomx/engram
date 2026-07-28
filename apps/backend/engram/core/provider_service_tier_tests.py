from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType

import pytest

from engram.core import provider_service_tier
from engram.core.provider_service_tier import resolve_service_tier

_FLEX_POLICY: dict[str, object] = {'service_tier': {'tier': 'flex'}}


@pytest.fixture
def f_env_tuned_budget(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv('ENGRAM_SERVICE_TIER_ATTEMPT_BUDGET', '1')

    yield importlib.reload(provider_service_tier)

    monkeypatch.undo()
    importlib.reload(provider_service_tier)


def test_unconfigured_policy_omits_the_service_tier() -> None:
    assert resolve_service_tier(None, attempt=0) is None
    assert resolve_service_tier({}, attempt=0) is None
    assert resolve_service_tier({'model_overrides': {}}, attempt=0) is None
    assert resolve_service_tier({'service_tier': 'flex'}, attempt=0) is None


def test_early_attempts_use_the_configured_cheap_tier() -> None:
    assert resolve_service_tier(_FLEX_POLICY, attempt=0) == 'flex'
    assert resolve_service_tier(_FLEX_POLICY, attempt=1) == 'flex'


def test_late_attempts_fall_back_to_the_project_default() -> None:
    assert resolve_service_tier(_FLEX_POLICY, attempt=2) is None
    assert resolve_service_tier(_FLEX_POLICY, attempt=7) is None


def test_negative_attempts_are_treated_as_the_first_attempt() -> None:
    assert resolve_service_tier(_FLEX_POLICY, attempt=-4) == 'flex'


def test_unparseable_attempt_gives_up_the_queued_tier() -> None:
    for attempt in ('0', None, 1.5, True, [0]):
        assert resolve_service_tier(_FLEX_POLICY, attempt=attempt) is None


def test_attempt_budget_is_configurable_per_policy() -> None:
    single = {'service_tier': {'tier': 'flex', 'attempt_budget': 1}}
    disabled = {'service_tier': {'tier': 'flex', 'attempt_budget': 0}}

    assert resolve_service_tier(single, attempt=0) == 'flex'
    assert resolve_service_tier(single, attempt=1) is None
    assert resolve_service_tier(disabled, attempt=0) is None


def test_every_documented_tier_survives_the_whitelist() -> None:
    for tier in ('auto', 'default', 'flex', 'priority'):
        assert resolve_service_tier({'service_tier': {'tier': tier}}, attempt=0) == tier


def test_unsupported_tier_values_are_ignored() -> None:
    for tier in ('turbo', 'FLEX', '', 123, None, {'tier': 'flex'}):
        assert resolve_service_tier({'service_tier': {'tier': tier}}, attempt=0) is None


def test_malformed_attempt_budget_falls_back_to_the_default_budget() -> None:
    text_budget = {'service_tier': {'tier': 'flex', 'attempt_budget': '1'}}
    bool_budget = {'service_tier': {'tier': 'flex', 'attempt_budget': True}}
    negative_budget = {'service_tier': {'tier': 'flex', 'attempt_budget': -5}}

    assert resolve_service_tier(text_budget, attempt=1) == 'flex'
    assert resolve_service_tier(bool_budget, attempt=1) == 'flex'
    assert resolve_service_tier(negative_budget, attempt=0) is None


def test_default_attempt_budget_is_env_tunable(f_env_tuned_budget: ModuleType) -> None:
    assert f_env_tuned_budget.resolve_service_tier(_FLEX_POLICY, attempt=0) == 'flex'
    assert f_env_tuned_budget.resolve_service_tier(_FLEX_POLICY, attempt=1) is None
