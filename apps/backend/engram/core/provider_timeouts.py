from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

FLEX_PROCESSING_CEILING_ENV = 'ENGRAM_FLEX_PROCESSING_CEILING'
FLEX_HTTP_TIMEOUT_ENV = 'ENGRAM_FLEX_HTTP_TIMEOUT'
PROVIDER_HTTP_TIMEOUT_ENV = 'ENGRAM_PROVIDER_HTTP_TIMEOUT'
EMBEDDING_HTTP_TIMEOUT_ENV = 'ENGRAM_EMBEDDING_HTTP_TIMEOUT'
LADDER_MARGIN_ENV = 'ENGRAM_TIMEOUT_LADDER_MARGIN'
TASK_SOFT_TIME_LIMIT_ENV = 'ENGRAM_TASK_SOFT_TIME_LIMIT'
TASK_TIME_LIMIT_ENV = 'ENGRAM_TASK_TIME_LIMIT'

_DEFAULT_FLEX_PROCESSING_CEILING = 600
_DEFAULT_PROVIDER_HTTP_TIMEOUT = 60
_DEFAULT_EMBEDDING_CALL_CEILING = 30
_DEFAULT_LADDER_MARGIN = 60

FLEX_PROCESSING_CEILING_SECONDS = int(
    os.environ.get(FLEX_PROCESSING_CEILING_ENV, str(_DEFAULT_FLEX_PROCESSING_CEILING))
)

OBSERVATION_PROCESSING = 'observation_processing'
SESSION_DISTILLATION = 'session_distillation'
DAILY_DIGEST = 'daily_digest'
WEEKLY_DIGEST = 'weekly_digest'
CANDIDATE_DECISION = 'candidate_decision'
MEMORY_EMBEDDING = 'memory_embedding'


@dataclass(frozen=True, slots=True)
class WorkTimeoutSpec:
    soft_env: str
    hard_env: str
    lease_env: str
    chat_calls: int
    embedding_calls: int


@dataclass(frozen=True, slots=True)
class WorkTimeouts:
    soft_time_limit: int
    time_limit: int
    lease_seconds: int


WORK_TIMEOUT_SPECS: dict[str, WorkTimeoutSpec] = {
    OBSERVATION_PROCESSING: WorkTimeoutSpec(
        soft_env='ENGRAM_OBSERVATION_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_OBSERVATION_TIME_LIMIT',
        lease_env='ENGRAM_OBSERVATION_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
    ),
    SESSION_DISTILLATION: WorkTimeoutSpec(
        soft_env='ENGRAM_DISTILL_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DISTILL_TIME_LIMIT',
        lease_env='ENGRAM_SESSION_LEASE_SECONDS',
        chat_calls=2,
        embedding_calls=0,
    ),
    DAILY_DIGEST: WorkTimeoutSpec(
        soft_env='ENGRAM_DIGEST_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DIGEST_TIME_LIMIT',
        lease_env='ENGRAM_DIGEST_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
    ),
    WEEKLY_DIGEST: WorkTimeoutSpec(
        soft_env='ENGRAM_DIGEST_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_DIGEST_TIME_LIMIT',
        lease_env='ENGRAM_DIGEST_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=0,
    ),
    CANDIDATE_DECISION: WorkTimeoutSpec(
        soft_env='ENGRAM_CANDIDATE_DECISION_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_CANDIDATE_DECISION_TIME_LIMIT',
        lease_env='ENGRAM_CANDIDATE_DECISION_LEASE_SECONDS',
        chat_calls=1,
        embedding_calls=1,
    ),
    MEMORY_EMBEDDING: WorkTimeoutSpec(
        soft_env='ENGRAM_EMBEDDING_SOFT_TIME_LIMIT',
        hard_env='ENGRAM_EMBEDDING_TIME_LIMIT',
        lease_env='ENGRAM_EMBEDDING_LEASE_SECONDS',
        chat_calls=0,
        embedding_calls=1,
    ),
}


def _seconds(env: Mapping[str, str] | None, name: str, default: int) -> int:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default

    return int(raw)


def ladder_margin(env: Mapping[str, str] | None = None) -> int:
    return _seconds(env, LADDER_MARGIN_ENV, _DEFAULT_LADDER_MARGIN)


def chat_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    flex_ceiling = _seconds(env, FLEX_PROCESSING_CEILING_ENV, _DEFAULT_FLEX_PROCESSING_CEILING)
    flex_timeout = _seconds(env, FLEX_HTTP_TIMEOUT_ENV, flex_ceiling)
    default_timeout = _seconds(env, PROVIDER_HTTP_TIMEOUT_ENV, _DEFAULT_PROVIDER_HTTP_TIMEOUT)

    return max(flex_timeout, default_timeout)


def embedding_call_ceiling(env: Mapping[str, str] | None = None) -> int:
    return _seconds(env, EMBEDDING_HTTP_TIMEOUT_ENV, _DEFAULT_EMBEDDING_CALL_CEILING)


def resolve_work_timeouts(work_type: str, env: Mapping[str, str] | None = None) -> WorkTimeouts:
    spec = WORK_TIMEOUT_SPECS[work_type]
    margin = ladder_margin(env)
    chat_seconds = spec.chat_calls * chat_call_ceiling(env)
    embedding_seconds = spec.embedding_calls * embedding_call_ceiling(env)
    soft_time_limit = _seconds(env, spec.soft_env, chat_seconds + embedding_seconds + margin)
    time_limit = _seconds(env, spec.hard_env, soft_time_limit + margin)
    lease_seconds = _seconds(env, spec.lease_env, time_limit + margin)

    return WorkTimeouts(soft_time_limit=soft_time_limit, time_limit=time_limit, lease_seconds=lease_seconds)


def chat_call_capacity(soft_time_limit: int, env: Mapping[str, str] | None = None) -> int:
    ceiling = chat_call_ceiling(env)
    if ceiling <= 0:
        return 0

    return max(soft_time_limit - ladder_margin(env), 0) // ceiling


def global_task_timeouts(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    margin = ladder_margin(env)
    soft_time_limit = _seconds(env, TASK_SOFT_TIME_LIMIT_ENV, chat_call_ceiling(env) + margin)
    time_limit = _seconds(env, TASK_TIME_LIMIT_ENV, soft_time_limit + margin)

    return soft_time_limit, time_limit


def required_stop_grace_seconds(
    env: Mapping[str, str] | None = None,
    *,
    worker_soft_shutdown_timeout: int,
) -> int:
    longest_hard_limit = max(resolve_work_timeouts(work_type, env).time_limit for work_type in WORK_TIMEOUT_SPECS)

    return max(longest_hard_limit, global_task_timeouts(env)[1]) + worker_soft_shutdown_timeout
