# Security Review: Memory Search API

**Branch:** `feat/memory-search-api`
**Date:** 2026-06-26
**Reviewer:** implementation lead self-review against the established backend
security bar (the slice is a read-only reuser of proven primitives, so a focused
self-review is appropriate; a dedicated independent agent pass is optional for
this additive read endpoint).

## Scope

- `apps/backend/engram/search/services.py` — `SearchMemories`, `SearchInput`,
  `SearchResult`.
- `apps/backend/engram/search/serializers.py` — `SearchRequestSerializer` and
  size caps.
- `apps/backend/engram/search/views.py` — `SearchView`.
- `apps/backend/engram/context/services.py` — extracted module helpers
  `score_retrieval_document` and `authorized_retrieval_documents` (1:1 move
  out of `BuildContextBundle`, behavior-preserving).

## Checks

- **Authorization.** `SearchMemories` calls
  `ResolveApiKeyScope(required_capability='search:query', ...)`. The
  `search:query` capability is seeded into the default roles. An API key
  scoped to a project cannot search another project (`project_scope_denied`),
  and a key without `search:query` is rejected (`missing_capability`). Covered
  by `test_search_requires_search_query_capability` and
  `test_search_denies_wrong_project`.
- **Tenant isolation.** Search candidates come only from
  `authorized_retrieval_documents`, the same organization/project/team/visibility
  filter used by context retrieval. A team-visible memory in a team outside the
  effective scope cannot appear in results. Covered by
  `test_search_excludes_other_team_memory`.
- **Redaction.** Response titles/bodies pass through `redact_text`; the raw API
  key is absent from the response (`RAW_KEY not in str(body)`).
- **Statelessness.** Search performs no writes; the only audit artifact is the
  `AccessScopeResolved` allow record emitted by `ResolveApiKeyScope`. No
  context bundle, no memory mutation, no new provider calls.
- **Input limits.** `SearchRequestSerializer` caps query length (8000),
  file-path/symbol entry length (1024), and list size (100), matching the
  context serializer discipline. Covered by `test_search_rejects_oversized_query`.
- **Extraction safety.** `score_retrieval_document` and
  `authorized_retrieval_documents` are 1:1 moves; the full context API suite
  (22 tests) stays green, confirming behavior preservation.

## Findings

None Critical or Important. No new write path, no new secret surface, no new
provider boundary, no new untrusted-content rendering.

## Accepted Risks

- Search is exact-only in this slice. Semantic recall for search is deferred;
  adding it later reuses the existing `_semantic_matches` path and widens the
  embedding provider call surface to search, which must be decided then.
- Search has no dedicated audit event beyond `AccessScopeResolved`; if search
  activity needs compliance-grade observability, a `MemorySearched` audit event
  can be added in a later slice without changing the public contract.

## Verdict

**SECURITY APPROVED.** The endpoint is an authorized, tenant-isolated,
stateless, size-capped read over existing retrieval primitives.
