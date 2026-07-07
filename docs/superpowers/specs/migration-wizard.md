# claude-mem Migration Wizard — Server Batch-Ingest API (M1)

## Goal

Let an operator move an existing local `claude-mem` store into an Engram project
without shipping the raw SQLite file to the server. The CLI reads the local
store, streams already-aliased rows table-by-table over authenticated HTTP, and
the server applies them through the same importer core the management command
uses. This document owns the M1 server contract; M2 (CLI streamer), M3
(frontend wizard), and M4 (console page) consume it.

## Design Principles

- CLI-first streaming: the client owns archive/SQLite handling. The server never
  reads a `.db` file, never opens an archive, and never touches the filesystem
  for import input. It receives plain JSON row dicts.
- Reuse, don't fork: the row-level normalize/redact/validate/promote logic is the
  security-reviewed `ClaudeMemImporter` core. The batch endpoint feeds in-memory
  row dicts into the same per-row methods; the management command keeps feeding
  rows from SQLite into that identical core. Idempotency, redaction, and
  validation semantics are unchanged.
- Tenant pinning via key scope: the org is fixed by the API key. The project is
  resolved with `resolve_project_for_scope` from an explicit `project_id`, then
  authorized exactly like the other data-plane services (hooks/search/context).
- Per-batch transactions: each batch applies in one `transaction.atomic()`.
  A batch either fully applies and records its result, or raises and rolls back.
- Deferred embedding: batch promotion indexes `RetrievalDocument`s without a
  synchronous provider embedding call. The existing
  `reembed_missing_embeddings` beat task backfills vectors. This keeps ingest
  fast and provider-independent; retrieval is eventually consistent.

## Wire Contract

Data-plane auth: Bearer API key (same as hooks/search), capability
`memories:admin`. Project routing via explicit `project_id`.

1. `POST /v1/imports/claude-mem`
   - body: `{project_id, source_store_id, manifest: {schema_version_head,
     tables: {sdk_sessions, user_prompts, observations, session_summaries}}}`
   - `201 {import_id, status: "created"}`
   - `409` if another non-terminal job exists for (org, project,
     source_store_id).

2. `POST /v1/imports/claude-mem/{import_id}/batches`
   - body: `{seq (0-based, strictly increasing), table, rows: [ {raw col->value,
     v17-aliased by client} ]}`
   - `200 {accepted: true, seq, created, duplicates, skipped}`
   - Caps: max 200 rows/batch, max 2MB request body.
   - Idempotent: replaying an already-applied `(import_id, seq)` returns the
     recorded result without re-applying.
   - One DB transaction per batch.

3. `POST /v1/imports/claude-mem/{import_id}/finalize`
   - body: `{client_row_counts: {table: int}}`
   - `200 {status: "succeeded", report: {counts, created, duplicates,
     unsupported, warnings, redactions, truncations}}`
   - Marks the job terminal, emits audit.

4. `GET /v1/imports/claude-mem/{import_id}`
   - `200 {status, progress: {batches_applied, rows_created, rows_duplicate},
     report?}`

### Ordering

The client streams `sdk_sessions` fully first, then `user_prompts`,
`observations`, `session_summaries`. The server enforces a monotonic table-phase
check: table order index is `sdk_sessions=0, user_prompts=1, observations=2,
session_summaries=3`; a batch whose table phase is earlier than the highest
phase already started is rejected. Same-phase batches (continuing a table) are
allowed.

### source_id / idempotency

The server builds source ids exactly as the importer does:
`claude-mem:{source_store_id}:{kind}:{...}`. Re-running a batch or re-importing a
store is idempotent through the existing `ObservationSource`/`RawEventEnvelope`
uniqueness plus the per-seq `applied_batches` replay ledger.

### Promotion confidence

- `observations` promote at `Decimal('0.700')`.
- `session_summaries` promote at `Decimal('0.800')`.
- `user_prompts` remain raw-events only (no promotion).
- `sdk_sessions` create `Agent` + `AgentSession` only.

## ImportJob Lifecycle

`ImportJob` lives in the `imports` app.

- `created` — job row exists, no batches applied yet.
- `receiving` — at least one batch applied, not finalized.
- `succeeded` — finalized; terminal.
- `failed` — explicit failure; terminal.
- `expired` — abandoned/swept; terminal (reserved for a future sweep).

Fields: org/project/team FKs, `source_store_id`, `status`, `manifest`,
progress (`batches_applied`, `rows_created`, `rows_duplicate`), `last_batch_seq`
and `max_table_phase` (ordering/replay control), `applied_batches`
(seq -> recorded result, for idempotent replay), `report`, `failure_reason`,
`created_by_api_key`/`created_by_identity` attribution, timestamps.

Constraint: a partial unique index guarantees at most one non-terminal
(`created`/`receiving`) job per (org, project, source_store_id).

## Audit Events

- `ImportStarted` — on create.
- `ImportBatchRejected` — on any rejected batch (caps, ordering, monotonicity,
  validation), `result=error`/`denied`, with a reason code.
- `ImportCompleted` — on successful finalize.
- `ImportFailed` — on explicit failure.

All audit metadata is passed through `redact_value` and carries only counts,
reason codes, and identifiers — never raw row content.

## Console Read Surface (M4 support)

- `GET /v1/admin/imports` (list) and `GET /v1/admin/imports/{id}` (detail).
- Console auth triple: `[IsAuthenticated, ActiveOrganizationPermission,
  RequireCapability('memories:read')]`, org-scoped and read-only.

## Security Notes

- No archive handling and no filesystem input on the server: the SQLite/`.tar`
  surface stays entirely client-side.
- Tenant isolation: org is bound to the key; project is resolved and authorized
  through `resolve_project_for_scope`; a key for org A cannot write an org B
  project (resolution 404/deny).
- Per-batch transactions bound blast radius and make replay safe.
- Deferred embedding avoids provider calls in the request path.
- Row content never lands in logs, audit, or error responses; only redacted
  metadata and fixed public error strings are surfaced.
