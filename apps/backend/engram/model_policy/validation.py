from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from engram.core.models import Project, ProjectTeam
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy, TaskType
from engram.model_policy.services import ProviderCallInput, get_provider_gateway

VALIDATION_PROMPT = 'engram_validate_policies health check: respond with a minimal completion.'
NO_PROJECT_AVAILABLE_ERROR_CODE = 'no_project_available'
VALIDATION_REQUEST_ID_PREFIX = 'engram_validate_policies:'

VALIDATION_HTTP_TIMEOUT_SECONDS = 15

_PROVIDER_SECRET_ERROR_CODE = 'provider_secret_unavailable'
_FALLBACK_ERROR_CODE = 'provider_error'
_RESPONSE_INVALID_ERROR_CODE = 'provider_response_invalid'
_VALIDATION_TIMEOUT_ERROR_CODE = 'validation_timeout'

_SANITIZED_ERROR_CODES = {
    'provider_http_error': 'provider_http_error',
    'provider_timeout': 'provider_timeout',
    'provider_unreachable': 'provider_unreachable',
    'provider_base_url_invalid': 'provider_base_url_invalid',
    'secret_scope_denied': 'secret_scope_denied',
    'team_required': 'team_required',
    'policy_scope_mismatch': 'policy_scope_mismatch',
    'model_policy_not_found': 'model_policy_not_found',
    NO_PROJECT_AVAILABLE_ERROR_CODE: NO_PROJECT_AVAILABLE_ERROR_CODE,
}

_PUBLIC_ERROR_MESSAGES = {
    'provider_http_error': 'The provider rejected the validation request.',
    'provider_timeout': 'The provider did not respond in time.',
    'provider_unreachable': 'The provider could not be reached.',
    'provider_base_url_invalid': 'The provider base URL is not permitted.',
    'secret_scope_denied': 'The provider secret is out of scope for this policy.',
    'team_required': 'This policy requires a team-scoped secret.',
    'policy_scope_mismatch': 'The policy scope is misconfigured.',
    'model_policy_not_found': 'The model policy could not be found.',
    NO_PROJECT_AVAILABLE_ERROR_CODE: 'No project is available to route this validation.',
    _PROVIDER_SECRET_ERROR_CODE: 'The provider secret is missing or disabled.',
    _FALLBACK_ERROR_CODE: 'The validation call failed.',
    _RESPONSE_INVALID_ERROR_CODE: 'The provider returned an unexpected response.',
    _VALIDATION_TIMEOUT_ERROR_CODE: 'The validation did not finish before the deadline.',
}


@dataclass(frozen=True)
class PolicyValidationResult:
    policy_id: str
    task_type: str
    provider: str
    model: str
    ok: bool
    latency_ms: int
    error_code: str | None = None
    public_error: str | None = None


def validate_policy(
    policy: ModelPolicy,
    *,
    gateway_factory: Callable[..., Any] | None = None,
    timeout: int = VALIDATION_HTTP_TIMEOUT_SECONDS,
) -> PolicyValidationResult:
    factory = gateway_factory or get_provider_gateway

    project = _resolve_validation_project(policy)
    if project is None:
        return _failure(policy, NO_PROJECT_AVAILABLE_ERROR_CODE, latency_ms=0)

    response_kind = 'candidates' if policy.task_type == TaskType.CURATION else 'single'
    request_id = f'{VALIDATION_REQUEST_ID_PREFIX}{policy.id}:{uuid.uuid4()}'
    started_at = time.monotonic()
    try:
        factory(policy, timeout=timeout).call(
            ProviderCallInput(
                organization_id=policy.organization_id,
                project_id=project.id,
                team_id=policy.team_id,
                policy=policy,
                request_id=request_id,
                trace_id=request_id,
                prompt=VALIDATION_PROMPT,
                response_kind=response_kind,
            ),
        )
    except ModelPolicyError as error:
        raw_code = error.error_code or _FALLBACK_ERROR_CODE

        return _failure(policy, _sanitize_error_code(raw_code), latency_ms=_elapsed_ms(started_at))
    except ProviderSecretError:
        return _failure(policy, _PROVIDER_SECRET_ERROR_CODE, latency_ms=_elapsed_ms(started_at))
    except (KeyError, IndexError, TypeError, ValueError):
        return _failure(policy, _RESPONSE_INVALID_ERROR_CODE, latency_ms=_elapsed_ms(started_at))

    return PolicyValidationResult(
        policy_id=str(policy.id),
        task_type=policy.task_type,
        provider=policy.provider,
        model=policy.model,
        ok=True,
        latency_ms=_elapsed_ms(started_at),
    )


def validation_timeout_failure(policy: ModelPolicy, *, latency_ms: int) -> PolicyValidationResult:
    return _failure(policy, _VALIDATION_TIMEOUT_ERROR_CODE, latency_ms=latency_ms)


def _sanitize_error_code(raw_code: str) -> str:
    return _SANITIZED_ERROR_CODES.get(raw_code, _FALLBACK_ERROR_CODE)


def _public_error_for(code: str) -> str:
    return _PUBLIC_ERROR_MESSAGES.get(code, _PUBLIC_ERROR_MESSAGES[_FALLBACK_ERROR_CODE])


def _failure(policy: ModelPolicy, code: str, *, latency_ms: int) -> PolicyValidationResult:
    return PolicyValidationResult(
        policy_id=str(policy.id),
        task_type=policy.task_type,
        provider=policy.provider,
        model=policy.model,
        ok=False,
        latency_ms=latency_ms,
        error_code=code,
        public_error=_public_error_for(code),
    )


def _resolve_validation_project(policy: ModelPolicy) -> Project | None:
    if policy.project_id:
        return policy.project

    if policy.team_id:
        project_team = (
            ProjectTeam.objects.filter(organization_id=policy.organization_id, team_id=policy.team_id)
            .select_related('project')
            .first()
        )
        if project_team is not None:
            return project_team.project

    return Project.objects.filter(organization_id=policy.organization_id).first()


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
