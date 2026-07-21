# Known backend issues

## B-004 inspection DETAIL view team-scope/visibility leaks (follow-up from MCP-parity campaign, 2026-07-21)

Found by adversarial review during the read-tools slice (#282); deliberately
descoped from that client-tooling slice (teamlead decision, spec
2026-07-19-mcp-read-tools-design.md Out of Scope). The LIST paths and the
by-id memory endpoints were hardened in #282; the inspection DETAIL view still
has pre-existing leaks:

- `_memory_source_provenance` reads unfiltered `memory.versions.all()`
  (inspection/views.py) — cross-scope version provenance can surface.
- Memory detail inlines `memory.retrieval_documents.all()` without
  `filter_documents_by_team_visibility` — a TEAM-tagged document on a
  PROJECT-visible memory is exposed to other teams (search filters
  per-document; detail does not).
- Detail selection uses the looser `team_filter` rather than the retrieval
  visibility whitelist, so `visibility_scope`/`team` mismatched rows render
  richer detail than retrieval would ever inject.

Fix direction: apply the same PROJECT-or-authorized-TEAM whitelist predicate
(memory/visibility.py from #282) to the inspection detail queryset, its
versions/retrieval_documents/related projections, and `_memory_source_provenance`.
