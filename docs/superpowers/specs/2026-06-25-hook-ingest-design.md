# Hook Ingest Design

## Goal

Add the first authenticated hook API surface: a dry-run endpoint for client
verification and durable observation ingest endpoints for post-tool-use and
session-end hook events.

This slice is backend-only. It does not add CLI `connect`, context retrieval,
session-start context injection, memory candidate generation, worker handlers,
provider calls, retry envelopes, plugin packages, or frontend screens.

## Current Gate

The current roadmap item is "Add hook dry-run and observation ingest." The
previous checkpoint added database-backed API keys and effective scope
resolution. The next parity step needs a server endpoint that a thin hook client
can call to prove credentials and then submit observed activity into PostgreSQL.

The hard parity gate requires one hook/client path to submit session-start,
observation/tool-use/error/decision events to the Django API. This checkpoint
implements the observation side of that path and the dry-run verification
needed by `connect`/`doctor`; session-start context retrieval remains a later
checkpoint.

## Approaches Considered

### Dedicated Hooks App

Create `engram.hooks` for HTTP serializers/views and hook ingest services,
depending on `engram.access` for authorization and `engram.core` for durable
records.

Tradeoff: one more app, but it keeps transport-specific hook behavior out of
the access and memory models. This gives future CLI/plugin packages one stable
API contract.

### Put Views In Core

Add DRF views directly beside the core models.

Tradeoff: fewer files today, but it mixes transport validation with persistence
models and makes future hook/client compatibility tests harder to isolate.

### Generic `/v1/events`

Accept every hook lifecycle event through one generic endpoint.

Tradeoff: compact, but the documented hook protocol names
`/v1/hooks/dry-run`, `/v1/hooks/post-tool-use`, and `/v1/hooks/session-end`.
Explicit endpoints make client error mapping and agent contract tests clearer.

## Decision

Create `engram.hooks` with:

- `HookDryRunView` at `POST /v1/hooks/dry-run`;
- `PostToolUseView` at `POST /v1/hooks/post-tool-use`;
- `SessionEndView` at `POST /v1/hooks/session-end`;
- request serializers for dry-run and hook events;
- one domain service for dry-run scope verification;
- one domain service for durable hook-event ingest.

All views authenticate using `Authorization: Bearer <api-key>`. The raw key is
never stored in models, audit rows, responses, logs, or outbox payloads. Hook
payload and observation text are redacted before persistence for obvious
secret-bearing keys and token-shaped values.

## Request Contract

Dry-run request:

```json
{
  "project_id": "uuid",
  "team_id": "uuid-or-null",
  "agent_runtime": "codex",
  "agent_version": "0.0.0",
  "request_id": "client-request-id"
}
```

Hook event request:

```json
{
  "project_id": "uuid",
  "team_id": "uuid-or-null",
  "agent_runtime": "codex",
  "agent_version": "0.0.0",
  "agent_external_id": "local-agent-id",
  "session_id": "external-session-id",
  "event_id": "client-event-id",
  "idempotency_key": "stable-client-key",
  "event_type": "post_tool_use",
  "payload_schema_version": "v1",
  "sequence_number": 1,
  "occurred_at": "2026-06-25T00:00:00Z",
  "content_hash": "sha256-or-client-content-hash",
  "repository_url": "https://example/repo.git",
  "repository_root": "/workspace/repo",
  "branch": "main",
  "cwd": "/workspace/repo",
  "payload": {
    "tool_name": "bash",
    "tool_input": {"command": "pytest"},
    "tool_response": {"exit_code": 0}
  },
  "observation": {
    "type": "tool_use",
    "title": "bash completed",
    "body": "pytest exited 0",
    "files_read": [],
    "files_modified": []
  }
}
```

The `payload` field must be a JSON object. The server treats `project_id`,
`team_id`, and all scope fields as hints. The effective authorization scope
comes from the API key resolver. If a key is bound to one team and the request
omits `team_id`, accepted durable rows use the key-bound team.
The `observation` object is optional. Thin hooks may send only raw tool/session
metadata; the server creates a deterministic observation shell when no
normalized observation is supplied.

## Response Contract

Dry-run success returns:

```json
{
  "status": "ok",
  "request_id": "client-request-id",
  "resolved_actor": {"type": "api_key", "id": "uuid"},
  "scope": {
    "organization_id": "uuid",
    "project_ids": ["uuid"],
    "team_ids": ["uuid"],
    "capabilities": ["observations:write"]
  },
  "server": {"health": "ok"}
}
```

Ingest success returns:

