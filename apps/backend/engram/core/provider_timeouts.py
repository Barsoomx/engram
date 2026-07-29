from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

FLEX_TIMEOUT_SIZING_ENV = 'ENGRAM_FLEX_TIMEOUT_SIZING'
FLEX_PROCESSING_CEILING_ENV = 'ENGRAM_FLEX_PROCESSING_CEILING'
FLEX_HTTP_TIMEOUT_ENV = 'ENGRAM_FLEX_HTTP_TIMEOUT'
PROVIDER_HTTP_TIMEOUT_ENV = 'ENGRAM_PROVIDER_HTTP_TIMEOUT'
EMBEDDING_HTTP_TIMEOUT_ENV = 'ENGRAM_EMBEDDING_HTTP_TIMEOUT'
TASK_SOFT_TIME_LIMIT_ENV = 'ENGRAM_TASK_SOFT_TIME_LIMIT'
TASK_TIME_LIMIT_ENV = 'ENGRAM_TASK_TIME_LIMIT'

_FLEX_SIZED_PROCESSING_CEILING = 600
_DEFAULT_PROVIDER_HTTP_TIMEOUT = 60
_DEFAULT_EMBEDDING_CALL_CEILING = 30
_ENABLED_VALUES = frozenset({'1', 'true', 'yes', 'on'})

OBSERVATION_PROCESSING = 'observation_processing'
SESSION_DISTILLATION = 'session_distillation'
DAILY_DIGEST = 'daily_digest'
WEEKLY_DIGEST = 'weekly_digest'
CANDIDATE_DECISION = 'candidate_decision'
MEMORY_EMBEDDING = 'memory_embedding'


@dataclass(frozen=True, slots=True)
class WorkTimeouts:
    soft_time_limit: int
    time_limit: int
    lease_seconds: int


@dataclass(frozen=True, slots=True)
class WorkTimeoutSpec:
    soft_env: str
    hard_env: str
    lease_env: str
    chat_calls: int
    embedding_calls: int
    baseline: WorkTimeouts


WORK_TIMEOUT_SPECS: dict[str, WorkTimeoutSpec] = {
    OBSERVATION_PROCESSING: WorkTimeoutSpec(
        soft_env='ENGRAM_OBSERVATION_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_OBSERVATION_TIME_LIMIT',
        lease_env='ENGRAM_OBSERVATION_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
        baseline=WorkTimeouts(soft_time_limit=60, time_limit=90, lease_seconds=120),
    ),
    SESSION_DISTILLATION: WorkTimeoutSpec(
        soft_env='ENGRAM_DISTILL_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DISTILL_TIME_LIMIT',
        lease_env='ENGRAM_SESSION_LEASE_SECONDS',
        chat_calls=2,
        embedding_calls=0,
        baseline=WorkTimeouts(soft_time_limit=600, time_limit=660, lease_seconds=720),
    ),
    DAILY_DIGEST: WorkTimeoutSpec(
        soft_env='ENGRAM_DIGEST_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DIGEST_TIME_LIMIT',
        lease_env='ENGRAM_DIGEST_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
        baseline=WorkTimeouts(soft_time_limit=180, time_limit=210, lease_seconds=240),
    ),
    WEEKLY_DIGEST: WorkTimeoutSpec(
        soft_env='ENGRAM_DIGEST_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DIGEST_TIME_LIMIT',
        lease_env='ENGRAM_DIGEST_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
        baseline=WorkTimeouts(soft_time_limit=180, time_limit=210, lease_seconds=240),
    ),
    CANDIDATE_DECISION: WorkTimeoutSpec(
        soft_env='ENGRAM_CANDIDATE_DECISION_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_CANDIDATE_DECISION_TIME_LIMIT',
        lease_env='ENGRAM_CANDIDATE_DECISION_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=1,
        baseline=WorkTimeouts(soft_time_limit=240, time_limit=270, lease_seconds=300),
    ),
    MEMORY_EMBEDDING: WorkTimeoutSpec(
        soft_env='ENGRAM_EMBEDDING_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_EMBEDDING_TIME_LIMIT',
        lease_env='ENGRAM_EMBEDDING_LEASE_SECONDS',
        chat_calls=0,
        embedding_calls=1,
        baseline=WorkTimeouts(soft_time_limit=180, time_limit=210, lease_seconds=300),
    ),
}

