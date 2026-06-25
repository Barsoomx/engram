# Memory Feedback Loop Design

Date: 2026-06-25

## Goal

Add the first memory-quality correction loop: an authorized reviewer can mark
approved memory as `stale` or `refuted`, Engram updates the memory and retrieval
index state atomically, records an audit event, and future context bundles no
longer inject that memory.

## Scope

This checkpoint is backend-only.

Included:

- `POST /v1/memories/{memory_id}/feedback`
- request validation and size caps before state changes
- `memories:review` authorization using existing API key scope resolution
- atomic `Memory` and `RetrievalDocument` flag updates
- redacted `MemoryFeedbackRecorded` audit event
- regression coverage proving future context retrieval excludes the corrected
  memory
- verification matrix and focused security review artifact

Excluded:

- frontend/admin memory review UI
- MCP `memory.feedback`
- daily digest or scheduled curator jobs
- provider/model-policy calls
- semantic/vector retrieval
- new outbox architecture
- migrations, unless implementation discovers missing model state

## Contract

Endpoint:

`POST /v1/memories/{memory_id}/feedback`

Required bearer capability:

`memories:review`

Request body:

```json
{
  "project_id": "uuid",
  "team_id": "uuid or omitted",
  "action": "stale",
  "reason": "short operator reason",
  "request_id": "request-memory-feedback-1",
  "correlation_id": "optional-correlation-id"
}
```

Allowed `action` values:

- `stale`: sets `Memory.stale=True` and all retrieval documents for that memory
  to `stale=True`
- `refuted`: sets `Memory.refuted=True` and all retrieval documents for that
  memory to `refuted=True`

The action is additive. A feedback call never clears `stale` or `refuted`.

Response body:

```json
{
  "memory_id": "uuid",
  "project_id": "uuid",
  "team_id": "uuid or empty string",
  "action": "stale",
  "stale": true,
  "refuted": false,
  "retrieval_documents_updated": 1,
  "already_applied": false
}
```

Error behavior:

- missing or invalid bearer key returns the existing access error shape
- missing `memories:review` returns HTTP 403 with `code=missing_capability`
- cross-project or cross-team access returns the existing access error shape
- memory outside the resolved organization/project scope returns HTTP 404
  with `code=memory_not_found`
- oversized `reason`, `request_id`, or `correlation_id` returns HTTP 400 before
  flags or audit records are created

## Security Requirements

- The endpoint must not leak raw API keys in responses, audit metadata, errors,
  or persisted memory/retrieval metadata.
- Authorization must happen before memory state changes.
- A team-scoped memory can only be corrected when the resolved effective scope
  includes that team.
- The service must update `Memory` and `RetrievalDocument` in one transaction.
- The audit event must record action, redacted reason, target memory id, project,
  team, capability, request id, correlation id, and result.
- The endpoint must fail closed: denied or invalid requests must not create
  `MemoryFeedbackRecorded` audit events and must not mutate memory flags.

## Acceptance Criteria

- Focused backend tests prove successful `stale` feedback updates memory,
  retrieval documents, and audit.
- Focused backend tests prove successful `refuted` feedback prevents future
  context injection.
- Focused backend tests prove missing `memories:review`, wrong project, and
  oversized reason leave state unchanged.
- Existing context bundle tests continue to pass.
- `python3 scripts/e2e_golden_path.py` continues to pass after this backend
  change.
- A focused security review is committed under `docs/security/reviews/`.

## Simplicity Review

The smallest design is a synchronous API/service update over existing model
fields. No new queue, outbox, provider adapter, model-policy lookup, semantic
retrieval change, or UI is required for this checkpoint. Existing retrieval
already filters `memory__stale=False`, `memory__refuted=False`, `stale=False`,
and `refuted=False`; this slice only adds the missing correction write path and
tests the end-to-end effect.
