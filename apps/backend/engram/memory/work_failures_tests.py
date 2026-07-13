from datetime import timedelta
from types import ModuleType

import pytest
from django.db.utils import DatabaseError, OperationalError

from engram.memory.services import MemoryWorkerError
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError

HEX64 = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
HEX64_UPPER = HEX64.upper()


def _wf() -> ModuleType:
    from engram.memory import work_failures

    return work_failures


def _classified(
    failure_class: str,
    code: str,
    *,
    redacted_detail: str = '',
    configuration_fingerprint: str = '',
) -> object:
    return _wf().ClassifiedWorkFailure(
        failure_class=failure_class,
        code=code,
        redacted_detail=redacted_detail,
        configuration_fingerprint=configuration_fingerprint,
    )


CODE_TABLE = (
    ('worker_lost', 'lease_expired', False),
    ('infrastructure_transient', 'database_unavailable', False),
    ('infrastructure_transient', 'dependency_timeout', False),
    ('infrastructure_transient', 'dependency_unreachable', False),
    ('provider_transient', 'provider_timeout', False),
    ('provider_transient', 'provider_unreachable', False),
    ('provider_transient', 'provider_rate_limited', False),
    ('provider_transient', 'provider_unavailable', False),
    ('configuration', 'model_policy_unavailable', True),
    ('configuration', 'provider_secret_unavailable', True),
    ('configuration', 'policy_scope_invalid', True),
    ('configuration', 'provider_endpoint_invalid', True),
    ('configuration', 'provider_account_unavailable', True),
    ('invalid_input', 'work_contract_invalid', False),
    ('invalid_input', 'work_scope_invalid', False),
    ('invalid_input', 'work_fingerprint_mismatch', False),
    ('invalid_input', 'provider_request_invalid', False),
    ('unexpected', 'unexpected_exception', False),
)


@pytest.mark.parametrize(('failure_class', 'code', 'needs_fingerprint'), CODE_TABLE)
def test_every_spec_class_and_code_constructs(failure_class: str, code: str, needs_fingerprint: bool) -> None:
    fingerprint = HEX64 if needs_fingerprint else ''
    failure = _classified(failure_class, code, configuration_fingerprint=fingerprint)

    assert failure.failure_class == failure_class
    assert failure.code == code
    assert failure.configuration_fingerprint == fingerprint


@pytest.mark.parametrize(
    'code',
    ['a', '0', 'lease_expired', 'provider_timeout', 'work_contract_invalid', 'provider_5xx', 'a' * 128],
)
def test_valid_codes_are_accepted(code: str) -> None:
    assert _classified('unexpected', code).code == code


@pytest.mark.parametrize(
    'code',
    ['', 'A', 'Provider', 'provider timeout', 'bad-code', 'code.dot', 'a' * 129, 'café'],
)
def test_invalid_codes_are_rejected(code: str) -> None:
    with pytest.raises(ValueError):
        _classified('unexpected', code)


def test_configuration_requires_lowercase_hex_fingerprint() -> None:
    assert _classified('configuration', 'model_policy_unavailable', configuration_fingerprint=HEX64)

    with pytest.raises(ValueError):
        _classified('configuration', 'model_policy_unavailable', configuration_fingerprint='')

    with pytest.raises(ValueError):
        _classified('configuration', 'model_policy_unavailable', configuration_fingerprint=HEX64_UPPER)

    with pytest.raises(ValueError):
        _classified('configuration', 'model_policy_unavailable', configuration_fingerprint='a' * 63)


def test_non_configuration_rejects_fingerprint() -> None:
    assert _classified('provider_transient', 'provider_timeout', configuration_fingerprint='')

    with pytest.raises(ValueError):
        _classified('provider_transient', 'provider_timeout', configuration_fingerprint=HEX64)


