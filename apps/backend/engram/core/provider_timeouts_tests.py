from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import ModuleType

import pytest

from engram.core import provider_timeouts


@pytest.fixture
def f_env_tuned_timeouts(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv('ENGRAM_FLEX_PROCESSING_CEILING', '900')
    monkeypatch.setenv('ENGRAM_TIMEOUT_LADDER_MARGIN', '30')

    yield importlib.reload(provider_timeouts)

    monkeypatch.undo()
    importlib.reload(provider_timeouts)


def test_ladder_defaults_cover_the_documented_flex_window() -> None:
    assert provider_timeouts.FLEX_PROCESSING_CEILING_SECONDS == 600
    assert provider_timeouts.LADDER_MARGIN_SECONDS == 60
    assert provider_timeouts.soft_time_limit_for(1) == 660
    assert provider_timeouts.ladder_step_above(660) == 720


def test_soft_time_limit_scales_with_the_provider_call_budget() -> None:
    assert provider_timeouts.soft_time_limit_for(2) == 1260
    assert provider_timeouts.soft_time_limit_for(0) == 660
    assert provider_timeouts.soft_time_limit_for(-3) == 660


def test_ladder_constants_are_env_tunable(f_env_tuned_timeouts: ModuleType) -> None:
    assert f_env_tuned_timeouts.FLEX_PROCESSING_CEILING_SECONDS == 900
    assert f_env_tuned_timeouts.LADDER_MARGIN_SECONDS == 30
    assert f_env_tuned_timeouts.soft_time_limit_for(1) == 930
    assert f_env_tuned_timeouts.ladder_step_above(930) == 960


def test_capacity_reports_how_many_queued_flex_calls_fit_in_a_soft_limit() -> None:
    for provider_calls in (1, 2, 3, 8):
        soft_limit = provider_timeouts.soft_time_limit_for(provider_calls)

        assert provider_timeouts.flex_provider_call_capacity(soft_limit) == provider_calls


def test_capacity_is_zero_for_limits_that_cannot_outlast_a_queued_flex_call() -> None:
    assert provider_timeouts.flex_provider_call_capacity(600) == 0
    assert provider_timeouts.flex_provider_call_capacity(180) == 0
    assert provider_timeouts.flex_provider_call_capacity(0) == 0
    assert provider_timeouts.flex_provider_call_capacity(-120) == 0


def test_capacity_follows_the_env_tuned_ceiling(f_env_tuned_timeouts: ModuleType) -> None:
    assert f_env_tuned_timeouts.flex_provider_call_capacity(930) == 1
    assert f_env_tuned_timeouts.flex_provider_call_capacity(660) == 0
