from __future__ import annotations

from collections.abc import Callable

import pytest
from pytest_django.fixtures import SettingsWrapper

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.base_url_validation import BaseUrlValidationError, validate_base_url
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.real_provider_tests import make_real_policy
from engram.model_policy.services import get_provider_gateway

Resolver = Callable[[str], tuple[str, ...]]


def _resolver_returning(*addresses: str) -> Resolver:
    def resolver(_host: str) -> tuple[str, ...]:
        return addresses

    return resolver


def _resolver_boom(_host: str) -> tuple[str, ...]:
    raise AssertionError('resolver must not be called for ip literals')


def test_empty_base_url_is_allowed() -> None:
    validate_base_url('', resolver=_resolver_boom)


def test_requires_https_in_production(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('http://api.example.com/v1', resolver=_resolver_returning('93.184.216.34'))


def test_allows_https_public_host_in_production(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    validate_base_url('https://api.example.com/v1', resolver=_resolver_returning('93.184.216.34'))


def test_rejects_loopback_in_production(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('https://gateway.internal/v1', resolver=_resolver_returning('127.0.0.1'))


def test_rejects_private_range_in_production(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('https://gateway.internal/v1', resolver=_resolver_returning('10.0.0.5'))


def test_rejects_metadata_endpoint_in_production(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('https://gateway.internal/v1', resolver=_resolver_returning('169.254.169.254'))


def test_rejects_metadata_ip_literal_without_dns(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('https://169.254.169.254/latest/meta-data', resolver=_resolver_boom)


def test_rejects_when_any_resolved_address_is_blocked(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('https://gateway.example/v1', resolver=_resolver_returning('93.184.216.34', '127.0.0.1'))


def test_dev_environment_allows_localhost_http() -> None:
    validate_base_url('http://localhost:11434/v1', resolver=_resolver_returning('127.0.0.1'))


def test_insecure_override_allows_localhost_http_in_production(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.ENVIRONMENT = 'production'
    monkeypatch.setenv('ENGRAM_ALLOW_INSECURE_PROVIDER_URLS', '1')
    validate_base_url('http://localhost:11434/v1', resolver=_resolver_returning('127.0.0.1'))


def test_insecure_override_still_rejects_metadata_in_production(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.ENVIRONMENT = 'production'
    monkeypatch.setenv('ENGRAM_ALLOW_INSECURE_PROVIDER_URLS', '1')
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('http://169.254.169.254/latest', resolver=_resolver_boom)


def test_insecure_override_still_rejects_private_range_in_production(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.ENVIRONMENT = 'production'
    monkeypatch.setenv('ENGRAM_ALLOW_INSECURE_PROVIDER_URLS', '1')
    with pytest.raises(BaseUrlValidationError):
        validate_base_url('http://gateway.internal/v1', resolver=_resolver_returning('10.0.0.5'))


@pytest.mark.django_db
def test_gateway_rechecks_custom_base_url_at_call_time(
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.ENVIRONMENT = 'production'
    settings.ENGRAM_SECRET_ENCRYPTION_KEY = 'unit-test-encryption-key'
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, base_url='https://169.254.169.254/v1')

    with pytest.raises(ModelPolicyError) as exc_info:
        get_provider_gateway(policy)

    assert exc_info.value.code == 'provider_base_url_invalid'
