# Design: Layer-3 follow-ups (3 in one branch)

> Branch `feat/layer3-followups`, based on master + curator (#45) + pgvector ANN
> retrieval (#46). Tests on postgres+pgvector. Three independent follow-ups, each a
> small coherent commit-set; behaviour-changing parts are opt-in flags so all existing
> tests + the golden-path e2e stay green by default.

---

## Follow-up 1 — Real per-observation confidence (replaces the hardcoded 0.5)
Today `ProcessObservationRecorded` creates the candidate with
`confidence=Decimal('0.500')` (a constant), so the gate can't distinguish rich vs thin
observations.

**Design:** `derive_observation_confidence(observation) -> Decimal` in
`memory/services.py` — a deterministic heuristic over observation richness:
- base `0.50`; `+0.10` if `facts` non-empty; `+0.10` if `files_read` or `files_modified`
  non-empty; `+0.10` if `narrative` non-empty; `+0.05` if `concepts` non-empty;
  `+0.10` if `observation_type` in a durable set (`decision`, `architecture`,
  `convention`, `gotcha`); clamp `[0,1]`, quantize 3dp.
Use it where the candidate is created (replace the `0.500` constant). The
session-distillation path keeps its model-emitted confidence (unchanged).
**Effect:** rich observations (≈0.85) auto-promote at the default 0.800 threshold;
thin ones fall to the review queue — the gate becomes meaningful. The golden-path org
(threshold 0) is unaffected.
**Tests:** `derive_observation_confidence` matrix; a rich observation auto-promotes, a
thin one is held (update the relevant worker test).

---

## Follow-up 2 — LLM-judged curation (opt-in)
The curator's near-dup decision is a hard cosine threshold. Borderline near-dups
(just under the threshold) are a coin-flip. Escalate those to an LLM judge.

**Design:**
- `OrganizationSettings.curator_llm_judge_enabled` (BooleanField, default **False** —
  opt-in, adds provider cost) + migration.
- In `CurateMemoryCandidate`: when the best near-dup score is in the GRAY BAND
  `[near_dup_threshold - 0.10, near_dup_threshold)` AND `curator_llm_judge_enabled`,
  call an LLM judge (`ModelPolicy task_type='curation'`, fall back to 'generation')
  with a prompt = the new candidate + the near-dup memory and a system prompt asking
  for a JSON `{"decision": "merge"|"keep_both"|"reject"}`. Map: `merge` → supersede
  (as today's near-dup path), `keep_both` → promote clean, `reject` → REJECTED +
  `MemoryAutoRejected` audit. Parse the provider body as JSON (reuse the distillation
  parse-with-fallback pattern; on unparseable → default `keep_both`, the safe
  non-destructive choice). `FakeProviderGateway` returns a deterministic judgment
  (extend its `response_kind` handling with a `'curation_judgment'` kind that emits a
  fixed JSON `{"decision":"merge"}` — or reuse the existing candidates path and let the
  curator parse a `decision` field).
- Above the hard threshold → deterministic supersede (unchanged). Below the gray band →
  promote clean (unchanged). Judge disabled → unchanged deterministic behaviour.
**Tests:** gray-band + judge `merge` → superseded; `keep_both` → promoted; `reject` →
rejected; judge disabled → deterministic; unparseable → keep_both.

---

## Follow-up 3 — Lexical fusion (pg_trgm/FTS) + RRF + HNSW index (fusion opt-in)
The semantic + exact legs are concatenated with constant bucket scores; there is no
lexical relevance weighting and no ANN index.

**Design:**
- **HNSW index** (additive, no behaviour change): a migration adding
  `pgvector.django.HnswIndex` on `RetrievalDocument.embedding_pgvector`
  (`vector_cosine_ops`). Pure speed; ruff-format the migration.
- **Lexical leg + RRF fusion (opt-in):** `OrganizationSettings.lexical_fusion_enabled`
  (BooleanField, default **False**) + migration. When enabled, compute a lexical score
  per authorized doc via Postgres FTS (`SearchVector('full_text')` +
  `SearchRank(SearchQuery(query_text))`) and/or `TrigramSimilarity('full_text', query)`,
  rank it, and fuse the lexical rank with the semantic rank via deterministic
  **Reciprocal Rank Fusion** (`score = Σ 1/(k + rank_i)`, k=60), with the existing
  stable tie-break `(-score, -updated_at, title.casefold, id)`. The fused list replaces
  the semantic leg ordering within `_rank_matches` ONLY when the flag is on; default
  (flag off) keeps today's exact-bucket + semantic-pgvector behaviour byte-identical
  (existing tests + e2e green).
**Tests:** HNSW migration applies (`makemigrations --check` clean after generation);
fusion-on reorders by combined lexical+semantic relevance on a seeded set
(deterministic); fusion-off path unchanged (all existing context/search tests green).
**Out of scope:** cross-encoder rerank; per-term frequency weighting beyond FTS rank.

---

## Process (each follow-up)
TDD; after EACH follow-up run the FULL CI gate (`ruff check . && ruff format --check . &&
migrate && makemigrations --check && pytest`) clean before the next. One PR for the
branch at the end. Behaviour-changing default-on paths are forbidden — fusion + LLM
judge are opt-in; only the per-observation confidence (Follow-up 1) changes a default,
and it is gated by the existing auto-approve threshold (review queue absorbs the rest).
