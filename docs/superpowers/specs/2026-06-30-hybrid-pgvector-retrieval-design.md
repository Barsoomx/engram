# Design: pgvector ANN semantic retrieval

> Roadmap Слой 3 P0 ("Retrieval в Postgres: pgvector ANN … вместо brute-force
> Python-цикла; качество + bounded CPU = маржа"). Base: current master (pgvector
> harness present; `embedding_pgvector = vector(64)` column exists but is unpopulated
> and unused). Tests on postgres+pgvector.

## Problem
`_embed_document` (`context/services.py:464-505`) writes only `embedding_vector`
(JSON list) — the `embedding_pgvector` column is never populated.
`semantic_retrieval_matches` (`context/services.py:336-355`) brute-forces a Python
`cosine_similarity` loop over EVERY authorized document's JSON vector — unbounded CPU
at scale. pgvector ANN exists in the DB but is dead.

## Target
Populate `embedding_pgvector` at index time and serve the semantic leg via a pgvector
ANN (cosine-distance) query — **behaviourally equivalent** to the current Python
cosine (same `SEMANTIC_MIN_SIMILARITY=0.3` floor, same ordering + tie-breaks) so every
existing context/search test and the compose golden-path e2e stay green — with a Python
fallback when the column is unpopulated or pgvector is unavailable. Gated by the
existing `OrganizationSettings.hybrid_retrieval_enabled`.

## Design

### 1. Populate the vector column
`IndexMemoryVersion._embed_document` (`context/services.py:503-505`): in addition to
`embedding_vector = list(result.embedding)`, set `document.embedding_pgvector =
list(result.embedding)` **only when `VectorField is not None`** (guard the import-gated
field; on a non-pgvector build the attribute is `None` and must be skipped). Add
`embedding_pgvector` to the `save(update_fields=[...])`. No behavioural change to
indexing.

### 2. pgvector ANN semantic path
New `semantic_retrieval_matches_pgvector(query_vector, documents_qs, already_matched, limit)`
in `context/services.py`:
- Use `from pgvector.django import CosineDistance` (import inside the function / module
  guarded by `VectorField is not None`).
- Query: `documents_qs.exclude(id__in=already_matched).filter(embedding_pgvector__isnull=False)
  .annotate(distance=CosineDistance('embedding_pgvector', query_vector))
  .filter(distance__lte=(1 - SEMANTIC_MIN_SIMILARITY)).order_by('distance', 'memory_id', 'id')[:limit]`
  — cosine distance = 1 − cosine similarity, so `distance ≤ 0.7` ⇔ `similarity ≥ 0.3`,
  matching the existing floor. similarity = `1 − distance`. Build the same
  `RetrievalMatch` shape (`inclusion_reason=f'semantic match: cosine {similarity:.2f}'`)
  the Python path emits, with the same deterministic tie-break.
- `semantic_retrieval_matches` becomes a dispatcher: if `VectorField is not None` AND any
  authorized doc has a non-null `embedding_pgvector`, use the pgvector path; else fall
  back to the existing Python loop (unchanged). This keeps legacy/un-backfilled docs and
  non-pgvector builds working.

### 3. Backfill (operator tool)
`engram_backfill_pgvector_embeddings` management command + test: copy each
`RetrievalDocument.embedding_vector` (non-empty) into `embedding_pgvector` where it is
null. Idempotent, batched. (Existing memories get an indexed vector without re-embedding.)

### 4. Index (optional, low-risk)
Add an `HnswIndex`/`IvfflatIndex` on `embedding_pgvector` only if it does not complicate
the migration; otherwise defer — a sequential pgvector scan still beats the Python loop
and keeps the migration simple. (Decide in implementation; if added, it is a `core`
migration and must be ruff-formatted.)

## Tests (postgres+pgvector — ANN is now testable)
- `_embed_document` populates `embedding_pgvector` (FakeProviderGateway deterministic 64-dim).
- `semantic_retrieval_matches_pgvector`: a query near one doc returns it with the right
  `similarity`/order; docs below the 0.3 floor excluded; `already_matched` excluded;
  ordering/tie-break deterministic and **identical** to the Python path on the same data
  (add a test asserting the two paths return the same ids+order for a seeded set).
- dispatcher falls back to Python when no `embedding_pgvector` present.
- backfill command copies vectors idempotently.
- ALL existing `context/`, `search/`, and the golden-path-shaped tests stay green
  (behavioural equivalence). FULL CI gate (`ruff check . && ruff format --check . &&
  migrate && makemigrations --check && pytest`) clean.

## Out of scope (follow-up)
pg_trgm / FTS `ts_rank` lexical leg + a true RRF/weighted fusion of lexical+semantic
(this slice keeps the existing exact-bucket + semantic structure, only swapping the
semantic engine to pgvector ANN); cross-encoder rerank.
