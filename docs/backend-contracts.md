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
- domain events emitted through the durable outbox before commit completes.

Adapters must not bypass domain services for writes.

## Durable Outbox

The outbox is the reliable bridge from transactional writes to asynchronous
work.

Required columns:

- id;
- tenant id;
- event type;
- payload JSON;
- payload version;
- idempotency key;
- actor type/id;
- organization/team/project ids when applicable;
- correlation id;
- trace id;
- status: pending, processing, done, failed, dead_letter;
- attempts;
- next retry at;
- locked by;
- locked at;
- last error;
- created at;
- updated at.

Rules:

- domain write and outbox insert happen in the same database transaction;
- idempotency key is unique per event type and source;
- dispatcher uses row locking with skip-locked semantics;
- retries use bounded exponential backoff;
- dead-letter records stay queryable and replayable by an admin action;
- event handlers are idempotent;
- schema changes include migration and replay tests.

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

Every API request, hook event, service call, outbox event, worker job, and
provider call propagates:

- request id;
- trace id;
- span id;
- tenant/organization/team/project ids;
- actor type/id;
- hook event id;
- idempotency key;
- outbox event id;
- worker job id;
- provider call id when present.

Forbidden in logs and traces:

- raw provider secrets;
- API keys and agent tokens;
- prompt bodies unless tenant policy explicitly enables capture;
- memory content marked sensitive;
- unredacted tool output that matches secret patterns.

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
