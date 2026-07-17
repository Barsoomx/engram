from __future__ import annotations

from typing import Any

import pytest
import sentry_sdk

from settings import logs
from settings.logs import configure_logger, resolve_environment


class _SentryInitRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def m_sentry_init(monkeypatch: pytest.MonkeyPatch) -> _SentryInitRecorder:
    recorder = _SentryInitRecorder()
    monkeypatch.setattr(sentry_sdk, 'init', recorder)
    monkeypatch.setattr(logs, 'configure_structlog', lambda **_: None)

    return recorder


def test_configure_logger_skips_sentry_when_dsn_unset(
    m_sentry_init: _SentryInitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logs, 'SENTRY_DSN', None)

    configure_logger()

    assert m_sentry_init.calls == []


def test_configure_logger_skips_sentry_when_dsn_is_blank(
    m_sentry_init: _SentryInitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logs, 'SENTRY_DSN', logs.optional_env('   '))

    configure_logger()

    assert m_sentry_init.calls == []


def test_configure_logger_initialises_sentry_once_when_dsn_present(
    m_sentry_init: _SentryInitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = 'https://public@sentry.invalid/1'
    monkeypatch.setattr(logs, 'SENTRY_DSN', dsn)
    monkeypatch.setattr(logs, 'SENTRY_RELEASE', 'engram@1.2.3')
    monkeypatch.setenv('SENTRY_ENVIRONMENT', 'staging')

    configure_logger(env_profile='dev')

    assert len(m_sentry_init.calls) == 1
    kwargs = m_sentry_init.calls[0]
    assert kwargs['dsn'] == dsn
    assert kwargs['environment'] == 'staging'
    assert kwargs['release'] == 'engram@1.2.3'
    assert kwargs['send_default_pii'] is False
    assert kwargs['debug'] is False


def test_configure_logger_falls_back_to_env_profile_without_sentry_environment(
    m_sentry_init: _SentryInitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logs, 'SENTRY_DSN', 'https://public@sentry.invalid/1')
    monkeypatch.delenv('SENTRY_ENVIRONMENT', raising=False)

    configure_logger(env_profile='production')

    assert m_sentry_init.calls[0]['environment'] == 'production'


def test_configure_logger_sends_no_release_when_unset(
    m_sentry_init: _SentryInitRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logs, 'SENTRY_DSN', 'https://public@sentry.invalid/1')
    monkeypatch.setattr(logs, 'SENTRY_RELEASE', None)

    configure_logger()

    assert m_sentry_init.calls[0]['release'] is None


def test_resolve_environment_prefers_explicit_sentry_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SENTRY_ENVIRONMENT', 'production')

    assert resolve_environment('dev') == 'production'


def test_resolve_environment_ignores_blank_sentry_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SENTRY_ENVIRONMENT', '  ')

    assert resolve_environment('dev') == 'dev'
