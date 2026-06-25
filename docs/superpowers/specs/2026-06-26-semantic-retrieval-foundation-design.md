# Semantic Retrieval Foundation Design

## Goal

Deliver the first working hybrid retrieval path on the Engram server. The exact
retrieval and context-bundle loop is green end to end; roadmap item 10 now
requires a semantic retrieval adapter behind an interface so Engram stops being
exact-only without yet committing to production vector infrastructure.

This slice adds an embeddings provider adapter, persists a deterministic
embedding vector on each `RetrievalDocument`, and uses cosine similarity as a
second retrieval signal inside `BuildContextBundle` when exact matching does not
fill the requested limit.

## Context And Gate

`docs/parity/claude-mem-parity-map.md` deferred semantic retrieval until the
first exact CLI/hooks/API E2E loop was green. That loop is green:
`scripts/e2e_golden_path.py` passes on the merged parity track and the
provider-memory-worker checkpoint. The deferral trigger conditions are now
satisfied, so semantic retrieval moves from `defer` to `replace` for the
runtime path.

Current state that this slice changes:

- `RetrievalDocument.embedding_reference` is a `CharField` that
  `IndexMemoryVersion` always writes as `''`.
- `BuildContextBundle` is exact-only. Bundle and audit metadata hard-code
  `retrieval_strategy: 'exact'`.
- `TaskType.EMBEDDING` exists in the model-policy choices but no code resolves
  or calls an embeddings policy.

## Decision

Approach A: embeddings provider adapter plus a vector `JSONField` plus cosine
fallback.

- Generate embeddings through the same model-policy resolution and
  `FakeProviderGateway` boundary already used for generation, with a new
  embeddings task path.
- Store the vector on `RetrievalDocument` as a new `embedding_vector`
  `JSONField`. Keep `embedding_reference` as a stable provider-call reference
  string for audit and future pgvector hand-off.
- Add a cosine-similarity fallback inside `BuildContextBundle`. Exact matches
  stay authoritative and always rank above semantic matches. Semantic matches
  only fill remaining slots when exact matching returns fewer items than the
  requested limit.
- Do not introduce pgvector, a vector index, OpenAI network calls, Chroma, a new
  HTTP endpoint, or prompt-submit injection in this slice.

Rationale:

- matches the existing provider/gateway and model-policy patterns;
- keeps the slice test-deterministic and free of new infrastructure;
- leaves a single adapter boundary to swap for real OpenAI embeddings plus a
  pgvector column later.

## Data Model

Add one field to `RetrievalDocument`:

- `embedding_vector = JSONField(default=list, blank=True)` holding a list of
  floats. Empty list means "not embedded yet".

No other model changes. `embedding_reference` keeps its current type but now
stores the embeddings provider call reference string when an embedding was
generated, and remains `''` only when no embeddings policy exists.

Migration: one additive `AddField` on `engram.RetrievalDocument`. Additive,
nullable-via-default, safe against existing rows. No data backfill in this
slice; existing retrieval documents keep an empty vector until they are
re-indexed. Backfill is out of scope.

## Embeddings Provider Adapter

Extend the model-policy provider surface with an embeddings-specific result and
gateway method. Generation behavior is unchanged.

- New `EmbeddingCallInput` dataclass: `organization_id`, `project_id`,
  `team_id`, `policy`, `request_id`, `trace_id`, `text`.
- New `EmbeddingCallResult` dataclass: `provider`, `model`, `call_record_id`,
  `redaction_state`, `embedding` (`tuple[float, ...]`).
- New `FakeProviderGateway.embed(data: EmbeddingCallInput) -> EmbeddingCallResult`.
  It mirrors the generation `call` contract: it resolves the active secret
  envelope, reuses an existing `ProviderCallRecord` for the same
  `(organization, project, task_type='embedding', request_id)` to stay
  idempotent, otherwise creates a redacted `ProviderCallRecord`, and returns a
  deterministic embedding derived from the redacted input text.

Deterministic embedding algorithm (feature-hashing projection):

- Tokenize the redacted text: lowercase, split on non-alphanumeric characters,
  drop tokens shorter than two characters.
