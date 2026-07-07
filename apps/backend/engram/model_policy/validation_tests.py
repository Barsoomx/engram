from __future__ import annotations

import uuid
from typing import Any

import pytest

from engram.core.models import Organization, Project, Team
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import ProviderCallInput, ProviderCallResult
from engram.model_policy.validation import (
    NO_PROJECT_AVAILABLE_ERROR_CODE,
    VALIDATION_HTTP_TIMEOUT_SECONDS,
    PolicyValidationResult,
    validate_policy,
)


class _OkGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='ok',
            generated_body='ok',
        )


class _HttpErrorGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ModelPolicyError('provider_http_error', 'provider returned 402', http_status=402)


class _TimeoutGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ModelPolicyError('provider_timeout', 'provider timed out', retryable=True)


class _SecretDisabledGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ProviderSecretError('provider secret is disabled')


class _UnknownErrorGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise ModelPolicyError('some_internal_code', 'internal boom 0xdeadbeef')


class _RecordingGateway:
    def __init__(self, sink: list[ProviderCallInput]) -> None:
        self._sink = sink

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self._sink.append(data)

        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='ok',
            generated_body='ok',
        )


def _gateway_factory(gateway: Any) -> Any:
    def factory(_policy: ModelPolicy, *, timeout: int | None = None) -> Any:
        return gateway

    return factory


def _make_secret(organization: Organization, team: Team | None) -> ProviderSecret:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'secret-{uuid.uuid4()}',
        provider='openai',
        scope='team' if team is not None else 'organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )

    return secret


def _make_policy(
    organization: Organization,
    team: Team | None,
    project: Project | None,
    secret: ProviderSecret,
    *,
    task_type: str = 'generation',
) -> ModelPolicy:
    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name=f'policy-{task_type}',
        scope='project' if project is not None else ('team' if team is not None else 'organization'),
        task_type=task_type,
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
        active=True,
    )


@pytest.fixture
def f_organization() -> Organization:
    return Organization.objects.create(name='ValidateOrg', slug='validate-org')


@pytest.fixture
def f_team(f_organization: Organization) -> Team:
    return Team.objects.create(organization=f_organization, name='Platform', slug='platform')


@pytest.fixture
def f_project(f_organization: Organization) -> Project:
    return Project.objects.create(organization=f_organization, name='Backend', slug='backend')


@pytest.fixture
def f_policy(
    f_organization: Organization,
    f_team: Team,
    f_project: Project,
) -> ModelPolicy:
    secret = _make_secret(f_organization, f_team)

    return _make_policy(f_organization, f_team, f_project, secret, task_type='generation')


@pytest.mark.django_db
def test_validate_policy_ok_returns_success_with_latency(f_policy: ModelPolicy) -> None:
    result = validate_policy(f_policy, gateway_factory=_gateway_factory(_OkGateway()))

    assert isinstance(result, PolicyValidationResult)
    assert result.policy_id == str(f_policy.id)
    assert result.task_type == 'generation'
    assert result.provider == 'openai'
    assert result.model == 'gpt-4o-mini'
    assert result.ok is True
    assert result.error_code is None
    assert result.public_error is None
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0


@pytest.mark.django_db
def test_validate_policy_http_error_returns_sanitized_code(f_policy: ModelPolicy) -> None:
    result = validate_policy(f_policy, gateway_factory=_gateway_factory(_HttpErrorGateway()))

    assert result.ok is False
    assert result.error_code == 'provider_http_error'
    assert result.public_error
    assert '402' not in result.public_error
    assert 'returned' not in result.public_error.lower()


@pytest.mark.django_db
def test_validate_policy_timeout_returns_sanitized_code(f_policy: ModelPolicy) -> None:
    result = validate_policy(f_policy, gateway_factory=_gateway_factory(_TimeoutGateway()))

    assert result.ok is False
    assert result.error_code == 'provider_timeout'
    assert result.public_error


@pytest.mark.django_db
def test_validate_policy_provider_secret_error_is_sanitized(f_policy: ModelPolicy) -> None:
    result = validate_policy(f_policy, gateway_factory=_gateway_factory(_SecretDisabledGateway()))

    assert result.ok is False
    assert result.error_code == 'provider_secret_unavailable'
    assert result.public_error
    assert 'provider secret is disabled' not in result.public_error


@pytest.mark.django_db
def test_validate_policy_unknown_error_code_falls_back(f_policy: ModelPolicy) -> None:
    result = validate_policy(f_policy, gateway_factory=_gateway_factory(_UnknownErrorGateway()))

    assert result.ok is False
    assert result.error_code == 'provider_error'
    assert result.public_error
    assert '0xdeadbeef' not in result.public_error


@pytest.mark.django_db
def test_validate_policy_without_project_returns_no_project_code(
    f_organization: Organization,
) -> None:
    secret = _make_secret(f_organization, None)
    policy = _make_policy(f_organization, None, None, secret, task_type='generation')

    result = validate_policy(policy, gateway_factory=_gateway_factory(_OkGateway()))

    assert result.ok is False
    assert result.error_code == NO_PROJECT_AVAILABLE_ERROR_CODE
    assert result.public_error


@pytest.mark.django_db
def test_validate_policy_uses_candidates_kind_for_curation(
    f_organization: Organization,
    f_team: Team,
    f_project: Project,
) -> None:
    secret = _make_secret(f_organization, f_team)
    curation_policy = _make_policy(f_organization, f_team, f_project, secret, task_type='curation')
    captured: list[ProviderCallInput] = []

    validate_policy(
        curation_policy,
        gateway_factory=_gateway_factory(_RecordingGateway(captured)),
    )

    assert len(captured) == 1
    assert captured[0].response_kind == 'candidates'


@pytest.mark.django_db
def test_validate_policy_uses_single_kind_for_generation(f_policy: ModelPolicy) -> None:
    captured: list[ProviderCallInput] = []

    validate_policy(
        f_policy,
        gateway_factory=_gateway_factory(_RecordingGateway(captured)),
    )

    assert captured[0].response_kind == 'single'


@pytest.mark.django_db
def test_validate_policy_bounds_gateway_timeout(f_policy: ModelPolicy) -> None:
    seen_timeouts: list[int | None] = []

    def factory(_policy: ModelPolicy, *, timeout: int | None = None) -> Any:
        seen_timeouts.append(timeout)

        return _OkGateway()

    validate_policy(f_policy, gateway_factory=factory)

    assert seen_timeouts == [VALIDATION_HTTP_TIMEOUT_SECONDS]
