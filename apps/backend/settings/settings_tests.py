from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from settings.settings import (
    DEFAULT_DEV_SECRET_KEY,
    require_secret_key,
    resolve_allowed_hosts,
)


def test_require_secret_key_returns_dev_default_when_unset_in_dev() -> None:
    assert require_secret_key(raw_secret_key='', environment='dev') == DEFAULT_DEV_SECRET_KEY


def test_require_secret_key_rejects_default_in_production() -> None:
    with pytest.raises(ImproperlyConfigured):
        require_secret_key(raw_secret_key=DEFAULT_DEV_SECRET_KEY, environment='production')


def test_require_secret_key_rejects_empty_in_production() -> None:
    with pytest.raises(ImproperlyConfigured):
        require_secret_key(raw_secret_key='', environment='production')


def test_require_secret_key_accepts_custom_value_in_production() -> None:
    provided = 'a-strong-production-secret'

    assert require_secret_key(raw_secret_key=provided, environment='production') == provided


def test_resolve_allowed_hosts_requires_env_in_production() -> None:
    with pytest.raises(ImproperlyConfigured):
        resolve_allowed_hosts(raw_allowed_hosts='', environment='production')


def test_resolve_allowed_hosts_does_not_default_to_wildcard_in_production() -> None:
    hosts = resolve_allowed_hosts(raw_allowed_hosts='engram.example.com', environment='production')
    wildcard = '0.0.0.0'  # noqa: S104

    assert hosts == ['engram.example.com']
    assert wildcard not in hosts


def test_resolve_allowed_hosts_keeps_local_defaults_in_dev() -> None:
    hosts = resolve_allowed_hosts(raw_allowed_hosts='', environment='dev')

    assert '127.0.0.1' in hosts
