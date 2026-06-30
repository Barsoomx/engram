# Design: token-budget packing for context bundles

> Roadmap Слой 3 ("[M/high] Token-budget packing — считать токены, trim по rank;
> сейчас bounded по числу items, не токенам"). Base: current master (has the pgvector
> harness). Independent of distillation/digest. Tests on postgres+pgvector.

## Problem
`BuildContextBundle._rank_matches` (`context/services.py:668-708`) truncates matches by
**item count** (`[:data.limit]`). `token_budget` is threaded through
(`ContextBundleInput.token_budget`, persisted at `services.py:523`, model field
`core/models.py`) but is **write-only** — nothing reads it to trim by token cost.

## Target
When `token_budget` is set, pack matches by accumulated token cost (in rank order)
instead of (in addition to) the item-count `limit`, and record packing evidence.

## Design
- **`estimate_tokens(text: str) -> int`** — pure, deterministic, char-based:
  `(len(text) + 3) // 4` (≈4 chars/token). Module-level in `context/services.py`,
  injectable/testable in isolation. (Documented heuristic; a real tokenizer is a
  later swap.)
- **`_pack_to_budget(matches, token_budget, limit) -> (kept, dropped)`**:
  - `token_budget is None` → preserve today's behavior: `kept = matches[:limit]`,
    `dropped = matches[limit:]`.
  - `token_budget` set → iterate matches in their already-ranked order; for each,
    compute its cost = `estimate_tokens(rendered_block(match))` over the SAME per-match
    rendered shape `_render_context` emits (so the budget is meaningful); keep while
    `tokens_used + cost <= token_budget` AND `len(kept) < limit`; the rest → dropped.
    Always keep at least the first match if it alone exceeds the budget? No — if even
    the top match exceeds the budget, keep it (a bundle with one over-budget top match
    is more useful than empty); document this single-exception. Tie-break/order is the
    existing stable `_rank_matches` ordering (do not re-sort).
  - Returns `(kept_tuple, dropped_tuple)` + the caller computes `tokens_used`.
- **Integrate** in `BuildContextBundle.execute` after `_rank_matches`: replace the
  item-count slice with `_pack_to_budget`; persist `bundle.metadata` additions
  `{'token_budget': int|None, 'tokens_used': int, 'dropped_for_budget': int}`; dropped
  matches get an exclusion reason `'token_budget'` consistent with the search-debug
  vocabulary. `selected_count = len(kept)`.
- **Scope**: context bundle ONLY. Do NOT add budget trimming to `SearchMemories`
  (search has no token_budget). The `search_debug_service._pack` placeholder
  realignment is an OPTIONAL follow-on, out of this slice.

## Tests (postgres+pgvector)
`context/services_tests.py` (+ extend `context_api_tests.py`):
- `estimate_tokens` pure cases.
- token_budget None → unchanged item-count behavior; existing context tests
  (which send `token_budget=2000` with tiny memories) stay green (tiny memories are
  well under 2000 tokens → nothing dropped).
- a small token_budget that fits only the top-1/2 matches → asserts the lower-ranked
  matches land in `dropped`, `metadata.tokens_used`/`dropped_for_budget` correct,
  `selected_count` reduced, ordering preserved (stable rank).
- single over-budget top match → kept (documented exception).
Full suite green; ruff clean; no migration (metadata-only).

## Out of scope (follow-up)
Real tokenizer; `search_debug` placeholder realignment; deterministic lexical+semantic
fusion/rerank (separate retrieval slice).
