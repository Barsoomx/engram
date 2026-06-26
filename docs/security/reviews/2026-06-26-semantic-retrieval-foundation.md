# Security Review: Semantic Retrieval Foundation

**Branch:** `feat/semantic-retrieval-foundation`
**Date:** 2026-06-26
**Reviewer:** independent read-only security review agent plus Karpathy simplicity review agent.
**Scope:** semantic retrieval foundation slice — embeddings provider adapter, indexer embedding
lifecycle, cosine semantic fallback in context retrieval, golden-path embeddings policy.

## Scope Reviewed

- `apps/backend/engram/model_policy/services.py` — `EMBEDDING_DIMENSION`,
  `EmbeddingCallInput`, `EmbeddingCallResult`, `_embedding_grams`,
  `generated_embedding`, `FakeProviderGateway.embed`.
- `apps/backend/engram/context/services.py` — `SEMANTIC_MIN_SIMILARITY`,
  `cosine_similarity`, `IndexMemoryVersion._embed_document`,
  `BuildContextBundle._rank_matches`, `_semantic_matches`,
  `_resolve_query_embedding`, `_audit_retrieval`.
- `apps/backend/engram/core/models.py` — `RetrievalDocument.embedding_vector`.
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
  — embeddings policy.

Cross-referenced for parity: `FakeProviderGateway.call`, `ProcessObservationRecorded`,
`engram.core.redaction.redact_value`, `ResolveApiKeyScope` / `EffectiveScope`,
`ProviderCallRecord`.

## Findings By Severity

### Critical

None.

### Important

**I-1. Audit metadata hard-coded `retrieval_strategy: 'exact'`.**
`BuildContextBundle._audit_retrieval` wrote a static `'exact'` value into the
`MemoryRetrieved` audit event even when the semantic fallback ran, and omitted
`semantic_provider_call_id` and the semantic document id list. This broke
audit attribution for the query-embedding provider call and contradicted the
spec, which requires the audit to record the strategy used.

**Status: fixed in commit `e48c07e1`.** `_audit_retrieval` now threads
`has_semantic` and `embedding_result` through from the call site and emits the
real `retrieval_strategy`, plus `semantic_provider_call_id` and
`semantic_document_ids` when the fallback activates. A regression assertion in
`test_context_bundle_returns_semantic_fallback_when_exact_misses` pins the audit
shape.

### Minor

- **M-1. Query-embedding `request_id` dedup parity.** The query embedding
  reuses `data.request_id` as the provider-call idempotency key, matching the
  context-bundle uniqueness contract and the generation path. A caller that
  reuses the same `request_id` for a semantically different query would get the
  stale embedding. Accepted as parity with the existing generation behavior.
- **M-2. `[REDACTED]` substring heuristic.** `redaction_state` flips to
  `'redacted'` if the literal `[REDACTED]` appears in the input. Cosmetic only;
  mirrors the generation path.
- **M-3. Short redacted text produces a zero vector.** Redacted inputs with
  fewer than three alphanumeric characters yield the zero vector and are skipped
  by cosine similarity and `_semantic_matches`. Consistent with exact full-text
  behavior. No action.
- **M-4. `embedding_vector` JSONField has no dimension validation.** A future
  real OpenAI adapter returning a different dimension would be silently dropped
  by the length-mismatch guard. Acceptable while there is exactly one producer
  at `EMBEDDING_DIMENSION = 64`; revisit when a real adapter lands.

## Karpathy Simplicity Findings

- **K-1. `FakeProviderGateway.embed` duplicates the `call` record-creation
  structure.** Accepted risk: with a single fake gateway and one consumer each,
  extracting a shared `_record_provider_call` helper is premature. The
  real-adapter slice should factor it then. Does not weaken any guarantee.
- **K-2. `list(...)` conversions around cosine inputs.** Cosmetic; no defect.
  Left as-is.
- **K-3. `EMBEDDING_DIMENSION` and `SEMANTIC_MIN_SIMILARITY` live in different
  modules.** Survivable today; both are single-line constants to retune
  together when a real adapter lands.
- **K-4. Audit hard-code** — same as I-1, fixed.

## Explicit Property Checks

- **Tenant isolation.** Semantic candidates come only from the
  `_authorized_documents` tuple filtered by organization, project,
  `memory__status=APPROVED`, `!stale`, `!refuted`, and `visibility_scope`
  (PROJECT auto-allow; TEAM requires `team_id in scope.team_ids`). No document
  outside the effective scope can enter the bundle through the semantic path.
- **Redaction before tokenization.** `FakeProviderGateway.embed` calls
  `redact_value(data.text)` before `generated_embedding`. The vector and the
  `ProviderCallRecord` are pure functions of the redacted text. Token-shaped
  secrets (`sk-`, `egk_`, `bearer`, `AIza`, `xox[baprs]-`, sensitive dict keys)
  cannot reach the vector, the provider call record, or logs.
- **Idempotency.** Document embedding is keyed by
  `memory-indexer:{version_id}:embedding`; query embedding by
  `data.request_id`. Both reuse `ProviderCallRecord` rows on replay. Covered by
  `test_index_memory_version_embedding_is_idempotent_across_reindex`.
- **Graceful degradation.** Missing embeddings policy skips silently; disabled
  secret skips with a structured warning log carrying only ids and the redacted
  error string. Covered by dedicated tests.
- **Eager-call guard.** Query embedding is computed only when exact matches are
  below the requested limit (`_rank_matches` short-circuits before
  `_resolve_query_embedding`). No eager provider call on the hot path.
- **Log hygiene.** Structured warning logs at the embedding boundary emit only
  ids and the `ProviderSecretError` message; never the secret, the input text,
  or the vector.
- **Prompt injection surface.** The embedding boundary is a numeric transform of
  redacted text; no LLM consumes the embedded text and the rendered context is
  unchanged from the pre-slice behavior. No new injection surface.

## Fixes Applied

- `e48c07e1 fix: record retrieval strategy and semantic evidence in audit`
  resolves I-1 / K-4.

## Accepted Risks

- Deterministic local embeddings (character 3-gram hashing) are a stand-in for
  real OpenAI embeddings; ranking quality is bounded and out of scope for this
  foundation slice. The single adapter boundary
  (`FakeProviderGateway.embed` + `embedding_vector` column) keeps the swap
  non-invasive.
- `JSONField` vector storage without dimension validation (M-4) while there is
  one producer at dimension 64.
- `request_id`-based query-embedding dedup (M-1) matches generation-path
  semantics.
- Gateway record-creation duplication (K-1) deferred to the real-adapter slice.

## Verdict

**SECURITY APPROVED** after the `e48c07e1` audit fix. No Critical or Important
findings remain. Tenant isolation, redaction, idempotency, graceful
degradation, eager-call guard, and log hygiene all hold and match or exceed the
generation-path baseline.
