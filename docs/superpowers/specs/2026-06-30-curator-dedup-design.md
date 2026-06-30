# Design: Curator — semantic near-dup + supersede + auto-reject

> Roadmap Слой 3 P0 ("Curator: semantic near-dup detection (cosine threshold) +
> merge/supersede + version-linkage + авто-reject шума; сейчас dedup только
> byte-exact"). Base: current master (has distillation #35 + the pgvector harness).
> Tests on postgres+pgvector.

## Problem
Dedup is byte-exact only (`memory_candidate_content_hash`). Two paraphrased
observations create two separate APPROVED memories. Promotion is unconditional once
the gate passes (`ProcessObservationRecorded` `services.py:218-219`; `DistillSession`
`distillation.py:206-207` both call `PromoteMemoryCandidate` directly). The
supersede/narrow primitives (`console/services.py:436 supersede_memory`) need an
Identity actor and can't be reused from the worker. Embeddings exist only AFTER a
memory is indexed (`IndexMemoryVersion`), so candidates have none.

## Target
At the moment a candidate is about to be auto-promoted, the curator decides:
**reject** low-signal noise, **supersede-merge** a semantic near-duplicate, or
**promote clean**. The held-for-review path (gate fails) is unchanged.

## Design

### Config
`OrganizationSettings` (+ migration): `curator_enabled = BooleanField(default=True)`,
`near_dup_threshold = DecimalField(max_digits=4, decimal_places=3, default 0.850)`.
`settings.py`: `ENGRAM_NEAR_DUP_THRESHOLD = Decimal('0.850')` fallback. The threshold
is conservative (real embeddings differ from the FakeProviderGateway's 64-dim hashed
vectors; keep configurable). `curator_enabled=False` → pure passthrough (today's
behavior).

### New module `memory/curation.py`
- `embed_candidate(candidate) -> list[float] | None`: resolve the `task_type='embedding'`
  ModelPolicy + `get_provider_gateway(policy).embed(f'{title}\n{body}')`; return the
  vector, or `None` when no embedding policy exists (mirror
  `context/services.py resolve_query_embedding`'s try/except → None). None ⇒ skip
  semantics (graceful degradation to byte-exact).
- `find_near_duplicate(candidate_embedding, documents, threshold) -> (RetrievalDocument, float) | None`:
  pure Python cosine (reuse `context.services.cosine_similarity`) over the authorized
  docs' `embedding_vector` JSON; return the highest-scoring doc with score ≥ threshold,
  else None. (Python cosine — deterministic + testable; pgvector ANN is the separate
  hybrid-retrieval slice.)
- `is_low_signal(candidate) -> bool`: conservative noise heuristic over the REDACTED
  body — body length < `_MIN_SIGNAL_CHARS` (e.g. 24) OR body equals title OR body
  empty. (Deliberately narrow: false-positive auto-reject is destructive; borderline
  candidates should fall through to promote/review, not be dropped.)
- `supersede_memory_system(loser, winner, *, request_id, correlation_id) -> MemoryLink`:
  worker-side (no Identity): set `loser.stale=True` (save update_fields),
  `MemoryLink.objects.get_or_create(memory=loser, link_type=SUPERSEDED_BY,
  target=str(winner.id))` (idempotent via the existing unique constraint), write a
  system `AuditEvent(event_type='MemorySuperseded', actor_type='system',
  result=RECORDED, metadata={winner, score})`. Skip if `loser.stale` already True.
- `CurateMemoryCandidate.execute(CurateMemoryCandidateInput(candidate_id, correlation_id='')) -> CurateMemoryCandidateResult(decision, candidate, memory|None)`:
  1. lock the candidate (PROPOSED). If `not curator_enabled` → `promote_clean` via
     `PromoteMemoryCandidate` (passthrough).
  2. `is_low_signal` → set candidate `status=REJECTED`, audit
     `MemoryAutoRejected`, return `decision='rejected', memory=None`.
  3. `embed_candidate`; if a vector and a near-dup is found among
     `authorized_retrieval_documents` for the candidate's org/project →
     `PromoteMemoryCandidate` the NEW candidate (newest-wins) THEN
     `supersede_memory_system(loser=near_dup.memory, winner=new_memory)`; return
     `decision='superseded'`.
  4. else → `promote_clean` (`PromoteMemoryCandidate`); return `decision='promoted'`.

### Integration (both promotion paths call the curator instead of promote directly)
- `ProcessObservationRecorded.execute` (`services.py:218-219`): when the gate says
  promotable, call `CurateMemoryCandidate().execute(...)` instead of
  `PromoteMemoryCandidate().execute(...)`. Map the curator decision onto
  `MemoryCandidateWorkerResult` (memory=None + a `curated_decision` field on rejected/…;
  keep `duplicate=True` for superseded/replay). The held (not-promotable) branch is
  unchanged.
- `DistillSession.execute` (`distillation.py:206-207`): same swap — high-confidence
  candidates go through the curator.
- Retrieval already honors supersede (`authorized_retrieval_documents` filters
  `memory__stale=False`), so a superseded loser disappears from context bundles
  automatically — no retrieval change needed.

## Tests (postgres+pgvector, FakeProviderGateway deterministic embeddings)
`memory/curation_tests.py`: 
- `is_low_signal` matrix; `find_near_duplicate` (above/below threshold, empty docs,
  None embedding).
- `supersede_memory_system`: sets loser stale + SUPERSEDED_BY link + audit; idempotent.
- `CurateMemoryCandidate`: clean candidate → promoted + RetrievalDocument; near-dup of
  an existing memory → new promoted + old superseded (stale, link, audit); low-signal →
  rejected, no memory; `curator_enabled=False` → passthrough promote; None embedding →
  promote_clean (byte-exact fallback).
- Update `memory_worker_tests.py` + `distillation_tests.py` for the curator-routed
  promotion (still promote clean candidates; assert dedup/reject on the new cases).
- FULL local CI gate before push: `ruff check . && ruff format --check . && migrate &&
  makemigrations --check && pytest`.

## Out of scope (follow-up)
pgvector ANN near-dup scan (hybrid-retrieval slice); LLM-judged curation; merging
candidate bodies (we supersede, not textually merge).
