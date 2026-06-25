# Backend Contracts

## Stack Decision

V1 uses:

- Django;
- Django REST Framework;
- PostgreSQL;
- Redis-compatible broker;
- Celery;
- Poetry;
- Ruff;
- pytest and pytest-django;
- structlog;
- Sentry and OpenTelemetry.

Any deviation from this stack needs a decision record before implementation.

## Domain Service Contract

Views, hook adapters, and Celery tasks are transport boundaries. Business logic
belongs in domain services.

Each service exposes one public `execute()` boundary:

- typed input DTO;
- typed output DTO;
- explicit actor and scope context;
- domain error taxonomy with HTTP/status mapping;
- transaction boundary documented at the service level;
- structured log context bound at entry;
- trace/span created for the service call;
- asynchronous follow-up work queued through the approved transaction-safe
  transport before commit completes.

Adapters must not bypass domain services for writes.

## Async Transport

Current V1 uses `django-celery-outbox` as the reliable bridge from
transactional writes to asynchronous work. Engram does not own a parallel
transport model, status enum, polling command, or relay in the live runtime.

Rules:

- domain writes and Celery task enqueue happen inside the same database
  transaction;
- task payloads contain stable ids only, not API keys, provider secrets, prompt
  bodies, or raw tool output;
- `django-celery-outbox` owns the transport tables, retry/dead-letter state,
  and `celery_outbox_relay` command;
- Engram workers load authoritative domain rows by id and must be idempotent;
- schema changes include migration and replay tests.

A future Engram-owned domain-event stream requires a separate decision record
and must not be introduced as a weaker replacement for the package transport.

## RBAC Source Of Truth

Roles, capabilities, grants, API keys, and scope bindings are database-backed.
Role presets are seed data only.

Rules:

- migrations seed default roles and capabilities;
- migrations are reversible where possible and covered by tests;
- API keys only narrow owner access;
- every sensitive success and deny writes an audit record;
- audit records include request id, actor, target resource, scope filters,
  capability checked, missing capability on deny, and result.

## Observability Fields

Every API request, hook event, service call, queued task, worker job, and
provider call propagates:

- request id;
- trace id;
- span id;
- tenant/organization/team/project ids;
- actor type/id;
- hook event id;
- idempotency key;
- Celery task id when queued;
- worker job id;
- provider call id when present.

Forbidden in logs and traces:

- raw provider secrets;
- API keys and agent tokens;
- prompt bodies unless tenant policy explicitly enables capture;
- memory content marked sensitive;
- unredacted tool output that matches secret patterns.

## Model Provider Contract

Memory generation, digesting, curation, and embeddings use provider adapters
resolved by model policy.

V1 adapters:

- Anthropic generation.
- OpenAI generation.
- OpenAI embeddings.

Provider calls record provider, model, policy version, tenant/team/project,
request id, trace id, token usage, latency, cost metadata when available, and
redaction state. Provider selection must be testable without changing hook
adapters.

## Vault Adapter Contract

Vault-compatible storage must support:

- tenant-scoped path prefixes;
- configurable auth mode;
- read and write roles separated;
- KV v2 style versioning or equivalent;
- CAS/create-if-absent semantics for seed/create;
- retries with bounded timeout;
- rotation state;
- dependent health invalidation after rotation;
- no raw secret in audit, logs, traces, or frontend responses.

Database envelope mode must define KEK/DEK hierarchy, key version, ciphertext,
HMAC, rotation state, and audit events.