_GLOBAL_TASK_BASELINE = WorkTimeouts(soft_time_limit=120, time_limit=180, lease_seconds=180)
_GLOBAL_TASK_CHAT_CALLS = 1


def _seconds(env: Mapping[str, str] | None, name: str, default: int) -> int:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default

    return int(raw)


def flex_timeout_sizing_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env

    return str(source.get(FLEX_TIMEOUT_SIZING_ENV, '')).strip().lower() in _ENABLED_VALUES


def flex_processing_ceiling(env: Mapping[str, str] | None = None) -> int:
    if flex_timeout_sizing_enabled(env):
        return _FLEX_SIZED_PROCESSING_CEILING

    return provider_call_ceiling(env)


def provider_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    return _seconds(env, PROVIDER_HTTP_TIMEOUT_ENV, _DEFAULT_PROVIDER_HTTP_TIMEOUT)


def flex_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    ceiling = _seconds(env, FLEX_PROCESSING_CEILING_ENV, flex_processing_ceiling(env))

    return _seconds(env, FLEX_HTTP_TIMEOUT_ENV, ceiling)


def chat_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    return max(flex_call_ceiling(env), provider_call_ceiling(env))


def embedding_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    return _seconds(env, EMBEDDING_HTTP_TIMEOUT_ENV, _DEFAULT_EMBEDDING_CALL_CEILING)


FLEX_PROCESSING_CEILING_SECONDS = flex_processing_ceiling()


def hosted_provider_seconds(work_type: str, env: Mapping[str, str] | None = None) -> int:
    spec = WORK_TIMEOUT_SPECS[work_type]

    return spec.chat_calls * chat_call_ceiling(env) + spec.embedding_calls * embedding_call_ceiling(env)


def _socket_growth(spec: WorkTimeoutSpec, env: Mapping[str, str] | None) -> int:
    chat_growth = max(chat_call_ceiling(env) - _DEFAULT_PROVIDER_HTTP_TIMEOUT, 0)
    embedding_growth = max(embedding_call_ceiling(env) - _DEFAULT_EMBEDDING_CALL_CEILING, 0)

    return spec.chat_calls * chat_growth + spec.embedding_calls * embedding_growth


def resolve_work_timeouts(work_type: str, env: Mapping[str, str] | None = None) -> WorkTimeouts:
    spec = WORK_TIMEOUT_SPECS[work_type]
    baseline = spec.baseline
    soft_time_limit = _seconds(env, spec.soft_env, baseline.soft_time_limit + _socket_growth(spec, env))
    time_limit = _seconds(env, spec.hard_env, soft_time_limit + baseline.time_limit - baseline.soft_time_limit)
    lease_seconds = _seconds(env, spec.lease_env, time_limit + baseline.lease_seconds - baseline.time_limit)

    return WorkTimeouts(soft_time_limit=soft_time_limit, time_limit=time_limit, lease_seconds=lease_seconds)


def global_task_timeouts(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    baseline = _GLOBAL_TASK_BASELINE
    growth = _GLOBAL_TASK_CHAT_CALLS * max(chat_call_ceiling(env) - _DEFAULT_PROVIDER_HTTP_TIMEOUT, 0)
    soft_time_limit = _seconds(env, TASK_SOFT_TIME_LIMIT_ENV, baseline.soft_time_limit + growth)
    time_limit = _seconds(env, TASK_TIME_LIMIT_ENV, soft_time_limit + baseline.time_limit - baseline.soft_time_limit)

    return soft_time_limit, time_limit


def required_stop_grace_seconds(
    env: Mapping[str, str] | None = None,
    *,
    worker_soft_shutdown_timeout: int,
) -> int:
    longest_hard_limit = max(resolve_work_timeouts(work_type, env).time_limit for work_type in WORK_TIMEOUT_SPECS)

    return max(longest_hard_limit, global_task_timeouts(env)[1]) + worker_soft_shutdown_timeout
