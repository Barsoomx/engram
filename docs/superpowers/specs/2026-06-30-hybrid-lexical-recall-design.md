# Design: Hybrid retrieval completion — pg_trgm lexical recall + RRF union (opt-in)

> Branch `feat/hybrid-lexical-recall`, off current master. Completes roadmap Слой 3
> "Retrieval в Postgres: pgvector ANN + pg_trgm + FTS + hybrid fusion + детерминированный
> rerank". Tests on postgres+pgvector. Default-off → byte-identical.

## What already exists (do NOT rebuild)
`_rank_matches` (`context/services.py:879`) runs, in order:
1. **Exact pass** over ALL authorized docs (`score_retrieval_document`): file=100, symbol=80,
   exact_term=60, **substring full_text=40** (a naive lexical recall leg already exists),
   filter-only=1. Sorted, early-exit if `len ≥ limit`.
2. **Semantic pass** (`semantic_retrieval_matches`, pgvector ANN or Python cosine): cosine ≥
   `SEMANTIC_MIN_SIMILARITY=0.3`, excludes exact-matched, score=30.
3. Optional `lexical_fusion_matches` (RRF re-rank of the semantic set only) when
   `OrganizationSettings.lexical_fusion_enabled` (#49).
4. Returns `(exact + tail)[:limit]`.

`fuse_semantic_lexical(semantic_matches, lexical_ranks)` already implements RRF
(`score = Σ 1/(60+rank)`) with the stable tie-break `(-rrf, -score, -updated_at,
title.casefold, id)`. `lexical_retrieval_ranks` already runs FTS (`SearchRank(SearchVector
('full_text'), query)`).

## The remaining gap
A doc that matches the query only **fuzzily** (typo / near-miss term) — not an exact
substring (so missed by the score=40 leg) and not semantically ≥0.3 (so missed by the
semantic leg) — is never recalled. There is no `pg_trgm` trigram leg, and the existing
fusion only re-ranks semantic candidates (never adds new recall).

## Target (opt-in, default byte-identical)
A new opt-in `lexical_recall_enabled` that adds an **independent lexical recall leg**
(FTS ts_rank ∪ pg_trgm trigram) over authorized docs missed by the exact+semantic passes,
then **RRF-fuses** the semantic and lexical legs into one ranked tail. Exact matches stay
dominant (always first). Flag off → today's behaviour byte-for-byte.

## Design

### Config + extension (migrations)
- `OrganizationSettings.lexical_recall_enabled = BooleanField(default=False)` → migration
  `core/0019` (head is `0018`).
- `pg_trgm` extension: a migration operation `TrigramExtension()` (`from
  django.contrib.postgres.operations import TrigramExtension`). Mirror how `core/0006` adds
  the pgvector extension. May share the `0019` migration or be a separate `0020` — keep
  `makemigrations --check` clean. (The test container DB role already creates extensions for
  pgvector; production needs the same CREATE EXTENSION right — note in the PR.)
- `resolve_lexical_recall_enabled(organization)` helper (mirror `resolve_lexical_fusion_enabled`).
- `TRIGRAM_MIN_SIMILARITY = 0.3` module constant.

### Lexical recall leg — `context/services.py`
`lexical_recall_matches(documents, already_matched_ids, query) -> list[RetrievalMatch]`:
- `candidate_ids = [d.id for d in documents if d.id not in already_matched_ids]`; if empty or
  `request_query_terms(query)` empty → return `[]`.
- Query (reuse `_lexical_search_query`):
  ```python
  rows = (RetrievalDocument.objects
      .filter(id__in=candidate_ids)
      .annotate(ts=SearchRank(SearchVector('full_text'), search_query),
                trgm=TrigramSimilarity('full_text', query))
      .filter(Q(ts__gt=0) | Q(trgm__gte=TRIGRAM_MIN_SIMILARITY))
      .values_list('id', 'ts', 'trgm'))
  ```
- Map each matched id back to the in-memory `documents` object (they carry `memory` via
  `select_related` — do NOT re-fetch). Build `RetrievalMatch(document=doc, score=20,
  matched_terms=(...), inclusion_reason='lexical match: trigram {trgm:.2f}'` or
  `'lexical match: ts_rank {ts:.3f}'` — pick the dominant signal). Order by `(-ts, -trgm,
  -updated_at, title.casefold, id)` and assign 1-based lexical ranks. score=20 sits below
  semantic (30) and above filter-only (1).
