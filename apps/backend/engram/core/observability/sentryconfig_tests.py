from __future__ import annotations

import pytest

from engram.core.observability.sentryconfig import optional_env


@pytest.mark.parametrize('value', [None, '', '   ', '\n', '\t '])
def test_optional_env_treats_missing_and_blank_as_none(value: str | None) -> None:
    assert optional_env(value) is None


def test_optional_env_returns_stripped_value() -> None:
    assert optional_env('  https://public@sentry.invalid/1  ') == 'https://public@sentry.invalid/1'
