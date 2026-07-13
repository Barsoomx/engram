import re
from dataclasses import dataclass
from datetime import timedelta

from django.db.utils import DatabaseError

from engram.model_policy.errors import ModelPolicyError, ProviderSecretError

WORKER_LOST = 'worker_lost'
INFRASTRUCTURE_TRANSIENT = 'infrastructure_transient'
PROVIDER_TRANSIENT = 'provider_transient'
CONFIGURATION = 'configuration'
INVALID_INPUT = 'invalid_input'
UNEXPECTED = 'unexpected'

_CODE_PATTERN = re.compile(r'[a-z0-9_]{1,128}')
_HEX64_PATTERN = re.compile(r'[0-9a-f]{64}')
_MAX_DETAIL = 1024

_BACKOFF = {
    WORKER_LOST: (0, 0),
    INFRASTRUCTURE_TRANSIENT: (30, 1800),
    PROVIDER_TRANSIENT: (30, 1800),
    UNEXPECTED: (300, 21600),
}

_MODEL_POLICY_CODE_MAP = {
    'provider_timeout': (PROVIDER_TRANSIENT, 'provider_timeout'),
    'provider_unreachable': (PROVIDER_TRANSIENT, 'provider_unreachable'),
    'model_policy_not_found': (CONFIGURATION, 'model_policy_unavailable'),
    'policy_scope_mismatch': (CONFIGURATION, 'policy_scope_invalid'),
    'team_required': (CONFIGURATION, 'policy_scope_invalid'),
    'secret_scope_denied': (CONFIGURATION, 'provider_secret_unavailable'),
}


@dataclass(frozen=True, slots=True)
class ClassifiedWorkFailure:
    failure_class: str
    code: str
    redacted_detail: str = ''
    configuration_fingerprint: str = ''

    def __post_init__(self) -> None:
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError(f'invalid failure code {self.code!r}')

        if self.failure_class == CONFIGURATION:
            if _HEX64_PATTERN.fullmatch(self.configuration_fingerprint) is None:
                raise ValueError('configuration failure requires a lowercase 64-hex fingerprint')

        elif self.configuration_fingerprint != '':
            raise ValueError('non-configuration failure must not carry a configuration fingerprint')

        return


def _classify_http(http_status: int | None) -> tuple[str, str]:
    if http_status in (401, 402, 403):
        return CONFIGURATION, 'provider_account_unavailable'

    if http_status == 404:
        return CONFIGURATION, 'provider_endpoint_invalid'

    if http_status == 408:
        return PROVIDER_TRANSIENT, 'provider_timeout'

    if http_status == 429:
        return PROVIDER_TRANSIENT, 'provider_rate_limited'

    if http_status == 425 or (http_status is not None and 500 <= http_status <= 599):
        return PROVIDER_TRANSIENT, 'provider_unavailable'

    return INVALID_INPUT, 'provider_request_invalid'


def _classify_model_policy(error: ModelPolicyError) -> tuple[str, str]:
    if error.code == 'provider_http_error':
        return _classify_http(error.http_status)

    mapping = _MODEL_POLICY_CODE_MAP.get(error.code)
    if mapping is not None:
        return mapping

    return UNEXPECTED, 'unexpected_exception'


def _classify(error: BaseException) -> tuple[str, str]:
    if isinstance(error, ModelPolicyError):
        return _classify_model_policy(error)

    if isinstance(error, ProviderSecretError):
        return CONFIGURATION, 'provider_secret_unavailable'

    if isinstance(error, TimeoutError):
        return INFRASTRUCTURE_TRANSIENT, 'dependency_timeout'

    if isinstance(error, ConnectionError):
        return INFRASTRUCTURE_TRANSIENT, 'dependency_unreachable'

    if isinstance(error, DatabaseError):
        return INFRASTRUCTURE_TRANSIENT, 'database_unavailable'

    return UNEXPECTED, 'unexpected_exception'


def translate_failure(error: BaseException, *, configuration_fingerprint: str = '') -> ClassifiedWorkFailure:
    failure_class, code = _classify(error)
    fingerprint = configuration_fingerprint if failure_class == CONFIGURATION else ''

    return ClassifiedWorkFailure(
        failure_class=failure_class,
        code=code,
        redacted_detail=str(error)[:_MAX_DETAIL],
        configuration_fingerprint=fingerprint,
    )


def retry_backoff(*, failure_class: str, failure_streak: int) -> timedelta:
    if failure_class not in _BACKOFF:
        raise ValueError(f'{failure_class} is not a retrying failure class')

    base_seconds, cap_seconds = _BACKOFF[failure_class]
    delay = min(cap_seconds, base_seconds * 2 ** min(failure_streak - 1, 16))

    return timedelta(seconds=delay)