- Tenant scope: only over the passed `documents` (already `authorized_retrieval_documents`);
  `filter(id__in=candidate_ids)` never widens visibility.

### Fusion across legs — `context/services.py`
`fuse_retrieval_legs(semantic_matches, lexical_matches) -> list[RetrievalMatch]`:
- `semantic_ranks = {m.document.id: i for i,m in enumerate(semantic_matches, 1)}`;
  `lexical_ranks = {m.document.id: i for i,m in enumerate(lexical_matches, 1)}`.
- Union the docs (semantic first, then lexical-only), one `RetrievalMatch` per doc — prefer the
  semantic match object when a doc is in both (higher score=30 for display; RRF drives order).
- `rrf(doc) = (1/(K+semantic_ranks[id]) if present) + (1/(K+lexical_ranks[id]) if present)`,
  `K=RECIPROCAL_RANK_FUSION_K=60`. Sort by `(-rrf, -score, -updated_at, title.casefold, id)`
  (same tie-break as `fuse_semantic_lexical`). Keep `fuse_semantic_lexical` UNCHANGED for the
  existing `lexical_fusion_enabled` path.

### Wire into `_rank_matches`
Replace the current tail computation (lines ~913-921) with precedence:
```python
semantic_matches = self._semantic_matches(documents, exact_matches, query_vector)
if org_settings.lexical_recall_enabled:
    already = {m.document.id for m in exact_matches} | {m.document.id for m in semantic_matches}
    lexical_matches = lexical_recall_matches(documents, already, data.query)
    tail = fuse_retrieval_legs(semantic_matches, lexical_matches)
elif org_settings.lexical_fusion_enabled:
    tail = lexical_fusion_matches(semantic_matches, data.query)   # existing, unchanged
else:
    tail = semantic_matches
return tuple((exact_matches + list(tail))[: data.limit]), bool(tail), embedding_result
```
`lexical_recall_enabled=False` → the `elif/else` is exactly today's code → **byte-identical**.

## TDD / tests (postgres+pgvector)
- **Unit `lexical_recall_matches`**: seed authorized docs; a doc whose `full_text` contains a
  trigram-near (typo) term to the query — NOT an exact substring — is returned with score=20
  and a `lexical match:` reason; a doc already in `already_matched_ids` is excluded; empty query
  → `[]`; deterministic order. (Deterministic — no embedding noise.)
- **Unit `fuse_retrieval_legs`**: a doc in BOTH legs outranks a doc in one; a lexical-only doc is
  included (recall expansion); RRF + tie-break deterministic (assert exact id order).
- **Migration**: `pg_trgm` extension applies; `makemigrations --check` clean after the field +
  extension migrations.
- **Flag-OFF byte-identity**: all existing `context/`, `search/`, golden-path tests green; add a
  regression test asserting a seeded retrieval returns identical ids+order+inclusion_reason with
  the flag off.
- **Flag-ON integration**: with `lexical_recall_enabled=True`, a fuzzy-only doc surfaces in the
  bundle that does NOT with the flag off (drive via `lexical_recall_matches` if the
  FakeProviderGateway embedding makes semantic similarity nondeterministic — keep the assertion
  on the lexical leg / fused tail, not on raw cosine).
- Tenant scope preserved (no cross-project recall).

## Out of scope
GIN `gin_trgm_ops` index (perf only — the candidate set is the small per-project authorized set,
`filter(id__in=...)`); cross-encoder rerank; per-term weighting beyond ts_rank/trigram; changing
the existing `lexical_fusion_enabled` re-ranker.

## Gate (all pass; baseline 730 passed / 4 skipped on master + new tests)
Backend (`engram-prod` + `engram-pg`): `ruff check .`, `ruff format --check .` (ruff-format any
generated migration), `migrate`, `makemigrations --check --dry-run` → No changes detected,
`pytest -q`.