```json
{
  "status": "accepted",
  "duplicate": false,
  "request_id": "client-request-id",
  "raw_event_id": "uuid",
  "observation_id": "uuid",
  "outbox_event_id": "uuid",
  "agent_session_id": "uuid"
}
```

Duplicate submissions with the same project/idempotency key or session/event id
return `duplicate: true` and the existing durable ids. They must not create new
observations or outbox entries.

Error responses use stable codes:

- `missing_api_key` -> HTTP 401;
- `invalid_key` -> HTTP 401;
- `inactive_key`, `revoked_key`, `expired_key`, `inactive_owner` -> HTTP 403;
- `missing_capability`, `project_scope_denied`, `team_scope_denied` -> HTTP 403;
- serializer validation errors -> HTTP 400.

## Domain Service Behavior

`VerifyHookDryRun.execute()` resolves the API key with
`observations:write`, requested project/team hints, and target type
`hook_dry_run`. It returns an already authorized `EffectiveScope`.

`IngestHookEvent.execute()`:

1. resolves API-key scope with required capability `observations:write`;
2. rejects requested projects/teams outside the resolved scope;
3. derives durable team ownership from the resolved scope when omitted by the
   request;
4. redacts secret-shaped hook payload and observation fields before persistence;
5. creates or updates an `Agent` for runtime/external id;
6. creates or updates an `AgentSession` for project/session id;
7. writes `RawEventEnvelope`;
8. writes a normalized `Observation`;
9. writes an `ObservationSource` linking the observation to the raw event;
10. writes an `OutboxEvent` for later worker processing;
11. returns existing durable rows on duplicate idempotency without duplicate
   side effects.

The transaction boundary covers raw event, observation, source, and outbox
write. If a concurrent duplicate insert reaches a database uniqueness
constraint first, the service reloads the existing rows and returns
`duplicate: true`. The API never relies on Celery delivery as proof of
acceptance.

## Observation Normalization

This checkpoint does not call a model provider. It stores a deterministic
observation shell from accepted hook payloads:

- if supplied, `observation.type` -> `Observation.observation_type`;
- if supplied, `observation.title` -> `Observation.title`;
- if supplied, `observation.body` -> `Observation.body`;
- if supplied, `observation.files_read` and `observation.files_modified` ->
  matching JSON fields;
- missing title falls back to event type and tool name.

The later worker checkpoint may refine observations and generate memory
candidates, but it must preserve raw event provenance and idempotency.

## Boundaries

This slice owns:

- hook endpoint URLs;
- request/response shape for dry-run and durable ingest;
- API-key authorization integration;
- session/agent creation for accepted events;
- raw event, observation, observation source, and outbox writes;
- duplicate replay behavior.

This slice defers:

- `/v1/context/session-start`;
- retrieval and context bundle assembly;
- CLI `connect`, `doctor`, `disconnect`;
- Claude Code/Codex adapter packages;
- request signatures and managed hook trust signing;
- worker handlers and memory candidate generation;
- provider calls, embeddings, semantic search, and context packing;
- offline retry envelope storage.

## Testing

Tests must prove behavior:

- dry-run resolves scope and never echoes raw credentials;
- missing/invalid/denied API keys return stable errors;
- post-tool-use creates agent, session, raw event, observation, source, and
  outbox in one accepted request;
- post-tool-use accepts a thin payload without `observation` and creates a
  deterministic observation shell server-side;
- hook payload and observation text redact obvious API keys, bearer tokens,
  provider keys, secrets, and passwords before persistence;
- key-bound team scope is preserved when the request omits `team_id`;
- duplicate idempotency returns existing ids without new durable rows;
- same session/event id replay is duplicate-safe;
- database uniqueness races on replay return existing rows instead of HTTP 500;
- cross-project and cross-team requests are denied before any event rows are
  written;
- malformed payloads and non-object `payload` values return HTTP 400;
- session-end marks the session ended and writes a durable observation/outbox;
- audit/outbox payloads do not include raw API keys.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest engram/hooks/hook_ingest_tests.py -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings`
- `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`

Docker Compose smoke remains blocked until Docker is available in this WSL
distro.

## Self-Review

- No North Star expansion: retrieval, context APIs, frontend, MCP, and plugin
  packaging remain deferred.
- No local worker regression: clients call the server and never write local
  memory stores.
- No raw credential persistence: API key material is only used for resolver
  lookup.
- No provider calls: observations are deterministic records from accepted hook
  payloads.
- No silent duplicates: idempotency returns existing durable ids.
