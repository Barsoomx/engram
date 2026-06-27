# Memory Search API Design

## Goal

Add an agent-native memory search endpoint `POST /v1/search` that returns
authorized, cited, ranked memory matches for a query without persisting a
context bundle. This unblocks the search MCP tool and a future `engram search`
CLI command, and closes a gap in the documented public API surface
(`docs/architecture.md` lists `/v1/search`).

## Context

The context bundle endpoint already implements authorized hybrid retrieval and
ranks `RetrievalDocument` rows. Search is the read-only, stateless counterpart:
same authorization and ranking intent, no `AgentSession` / `ContextBundle`
persistence, no session-start hook contract.

## Decision

Approach: a new `engram.search` Django app with a thin DRF view backed by a
`SearchMemories` domain service. The service resolves the API-key scope with
`search:query`, loads the authorized document set with the same helper used by
`BuildContextBundle._authorized_documents`, ranks documents with the existing
exact scoring logic, and returns cited matches. No context bundle, no audit
event beyond the standard `AccessScopeResolved` allow record emitted by
`ResolveApiKeyScope`.

To avoid duplicating the exact scoring logic, extract the per-document scoring
into a module-level function `score_retrieval_document(...)` in
`engram.context.services` and have both `BuildContextBundle._score_document`
and `SearchMemories` call it. This is a 1:1 move: behavior is identical, the
existing context tests stay green.

Semantic fallback is intentionally out of scope for this slice. Search is
exact-only initially. Adding semantic recall to search is a one-line reuse of
`_semantic_matches` once the ranking extraction is proven; it is deferred to
keep this slice cohesive and to avoid widening the embedding provider call
surface before search audit semantics are decided.

## Data Model

No model changes. Search reads `Memory`, `MemoryVersion`, and
`RetrievalDocument` and writes no domain rows.

## API Contract

`POST /v1/search` with `Authorization: Bearer <api-key>`:

Request body (mirrors the context request minus session/hook fields):

- `project_id` (uuid, required)
- `team_id` (uuid, optional)
- `query` (string, optional, default `''`)
- `file_paths` (list[string], optional)
- `symbols` (list[string], optional)
- `limit` (int, optional, default 5, max 10)
- `correlation_id`, `trace_id` (string, optional, for observability)

Response 200:

```json
{
  "request_id": "<server or client request id>",
  "items": [
    {
      "citation": "M1",
      "memory_id": "...",
      "memory_version_id": "...",
      "retrieval_document_id": "...",
      "title": "...",
      "body": "...",
      "inclusion_reason": "exact match: ...",
      "scope_evidence": {"visibility_scope": "...", "project_id": "...", "team_id": "..."},
      "matched_terms": ["..."]
    }
  ],
  "warnings": []
}
```

Errors: same `AccessDeniedError` shape and status mapping as the context and
hook endpoints (`invalid_key` 401, capability/scope denies 403).

## Authorization

`ResolveApiKeyScope(required_capability='search:query', requested_project_id=...,
requested_team_id=...)`. The `search:query` capability already exists in the
seeded default roles (`developer`, `auditor`, admin, owner). The authorized
document set is computed exactly as in context retrieval: same organization,
project, approved-memory, non-stale, non-refuted filter, plus visibility-scope
team intersection.

## Boundaries

This slice owns:

- `engram.search` app: `apps.py`, `serializers.py`, `services.py`, `views.py`,
  `urls.py`, `search_api_tests.py`.
- `engram.context.services`: extract `score_retrieval_document` module function;
  `BuildContextBundle._score_document` delegates to it.
- `settings/settings.py`: register `'engram.search'`.
- `settings/urls.py`: add `path('v1/search/', include('engram.search.urls'))`.
- `scripts/repository_layout.py`: require the new search app paths.
- Spec, plan, verification matrix entry, security review note.

This slice defers:

- semantic/vector recall in search;
- a search audit event distinct from `AccessScopeResolved`;
- CLI `engram search` and MCP search tool (this slice only proves the server
  endpoint);
- search result pagination beyond the `limit` cap;
- full-text search indexes (trigram/GIN).

## Verification

Required commands inside Compose:

- focused RED then GREEN for: search returns ranked cited matches; wrong
  capability denied; wrong project denied; cross-team team-visible memory
  excluded; oversized query/file-path rejection; replay idempotency of scope
  resolution.
- full backend `pytest -v`, `ruff check .`, `ruff format --check .`.
- migration apply plus `makemigrations --check --dry-run` (no migration expected
  but the gate must stay clean).
- `python3 scripts/e2e_golden_path.py` unchanged exact path stays green.
- repository checks and whitespace.

## Self-Review

- The slice is purely additive: one new read-only endpoint, one extracted helper
  that is a 1:1 move, no model or migration changes.
- Exact context retrieval behavior is unchanged because the scoring extraction
  is behavior-preserving and covered by existing context tests.
- Authorization reuses the proven `ResolveApiKeyScope` + authorized-document
  path; no new scope logic.
- Semantic recall is deferred explicitly so the embedding provider call surface
  does not widen silently.
