# Backend Contracts

## Stack Decision

V1 uses:

- Django;
- Django REST Framework;
- PostgreSQL;
- RabbitMQ-compatible broker;
- Redis result/cache backend;
- Celery;
- Poetry;
- Ruff;
- pytest and pytest-django;
- structlog;
- Sentry.

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

### Product-Domain Progress

The transport prohibition does not prohibit durable product-domain progress.
Engram may own one scoped logical-work record describing which immutable input
still requires processing and its explicit product disposition.

`WorkflowWork` is the stable logical requirement. `WorkflowRun` remains
append-only attempt/history and provider/result provenance.
`django-celery-outbox` remains the sole transport authority and the only owner
of publication, transport retries, dead letters, and relay behavior.

Logical work never mirrors broker status or polls transport as its source of
truth. Reconciliation starts from organization/project-scoped domain
invariants and emits stable-id tasks through the package-backed Celery
boundary. Operational work state, candidate state, and memory temporal
validity are separate.

Current hook ingest violates the existing atomic enqueue contract: it registers
`.delay()` through `transaction.on_commit()`, leaving a process-death window
after evidence commit and before package-row creation. CP1 replaces that
characterization with one transaction containing evidence, logical work, and
the package row.

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

## Project Routing Contract

Every agent-facing endpoint that reads or writes tenant-scoped memory data
(hooks ingest, context bundles, search, observations list/detail, memory
feedback/version/links/diff) resolves its target project through one shared,
scope-enforcing resolver (`resolve_project_for_scope` in
`engram/core/repository.py`), not per-view logic.

Rules:

- request contract: `project_id` (UUID) and `repository_url` (string,
  optional, at most 1024 characters) are both accepted; `project_id` wins
  when both are present; at least one is required or the request is denied
  with `400 project_or_repository_required`;
- resolution always happens inside the caller's own organization - a
  `repository_url` can never resolve a project belonging to another
  organization, regardless of whether that project's repository URL
  collides;
- membership guard, binding wins over capability: a request is authorized
  only if the resolved project is already in the caller's scoped
  `project_ids`, or the caller is an **unbound** API-key scope
  (`project_bound=False`) carrying the `projects:agent` capability (the
  branch that admits a project newly auto-created by this same call). A
  **project-bound** key never takes the capability branch, even if it was
  (mis)granted `projects:agent` - its binding is the sole rule. Console and
  session scopes never take the capability branch either. Failing the guard
  raises `403 project_scope_denied` and writes a DENIED audit event carrying
  the resolved project id;
- resolve-only vs. resolve-or-create: hooks ingest, context bundles, and
  search may auto-create a project for an unmatched `repository_url`
  (`allow_create=True`), but only take the create path when the caller
  already holds the unbound `projects:agent` branch above - a project-bound
  key, or an unbound key without the capability, gets `404 project_not_found`
  instead of a side-effecting create. Observations (list/detail) and memory
  mutations/reads (feedback, version, links, diff) are resolve-only: an
  unmatched `repository_url` always returns `404 project_not_found`, with no
  project created as a read/write side effect;
- client-provided `project_id`/`repository_url` values are selection, never
  authorization - the resolver only narrows within the scope
  `resolve_request_scope` already computed from the API key or session; no
  value on either field can expand organization/team/project scope.

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
- DeepSeek generation.
- OpenAI embeddings.

Provider calls record provider, model, policy version, tenant/team/project,
request id, trace id, token usage, latency, cost metadata when available, and
redaction state. Provider selection must be testable without changing hook
adapters.

## Secret Storage Contract

Provider secrets are stored using a single database envelope (Fernet
symmetric encryption); `SecretStorageMode.EXTERNAL_VAULT` exists as a schema
choice with no adapter implementation behind it. No raw secret appears in
audit, logs, traces, or frontend responses.

Database envelope mode defines key version, ciphertext, HMAC, and audit
events, using one symmetric key derived from `ENGRAM_SECRET_ENCRYPTION_KEY`
for all secrets, not a separate KEK/DEK wrapping hierarchy.
