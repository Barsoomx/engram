# Memory Links Design

## Goal

Add a `MemoryLink` model and `POST /v1/memories/<id>/links` endpoint that lets
an authorized reviewer or agent attach structured links (file path, symbol,
commit, issue/PR reference) to an approved memory, and a `GET` to list them.
Advances the Memory Quality roadmap item ("links between memory, code, pull
requests, and tasks") and gives retrieval, inspection, and future UI a stable
provenance surface for code↔memory navigation.

## Decision

Approach: a new `MemoryLink` model scoped to organization/project/memory, plus a
`RecordMemoryLink` service and thin DRF views, mirroring the versioning and
feedback contracts.

- Capability `memories:review` (same as versioning/feedback).
- `MemoryLink` fields: `memory` FK, `link_type` (file/symbol/commit/issue),
  `target` (the path/sha/ref string, max 1024), `label` (optional human label,
  max 255), scoped `organization`/`project`, timestamps. Unique constraint on
  `(memory, link_type, target)` so replay does not duplicate a link.
- `RecordMemoryLink` resolves scope, locks the memory (shared
  `lock_memory_for_update` + `ensure_memory_team_scope`), redacts `target` and
  `label`, creates or gets the link idempotently, audits `MemoryLinkRecorded`.
- Endpoints:
  - `POST /v1/memories/<id>/links` returns the link record (idempotent on
    `(link_type, target)`).
  - `GET /v1/memories/<id>/links` lists links for the memory.

## Data Model

Add `MemoryLink(TimestampedModel)` in `engram.core.models`:

- `organization`, `project` FKs (cascade/protect per existing pattern).
- `memory` FK (cascade).
- `link_type` CharField choices `file`, `symbol`, `commit`, `issue`.
- `target` CharField max 1024.
- `label` CharField max 255 blank.
- Unique constraint `(memory, link_type, target)`; index on
  `(organization, project, link_type)`.

Migration: one additive `CreateModel`.

## API Contract

`POST /v1/memories/<memory_id>/links`:

```json
{
  "project_id": "...",
  "team_id": "optional",
  "link_type": "file",
  "target": "apps/backend/engram/memory/services.py",
  "label": "versioning service",
  "request_id": "...",
  "correlation_id": "optional"
}
```

Response 200 (or 201 on first create):

```json
{
  "memory_id": "...",
  "link_id": "...",
  "link_type": "file",
  "target": "...",
  "label": "...",
  "created": false
}
```

`GET /v1/memories/<memory_id>/links?project_id=...[&team_id=...]` returns
`{"items": [...]}`.

Errors reuse the access-denied shape; `memory_not_found` 404; oversized
`target`/`label` 400.

## Boundaries

This slice owns:

- `apps/backend/engram/core/models.py` — `MemoryLink`, `LinkType` choices.
- `apps/backend/engram/core/migrations/0005_memorylink.py`.
- `apps/backend/engram/memory/services.py` — `RecordMemoryLink`, dataclasses.
- `apps/backend/engram/memory/serializers.py` — `MemoryLinkSerializer`,
  `MemoryLinkQuerySerializer`.
- `apps/backend/engram/memory/views.py` — `MemoryLinkView`,
  `MemoryLinkListView`.
- `apps/backend/engram/memory/urls.py` — routes.
- `apps/backend/engram/memory/memory_links_tests.py`.

This slice defers:

- automatic link extraction from observations/diffs;
- link-driven retrieval boosting (separate slice);
- MCP/CLI link commands;
- cross-memory link graphs.

## Verification

Compose: focused RED→GREEN for create+idempotency, list, capability denial,
project/team denial, oversized target rejection, audit; full backend gate;
e2e golden path; repository checks; security note.

## Self-Review

- Additive: one model, one migration, one write path reusing proven lock/scope/
  audit helpers.
- Idempotency via unique constraint + get-or-create.
- Redaction of `target`/`label` follows the existing pattern.
- No retrieval change in this slice; link-driven ranking is deferred.