TRANSLATION_TABLE = (
    (ModelPolicyError('provider_timeout', 'slow', retryable=True), 'provider_transient', 'provider_timeout', False),
    (
        ModelPolicyError('provider_unreachable', 'down', retryable=True),
        'provider_transient',
        'provider_unreachable',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'rate', retryable=True, http_status=429),
        'provider_transient',
        'provider_rate_limited',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'boom', retryable=True, http_status=500),
        'provider_transient',
        'provider_unavailable',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'boom', retryable=True, http_status=503),
        'provider_transient',
        'provider_unavailable',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'slow', retryable=True, http_status=408),
        'provider_transient',
        'provider_timeout',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'early', retryable=True, http_status=425),
        'provider_transient',
        'provider_unavailable',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'auth', http_status=401),
        'configuration',
        'provider_account_unavailable',
        True,
    ),
    (
        ModelPolicyError('provider_http_error', 'pay', http_status=402),
        'configuration',
        'provider_account_unavailable',
        True,
    ),
    (
        ModelPolicyError('provider_http_error', 'forbidden', http_status=403),
        'configuration',
        'provider_account_unavailable',
        True,
    ),
    (
        ModelPolicyError('provider_http_error', 'missing', http_status=404),
        'configuration',
        'provider_endpoint_invalid',
        True,
    ),
    (
        ModelPolicyError('provider_http_error', 'bad', http_status=400),
        'invalid_input',
        'provider_request_invalid',
        False,
    ),
    (
        ModelPolicyError('provider_http_error', 'unprocessable', http_status=422),
        'invalid_input',
        'provider_request_invalid',
        False,
    ),
    (
        ModelPolicyError('model_policy_not_found', 'none'),
        'configuration',
        'model_policy_unavailable',
        True,
    ),
    (
        ModelPolicyError('policy_scope_mismatch', 'scope'),
        'configuration',
        'policy_scope_invalid',
        True,
    ),
    (
        ModelPolicyError('secret_scope_denied', 'secret'),
        'configuration',
        'provider_secret_unavailable',
        True,
    ),
    (
        ModelPolicyError('team_required', 'team'),
        'configuration',
        'policy_scope_invalid',
        True,
    ),
    (ProviderSecretError('provider secret is disabled'), 'configuration', 'provider_secret_unavailable', True),
    (TimeoutError('slow'), 'infrastructure_transient', 'dependency_timeout', False),
    (ConnectionError('refused'), 'infrastructure_transient', 'dependency_unreachable', False),
    (OperationalError('db down'), 'infrastructure_transient', 'database_unavailable', False),
    (DatabaseError('db error'), 'infrastructure_transient', 'database_unavailable', False),
    (ValueError('boom'), 'unexpected', 'unexpected_exception', False),
    (RuntimeError('boom'), 'unexpected', 'unexpected_exception', False),
)


_TRANSLATION_IDS = [f'{row[1]}-{row[2]}-{index}' for index, row in enumerate(TRANSLATION_TABLE)]


@pytest.mark.parametrize(
    ('error', 'expected_class', 'expected_code', 'needs_fingerprint'),
    TRANSLATION_TABLE,
    ids=_TRANSLATION_IDS,
)
def test_translate_failure_maps_exact_sources(
    error: BaseException,
    expected_class: str,
    expected_code: str,
    needs_fingerprint: bool,
) -> None:
    fingerprint = HEX64 if needs_fingerprint else ''
    failure = _wf().translate_failure(error, configuration_fingerprint=fingerprint)

    assert failure.failure_class == expected_class
    assert failure.code == expected_code
    assert failure.configuration_fingerprint == fingerprint


def test_translate_failure_requires_fingerprint_for_configuration() -> None:
    with pytest.raises(ValueError):
        _wf().translate_failure(ModelPolicyError('model_policy_not_found', 'none'))


def test_translate_failure_ignores_fingerprint_for_non_configuration() -> None:
    failure = _wf().translate_failure(
        ModelPolicyError('provider_timeout', 'slow', retryable=True),
        configuration_fingerprint=HEX64,
    )

    assert failure.configuration_fingerprint == ''


def test_translate_failure_does_not_read_message_text() -> None:
    first = _wf().translate_failure(ModelPolicyError('provider_http_error', 'not found please retry', http_status=429))
    second = _wf().translate_failure(ModelPolicyError('provider_http_error', 'timeout unreachable', http_status=429))

    assert first.code == 'provider_rate_limited'
    assert second.code == 'provider_rate_limited'