- For each token, compute `sha256(token)`; use the digest to pick a dimension
  index in `[0, EMBEDDING_DIMENSION)` and a sign in `{-1, +1}`; accumulate
  `sign` into that dimension.
- `EMBEDDING_DIMENSION = 64`.
- L2-normalize the vector. If the text has no tokens, return the zero vector.
- Round each component to six decimal places so persisted JSON is stable and
  readable.

This is not real semantic similarity. It is a deterministic lexical-overlap
signal: documents and queries that share tokens produce correlated vectors, so
cosine similarity is non-trivial and testable, while the provider boundary stays
identical to the generation path. Swapping in OpenAI embeddings later replaces
only the `embed` implementation and the storage column type.

Redaction: the embedding input text is redacted through
`engram.core.redaction.redact_value` before tokenization, exactly as generation
redacts its prompt. Token-shaped values never reach the vector or the provider
call record.

## Indexing Lifecycle

`IndexMemoryVersion.execute` gains an embedding step after the
`RetrievalDocument` is created or updated:

- Resolve an embeddings model policy with `ResolveModelPolicy` for
  `task_type='embedding'` under the memory's organization/project/team scope.
- If a policy is resolved, call `FakeProviderGateway.embed` with the document
  `full_text` and store the result vector on `embedding_vector` and the provider
  call reference on `embedding_reference`.
- If `ResolveModelPolicy` raises `ModelPolicyError` (`model_policy_not_found`),
  skip embedding silently: leave `embedding_vector` as `[]` and
  `embedding_reference` as `''`. Indexing must not fail when an embeddings
  policy is absent, because exact retrieval remains the authoritative path and
  older fixtures have no embeddings policy.
- If `ProviderSecretError` is raised (disabled secret, missing envelope), also
  skip embedding, but emit a structured warning log with the organization,
  project, memory version id, and redacted error so the operator state is
  visible. Leave `embedding_vector` as `[]` and `embedding_reference` as `''`.

Rationale: embedding is a best-effort enhancement over authoritative exact
retrieval. Indexing and memory promotion must never fail because of embeddings
infrastructure. A disabled secret is observable through logs and through the
absence of an embeddings provider call record, and the document becomes
embeddable on the next indexing pass once the secret is restored. This keeps the
memory worker transaction simple and avoids half-indexed documents.

Embedding is idempotent: the vector is a pure function of the redacted
`full_text`, so re-indexing the same memory version produces the same vector.
The provider call record is reused by `request_id`, matching the generation
path. The embedding `request_id` is
`memory-indexer:{memory_version_id}:embedding`.

The memory worker (`ProcessObservationRecorded`) already promotes and indexes
in one transaction; the embedding step runs inside the same
`IndexMemoryVersion` call, so no new transaction boundary or outbox signal is
introduced.

## Retrieval Semantic Fallback

`BuildContextBundle` keeps its current exact scoring and ordering unchanged.
Add a semantic fallback stage after exact ranking:

- Compute the set of exact matches as today.
- If `len(exact_matches) >= data.limit`, return exact matches only.
  `retrieval_strategy` is `'exact'`.
- Otherwise compute a query embedding:
  - Resolve an embeddings policy under the request scope.
  - If no policy resolves, skip semantic fallback. `retrieval_strategy` stays
    `'exact'` even if fewer than `limit` items were returned. This preserves the
    existing behavior for projects without an embeddings policy.
  - If a policy resolves, call `FakeProviderGateway.embed` with the request
    query text joined with `file_paths` and `symbols` terms.
- For each authorized document not already in the exact match set, compute
  cosine similarity between the query vector and the document
  `embedding_vector`. Skip documents whose `embedding_vector` is empty.
- Keep documents whose similarity is at least `SEMANTIC_MIN_SIMILARITY = 0.3`.
  Rank them by descending similarity, assign score `30` (below the exact
  full-text band of `40`), and set `inclusion_reason` to
  `'semantic match: cosine {similarity:.2f}'`.
- Append semantic matches to the exact matches until the combined set reaches
  `data.limit`.

`retrieval_strategy` metadata:

- `'exact'` when only exact matches were selected.
- `'semantic_fallback'` when the selected set contains any semantic match.
- Do not emit `'hybrid'` in this slice; the fallback is additive but exact
  remains the only primary signal. A future slice can introduce true hybrid
  fusion once a real embeddings adapter exists.

