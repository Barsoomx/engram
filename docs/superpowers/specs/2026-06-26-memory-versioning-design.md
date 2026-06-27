# Memory Versioning Design

## Goal

Add an agent-native memory update endpoint `POST /v1/memories/<id>/version` that
updates a memory's body by creating a new `MemoryVersion`, incrementing
`Memory.current_version`, replacing the indexed `RetrievalDocument`, and
emitting an audit record. This advances the Memory Quality roadmap item
(proposals, deduplication, versions, links) and lets an authorized reviewer or
agent correct, refine, or extend an approved memory without losing history.

## Context

Memory promotion currently creates exactly one `MemoryVersion` (version 1) and
one `RetrievalDocument`. There is no path to update memory content after
promotion. `RecordMemoryFeedback` flips stale/refuted flags but does not edit
content. Versioning fills that gap: every body edit becomes an append-only
`MemoryVersion` and the retrieval index is rebuilt for the new version.

## Decision

Approach: a `UpdateMemoryBody` domain service plus a thin DRF view, mirroring
the established feedback endpoint contract.

- Capability `memories:review` (already seeded into default roles), same as the
  feedback loop.
- `select_for_update` lock on the memory row under `transaction.atomic()`.
- Team-scope check identical to `RecordMemoryFeedback`: a team-visible memory
  outside the effective team scope is denied.
- New `MemoryVersion(version=memory.current_version + 1, body=data.body,
  content_hash=...)`, then `memory.body = data.body`,
  `memory.current_version = next_version`, save.
- Re-index through `IndexMemoryVersion` so the new body, exact terms, and
  embedding vector reflect the update. `IndexMemoryVersion.update_or_create`
  keys on `memory_version`, so the prior version's document is untouched and the
  new version gets its own document.
- Audit `MemoryVersionCreated` event with capability `memories:review`,
  redacted reason/body summary, and resolved scope filters.

Idempotency: a duplicate `(memory, version)` is prevented by the existing
`core_memory_version_unique_version` constraint; concurrent updates serialize
through the row lock, so each call produces exactly one new version.

## API Contract

`POST /v1/memories/<memory_id>/version` with `Authorization: Bearer <api-key>`:

Request body:

- `project_id` (uuid, required)
- `team_id` (uuid, optional)
- `body` (string, required, non-blank, max 16000 chars)
- `reason` (string, optional, max 2000 chars)
- `request_id` (string, required, max 255)
- `correlation_id` (string, optional, max 255)

Response 200:

```json
{
  "memory_id": "...",
  "project_id": "...",
  "team_id": "...",
  "current_version": 2,
  "memory_version_id": "...",
  "retrieval_document_id": "..."
}
```

Errors: `AccessDeniedError` shape and status mapping reused from the feedback
endpoint; `memory_not_found` 404; oversized `body`/`reason`/`request_id`/
`correlation_id` 400.

## Data Model

No model changes. Uses existing `Memory`, `MemoryVersion`, `RetrievalDocument`,
`AuditEvent`, and the `core_memory_version_unique_version` constraint.

## Boundaries

This slice owns:

- `apps/backend/engram/memory/services.py` — `UpdateMemoryBody`,
  `UpdateMemoryBodyInput`, `UpdateMemoryBodyResult`, `memory_body_content_hash`.
- `apps/backend/engram/memory/serializers.py` — `MemoryVersionSerializer`.
- `apps/backend/engram/memory/views.py` — `MemoryVersionView`.
- `apps/backend/engram/memory/urls.py` — `version` route.
- `apps/backend/engram/memory/memory_versioning_tests.py`.

This slice defers:

- memory deduplication across distinct observations;
- links between memory and code/PRs/tasks;
- supersede/conflict UX;
- multi-field edits beyond `body`;
- MCP `memory.update` tool and CLI update commands.

## Verification

Required commands inside Compose:

- focused RED then GREEN for: update creates a new version and re-indexes;
  unauthorized capability denied; wrong project denied; cross-team team-visible
  memory denied; oversized body/reason rejection; duplicate-concurrent safety
  via the unique-version constraint; replay does not create a second version for
  the same body.
- full backend `pytest -v`, `ruff check .`, `ruff format --check .`.
- `python3 scripts/e2e_golden_path.py` unchanged path stays green.
- repository checks and whitespace.

## Self-Review

- The slice is additive: one new write path, no model change, reuses proven
  authorization, locking, indexing, and audit patterns.
- Append-only versioning preserves history; the retrieval index always reflects
  the latest version.
- Idempotency and concurrency are covered by the existing unique-version
  constraint plus a row lock.
- Redaction of body/reason and audit metadata follows the feedback-loop pattern.