def test_translate_failure_bounds_redacted_detail() -> None:
    failure = _wf().translate_failure(ValueError('x' * 5000))

    assert isinstance(failure.redacted_detail, str)
    assert len(failure.redacted_detail) <= 1024


BACKOFF_TABLE = (
    ('worker_lost', 1, 0),
    ('worker_lost', 5, 0),
    ('worker_lost', 100000, 0),
    ('infrastructure_transient', 1, 30),
    ('infrastructure_transient', 2, 60),
    ('infrastructure_transient', 6, 960),
    ('infrastructure_transient', 7, 1800),
    ('infrastructure_transient', 100, 1800),
    ('infrastructure_transient', 100000, 1800),
    ('provider_transient', 1, 30),
    ('provider_transient', 2, 60),
    ('provider_transient', 7, 1800),
    ('provider_transient', 100000, 1800),
    ('unexpected', 1, 300),
    ('unexpected', 2, 600),
    ('unexpected', 7, 19200),
    ('unexpected', 8, 21600),
    ('unexpected', 100000, 21600),
)


@pytest.mark.parametrize(('failure_class', 'streak', 'expected_seconds'), BACKOFF_TABLE)
def test_retry_backoff_math(failure_class: str, streak: int, expected_seconds: int) -> None:
    delay = _wf().retry_backoff(failure_class=failure_class, failure_streak=streak)

    assert delay == timedelta(seconds=expected_seconds)


@pytest.mark.parametrize('failure_class', ['configuration', 'invalid_input'])
def test_retry_backoff_rejects_non_retrying_classes(failure_class: str) -> None:
    with pytest.raises(ValueError):
        _wf().retry_backoff(failure_class=failure_class, failure_streak=1)


def test_retry_backoff_clamps_exponent_at_sixteen() -> None:
    clamped = _wf().retry_backoff(failure_class='unexpected', failure_streak=17)
    saturated = _wf().retry_backoff(failure_class='unexpected', failure_streak=100000)

    assert clamped == saturated == timedelta(seconds=21600)


# ---------------------------------------------------------------------------
# C2.1 Zone-D translator mapping for deterministic worker-boundary faults (RED)
#
# Pinned contract: MemoryWorkerError gains an optional keyword `code`. When a
# MemoryWorkerError raised by the digest/session validation boundary carries one
# of the invalid_input terminal codes, translate_failure classifies it as the
# terminal `invalid_input` class with that exact stable code (never bounded
# retry / unexpected). An uncoded MemoryWorkerError keeps classifying as
# unexpected.
# ---------------------------------------------------------------------------

INVALID_INPUT_TERMINAL_CODES = (
    'work_contract_invalid',
    'work_scope_invalid',
    'work_fingerprint_mismatch',
)


@pytest.mark.parametrize('code', INVALID_INPUT_TERMINAL_CODES)
def test_translate_failure_maps_worker_boundary_code_to_invalid_input(code: str) -> None:
    failure = _wf().translate_failure(MemoryWorkerError('deterministic boundary fault', code=code))

    assert failure.failure_class == 'invalid_input'
    assert failure.code == code
    assert failure.configuration_fingerprint == ''


@pytest.mark.parametrize('code', INVALID_INPUT_TERMINAL_CODES)
def test_translate_failure_worker_boundary_code_survives_configuration_fingerprint_kwarg(code: str) -> None:
    failure = _wf().translate_failure(
        MemoryWorkerError('deterministic boundary fault', code=code),
        configuration_fingerprint=HEX64,
    )

    assert failure.failure_class == 'invalid_input'
    assert failure.code == code
    assert failure.configuration_fingerprint == ''


def test_translate_failure_uncoded_worker_error_stays_unexpected() -> None:
    failure = _wf().translate_failure(MemoryWorkerError('opaque worker failure'))

    assert failure.failure_class == 'unexpected'
    assert failure.code == 'unexpected_exception'