The query embedding provider call is made once per context request when the
fallback activates. It is deterministic for a given redacted query text, so the
same `request_id` replay yields the same selection and the same context bundle,
preserving context-bundle idempotency.

Authorization is unchanged: semantic candidates come from the same
`_authorized_documents` set used by exact scoring. No document outside the
effective organization/project/team scope can enter the bundle through the
semantic path.

## Audit And Observability

- The embeddings provider call writes a `ProviderCallRecord` with
  `task_type='embedding'`, redacted input, token usage, latency, and cost
  metadata, exactly like generation calls.
- `MemoryRetrieved` audit metadata gains `retrieval_strategy` and, when the
  fallback activates, `semantic_provider_call_id` plus the list of semantic
  `retrieval_document_ids`. Exact-only audits keep their current shape with the
  new `retrieval_strategy: 'exact'` field.
- Structured logs at the embedding boundary record provider, model, policy id,
  redaction state, and vector dimension, never the vector itself or the input
  text.

## Golden Path

`engram_bootstrap_golden_path` already creates a generation policy. Add a
parallel embeddings policy:

- Reuse the existing golden-path OpenAI provider secret.
- Create a project-scoped `ModelPolicy` with `task_type='embedding'`,
  `provider='openai'`, `model='text-embedding-3-small'`, bound to the golden
  secret.

This keeps the deterministic local golden path free of real provider calls
while exercising the embeddings resolution and indexing path. The E2E golden
path script itself is not changed in this slice: its fixture is exact and stays
exact. Semantic fallback is proved by focused backend tests, not by the
Compose E2E.

## Boundaries

This slice owns:

- `RetrievalDocument.embedding_vector` field and migration.
- Embeddings provider adapter: `EmbeddingCallInput`, `EmbeddingCallResult`,
  `FakeProviderGateway.embed`, and the deterministic embedding function.
- `IndexMemoryVersion` embedding lifecycle.
- `BuildContextBundle` cosine fallback and dynamic `retrieval_strategy`.
- Golden-path embeddings policy.
- Spec, plan, verification matrix, security review.

This slice defers:

- pgvector extension, vector indexes, and a real vector column type.
- Real OpenAI or Anthropic embedding network calls.
- A separate `/v1/context/semantic` or prompt-submit semantic endpoint.
- Chroma-compatible ordering and the upstream 90-day recency window.
- Backfill of existing retrieval documents.
- Frontend, MCP, and digest/curation changes.
- True hybrid rank fusion beyond additive fallback.

## Verification

Required commands, all run inside Compose per `AGENTS.md`:

- Focused RED then GREEN tests for embeddings adapter, indexer embedding,
  fallback retrieval, `retrieval_strategy`, redaction, golden-path policy, and
  missing-policy graceful degradation.
- `docker compose ... run --rm api sh -ec "poetry install ... --with dev && pytest -v && ruff check . && ruff format --check ."`
- Migration apply plus `makemigrations --check --dry-run`.
- `python3 scripts/e2e_golden_path.py` (unchanged exact path must stay green).
- `python3 -m unittest discover -s tests -v`, `scripts/repository_layout.py`,
  `scripts/repository_quality.py`, `git diff --check HEAD`.
- Focused security review and Karpathy simplicity review recorded under
  `docs/security/reviews/`.

Docker may be unavailable in this WSL distro; if so, record the blockage and
fall back to host `poetry run pytest` with the test settings, while recording
the exact commands and the reason Compose could not run.

## Self-Review

- The slice is one cohesive backend behavior change with one migration, one
  provider method, one indexer step, and one retrieval stage. It does not widen
  the API surface or the deployment topology.
- Exact retrieval stays authoritative and unchanged, so existing tests, the
  golden path, and parity fixtures remain green.
- Missing embeddings policy degrades gracefully to exact-only; a disabled
  embeddings secret skips embedding with a warning log. Both paths are tested.
- Redaction, idempotency, tenant scoping, and audit are preserved on the new
  path.
- The design leaves a single adapter boundary (`FakeProviderGateway.embed` and
  the `embedding_vector` column) to swap for real embeddings and pgvector
  later, without touching retrieval ranking or the public context contract.
