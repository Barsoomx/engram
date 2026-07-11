# Checkpoint 9 — Retrieval And Curation Convergence

Date: 2026-07-11
Status: focused implementation specification
Roadmap gate: Checkpoint 9, C9.1 and C9.2
Baseline inspected: `master` at `79ddb15a`

## Scope

Checkpoint 9 makes semantic retrieval a bounded scope-first PostgreSQL query and removes independent ranking from search debug. It has two serial slices:

1. C9.1 adopts one authorized pgvector primitive for context, search, and curation with behavior parity.
2. C9.2 measures lexical plans, conditionally indexes, delegates debug to production search, and proves load SLOs.

This extracts Checkpoint 9 of `2026-07-09-autonomous-memory-loop-roadmap.md`. It authorizes no production access, SSH, deploy, D2, repair, or production data collection; evidence uses deterministic local/CI data.

CP9 follows CP5, CP6, and Memory CI. Refresh adapters after those merges while preserving this scope/result/fallback contract; renamed interfaces do not permit a second algorithm.

## Goal And Success Boundary

CP9 is complete only when:

- every semantic query applies organization/project/visibility/lifecycle/kind/provenance/exclusions before distance;
- retrieval and curation share `SearchScopedVectors`, with no raw queryset or global id universe;
- context/search preserve accepted exact/semantic/lexical/warning/citation/explanation behavior;
- curation preserves its threshold, gray-band, judge, and transition behavior;
- search debug receives ranks and scores from the production search core;
- lexical indexes require plans proving an SLO breach and material improvement;
- query/search/context/curation-shortlist gates pass on the frozen corpus;
- index size, write cost, forward operation, and reverse operation are recorded;
- all local Python verification runs in the backend Compose container.

## Current Seams And Defects

The current baseline has useful behavior but four divergent execution paths:

- `authorized_retrieval_documents` loads all authorized rows for exact scoring.
- `semantic_retrieval_matches_pgvector` uses preloaded ids, then recomputes cosine in Python.
- curation synthesizes `EffectiveScope`, loads all JSON vectors, and scans in Python.
- debug independently repeats authorization, scoring, embedding, lexical recall, and packing.

`core_retdoc_emb_hnsw` already indexes the 1536-dimensional pgvector column. `pg_trgm` is enabled, but `full_text` has no FTS/trigram index; measure before adding one.

Search/context limits remain 1–10. Semantic score `30`, two-decimal term/reason, and debug's four-decimal similarity are compatibility contracts.

## Binding Design Decisions

1. Add one small retrieval module; do not build a generic search framework.
2. Keep exact scoring, context packing, warnings, and curation decisions with current owners.
3. Share authorized predicates and vector selection so exact/vector scope cannot drift.
4. Split public search into API-key adapter plus authorized core; debug calls the core.
5. Pass immutable `AuthorizedRetrievalScope`, never raw queryset/ids or nullable global scope.
6. Require a positive bounded result limit on every vector call.
7. PostgreSQL cosine distance is authoritative for pgvector rows; never recalculate from JSON.
8. JSON fallback covers unsupported/incomplete storage only, never database errors.
9. Preserve HNSW `m`, `ef_construction`, `ef_search`, batch size, and concurrency.
10. A passing lexical baseline produces no C9.2 migration.

## C9.1 Shared Scope Interface

Create `apps/backend/engram/context/retrieval.py`. It owns these exact public
types and entry points:

```text
AuthorizedRetrievalScope
    organization_id: UUID; project_id: UUID
    team_ids: tuple[UUID, ...]; kinds: tuple[str, ...] = ()
    require_provenance: bool = False

ScopedVectorSearchInput
    scope: AuthorizedRetrievalScope; query_vector: tuple[float, ...]
    minimum_similarity: float; limit: int
    exclude_document_ids: tuple[UUID, ...] = ()

ScopedVectorCandidate
    document: RetrievalDocument; distance: float; similarity: float
    source: Literal['pgvector', 'json_fallback']

ScopedVectorSearchResult
    candidates: tuple[ScopedVectorCandidate, ...]
    backend: Literal['pgvector', 'python', 'mixed', 'none']; fallback_document_count: int

authorized_scope_from_effective_scope(
    *, scope: EffectiveScope, project: Project,
    requested_team_id: UUID | None, kinds: tuple[str, ...] = (),
    require_provenance: bool = False,
) -> AuthorizedRetrievalScope

load_authorized_retrieval_documents(
    scope: AuthorizedRetrievalScope, *, include_embeddings: bool = False,
) -> tuple[RetrievalDocument, ...]

SearchScopedVectors.execute(
    data: ScopedVectorSearchInput,
) -> ScopedVectorSearchResult
```

`MAX_SCOPED_VECTOR_RESULTS` is `100`. `execute` rejects limits outside
`1..100`, non-finite similarity floors, floors outside `-1..1`, duplicate
exclusion ids or malformed ids, and non-empty vectors whose length is not the repository
`EMBEDDING_DIMENSION`. An empty or all-zero vector returns an empty result with
backend `none` and performs no distance query.

Scope construction sorts/deduplicates team and kind ids; vector input rejects
duplicate exclusion ids. The effective-scope factory verifies:

- `scope.organization_id == project.organization_id`;
- `project.id` is present in `scope.project_ids`;
- a requested team is present in `scope.team_ids`;
- a requested team narrows `team_ids` to that single id;
- without a requested team, the effective team tuple is preserved;
- kinds are already validated repository memory kinds.

The admin search-debug adapter does not use this factory for a full organization
administrator whose effective project tuple is intentionally empty. It first
passes its existing explicit project/team authorization checks, then constructs
the same value from the allowed ids. Curation constructs the value from its
locked internal candidate only after model scope consistency is validated.
No function accepts a null/global project or a scope-disable flag.

## Authorized Query Predicate

`retrieval.py` owns the private candidate-query predicate used by document
loading, pgvector selection, and JSON fallback. It is exactly:

```text
RetrievalDocument.organization_id = scope.organization_id
AND RetrievalDocument.project_id = scope.project_id
AND Memory.status = approved
AND Memory.stale = false
AND Memory.refuted = false
AND RetrievalDocument.stale = false
AND RetrievalDocument.refuted = false
AND (
    RetrievalDocument.visibility_scope = project
    OR (
        RetrievalDocument.visibility_scope = team
        AND RetrievalDocument.team_id IN scope.team_ids
    )
)
```

An empty `team_ids` tuple emits only the project-visibility branch; it must not
emit `IN ()` and must not broaden to all teams. Organization-visible and unknown
visibility values remain excluded because current production retrieval does not
inject them.

When `kinds` is non-empty, add `Memory.kind IN scope.kinds`. When
`require_provenance` is true, require a non-null
`MemoryVersion.source_observation_id`. Apply `exclude_document_ids` in the same
SQL query before distance annotation. Select the current `memory`,
`memory_version`, and `team`; never load a foreign-scope body and discard it in
Python.

The scope predicate is applied before `CosineDistance`, `ORDER BY`, or `LIMIT`
in the logical query. Tests inspect generated SQL and results; a planner may use
a global physical HNSW index internally, but the application never emits a
logically global vector query.

## PostgreSQL Distance And Ordering Contract

For populated pgvector rows, annotate once with:

```text
distance = CosineDistance('embedding_pgvector', query_vector)
```

Filter with a maximum distance of
`1 - minimum_similarity + PGVECTOR_FLOOR_DISTANCE_EPSILON`, order by distance
ascending, `memory_id` ascending, then retrieval-document id ascending, and
apply `limit` in SQL. The secondary order preserves the effective current input
order because `RetrievalDocument.Meta.ordering` already uses `memory_id` inside
one organization/project.

For each returned row, convert distance to a finite float, calculate
`similarity = min(1.0, max(-1.0, 1.0 - distance))`, discard it when unrounded
similarity is below the floor, and retain both raw values. Round only for public
explanations. The epsilon avoids premature boundary loss; the final raw check
prevents a truly below-floor row from leaking through.

pgvector rows never pass through `cosine_similarity` again. Every vector query
contains distance order and a limit; calls above 100 are rejected.

## Fallback Contract

Fallback exists for compatibility, not as a second normal algorithm.

1. When pgvector and `CosineDistance` are available, query populated
   `embedding_pgvector` rows in PostgreSQL.
2. In a second query, load only in-scope rows whose pgvector value is null and
   whose JSON `embedding_vector` is non-empty.
3. Score those missing rows with the existing Python cosine helper, apply the
   same floor and tie order, merge with PostgreSQL candidates, and truncate to
   the requested limit.
4. When the optional pgvector field or distance expression is unavailable,
   score all in-scope JSON vectors in Python with the same merge/order contract.
5. Report `python` or `mixed` plus the exact fallback row count in the result
   and structured retrieval telemetry.

Never catch a database error, timeout, cancellation, malformed stored vector,
or dimension error and rescan in Python. Provider absence remains caller-owned:
search/context keep exact results and warnings; curation keeps CP5 failure-safe
behavior.

The SLO requires at least 99.5 percent pgvector coverage. More than 0.5 percent
fallback marks the run degraded and creates backfill evidence, not tuning work.

## RetrievalMatch Adaptation And Parity

Context and search adapt each `ScopedVectorCandidate` to the existing
`RetrievalMatch` without changing its public shape:

```text
score = 30
matched_terms = (f'cosine {candidate.similarity:.2f}',)
inclusion_reason = f'semantic match: cosine {candidate.similarity:.2f}'
```

The accepted corpus freezes:

- file-path score `100`, symbol score `80`, exact-term score `60`, full-text
  score `40`, semantic score `30`, lexical-recall score `20`, and filter-only
  score `1` remain unchanged;
- exact matches still precede the semantic/lexical tail;
- search exact ties remain updated time, title, and document id ordered;
- context exact ties retain confidence before updated time/title/id;
- context filter-only digest capping remains unchanged;
- semantic ties use memory id then retrieval-document id;
- lexical recall and reciprocal-rank-fusion formulas remain unchanged;
- public citations, redaction, kinds, confidence, scope evidence, warnings,
  retrieval strategy, audit metadata, and token-budget behavior remain stable;
- flags off remain byte-equivalent to the current exact-only paths;
- pgvector and JSON fallback return the same ids/order/explanations on the
  accepted corpus, with raw similarity tolerance `1e-6`;
- mixed populated/null vector data merges before limiting;
- a row below the semantic floor does not appear because formatting rounds it
  to the floor.

For search and context, call `SearchScopedVectors` with `limit=data.limit`, the
same authorized scope as exact loading, and the ids of exact matches. Existing
external request limits therefore bound semantic work at ten. Curation calls it
with `limit=1`, its gray-band floor, and no arbitrary wider corpus.

## Curation Convergence

`CurateMemoryCandidate._curate_with_embedding` replaces
`_authorized_documents` plus `find_near_duplicate` with one
`SearchScopedVectors` call. Its scope is the candidate organization/project,
the candidate team when present, project-visible plus that team's rows, current
approved/non-stale/non-refuted state, and no kind restriction unless CP5 has
already frozen one.

Pass `threshold - _GRAY_BAND_WIDTH` when the judge is enabled and `threshold`
otherwise. A result becomes the existing document/raw-similarity `near_dup`.
The vector service never judges, locks, audits, writes, or chooses a transition.

Similarity remains comparison input. The CP5 orchestrator retains all
failure-safe and destructive-transition guards. CP9 does not reinterpret a
distance, change thresholds, widen team visibility, or turn an unavailable
embedding into a semantic decision.

Parity covers duplicate, gray-band, below-floor, tie, foreign scope, lifecycle,
and missing-vector cases. Selected memory, raw score, judge route, audit, and
decision match the accepted pre-CP9 corpus.

## Production Search Core

Refactor `apps/backend/engram/search/services.py` without changing the public
`SearchInput`, `SearchResult`, or HTTP response. `SearchMemories` remains the
API-key adapter and delegates ranking to this exact internal interface:

```text
AuthorizedSearchInput
    organization: Organization; project: Project; team: Team | None
    retrieval_scope: AuthorizedRetrievalScope
    query: str; file_paths: tuple[str, ...]; symbols: tuple[str, ...]
    limit: int
    request_id: str; trace_id: str
    explain: bool = False

RetrievalExclusion
    document: RetrievalDocument
    reason: Literal[
        'not_approved', 'stale', 'refuted', 'team_not_in_scope',
        'visibility_not_injectable', 'below_relevance', 'token_budget'
    ]

RetrievalExplanation
    scope_filters: dict[str, object]; candidate_universe_count: int
    exact_matches: tuple[RetrievalMatch, ...]
    semantic_candidates: tuple[ScopedVectorCandidate, ...]
    lexical_matches: tuple[RetrievalMatch, ...]
    excluded: tuple[RetrievalExclusion, ...]
    semantic_enabled: bool; lexical_enabled: bool

AuthorizedSearchResult
    matches: tuple[RetrievalMatch, ...]; explanation: RetrievalExplanation
    semantic_unavailable: bool

SearchAuthorizedMemories.execute(
    data: AuthorizedSearchInput,
) -> AuthorizedSearchResult
```

Before any setting/content/provider read, `execute` requires organization and
project ids to equal the scope, project organization to match, and any team to
belong to the scope organization and `team_ids`; mismatch is access denial.
Kinds come only from `retrieval_scope.kinds` so two filters cannot drift.

`SearchAuthorizedMemories` owns the exact, embedding, shared-vector, lexical,
fusion, result-limit, and explanation sequence used by production search. It
does not resolve credentials or create projects. `SearchMemories` remains the
authorization/project/team/warning adapter and returns the unchanged result.

`explain=False` skips excluded-row classification. `explain=True` may run a
metadata-only scoped classification query through the same predicate owner; it
never recomputes ranking, embedding, distance, lexical score, or fusion.

## Authorization Order

Every external path keeps authorization before content, embedding, distance,
lexical, explanation, or packing work.

### Public search

1. Validate request bounds.
2. `ResolveApiKeyScope` validates key/organization state, requested
   project/team, and `search:query`.
3. Resolve the authorized project/team and build `AuthorizedRetrievalScope`.
4. Load settings/exact candidates; embed only when hybrid is enabled and exact
   results do not fill the limit.
5. Vector/lexical rank, warn, and render.

### Context

1. Validate, then resolve `memories:read` plus requested project/team.
2. Resolve the project and reauthorize any immutable replay.
3. Resolve team/session identities and build `AuthorizedRetrievalScope`.
4. Exact/embed/vector/lexical rank and pack, then persist/audit only selection.

### Search debug

1. DRF enforces authentication, active organization, and `memories:read`.
2. Validate the serializer, resolve the project inside that organization (`404`
   otherwise), then enforce project grant/full-admin and team narrowing.
3. Build scope, call `SearchAuthorizedMemories(explain=True)`, and map it.

### Curation

1. Load/lock via CP5 and validate candidate state/scope.
2. Run deterministic gates and resolve candidate-scoped embedding policy.
3. Build the scope, invoke shared vector selection, then pass the comparison to
   the CP5 judge/orchestrator.

Negative tests assert that a denied path performs zero provider calls and zero
distance/lexical queries. Authorization errors are never converted into empty
search results.

## C9.2 Search-Debug Convergence

`ReplaySearchDebug` remains the admin DTO adapter but must not import or call
`cosine_similarity`, `score_retrieval_document`, `resolve_query_embedding`,
`lexical_recall_matches`, semantic matching helpers, or fusion helpers.

It performs only the explicit admin authorization steps, builds an
`AuthorizedSearchInput` with `limit=20` and `explain=True`, calls
`SearchAuthorizedMemories`, and maps:

- exact matches from `explanation.exact_matches`;
- semantic candidates and four-decimal score from the shared raw similarity;
- lexical candidates from `explanation.lexical_matches`;
- packed context from `AuthorizedSearchResult.matches`;
- exclusions and scope filters from `explanation`;
- semantic/lexical enabled flags from `explanation`.

The HTTP field names and value types remain unchanged. Candidate ordering and
packed results now intentionally match production search for the same scope,
signals, feature flags, and limit. A static dependency test fails if debug
reintroduces ranking/provider imports or direct `RetrievalDocument` distance
annotation.

The diagnostic universe remains organization/project scoped. It may explain
stale, refuted, unapproved, non-injectable, and out-of-team rows exactly as the
current admin contract does, but those rows never enter production ranking and
never trigger provider work. No non-admin endpoint receives this trace.

## Lexical Plan And Index Decision Contract

C9.2 first runs unchanged full-text, trigram, and combined queries. After `ANALYZE`, record `EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS, FORMAT JSON)`, one cold run, and five warm runs.

Commit `docs/performance/retrieval-c9/baseline-plans.json`, `final-plans.json`, and `index-decision.md`.

The decision records corpus/query fingerprints, versions/settings, plan nodes/times/buffers/WAL, index size, write throughput, and each outcome.

An index is permitted only when all are true:

1. the relevant lexical p95 breaches the numeric SLO below;
2. the plan scans or filters at least 20 percent of scoped eligible rows;
3. the proposed index is used in at least four of five warm plans;
4. p95 improves by at least 20 percent on identical queries/data;
5. accepted result ids, order, matched terms, and explanations are unchanged;
6. indexed write throughput regresses by no more than 10 percent;
7. the reversible migration and measured index size are recorded.

Only a production-expression-matched FTS GIN and `full_text` `gin_trgm_ops` GIN are pre-authorized, named `core_retdoc_fulltext_fts_gin` and `core_retdoc_fulltext_trgm_gin`. Other shapes need a spec amendment.

If baseline passes or any criterion fails, record `no index` and create no migration. Do not change dictionaries, tokenization, thresholds, RRF, HNSW, or semantics to favor an index.

## Migration Ownership And Rollback

C9.1 owns no model or migration change. The existing pgvector column and HNSW
index remain intact.

If and only if the evidence contract approves a lexical index, the single C9.2
migration owner modifies `apps/backend/engram/core/models.py` and runs:

```text
python manage.py makemigrations core --name retrieval_lexical_indexes
```

The Django-emitted next leaf with basename `retrieval_lexical_indexes` is the
only CP9 migration. It is rebased on the then-current CP8 migration leaf, uses
`atomic = False`, and creates approved indexes concurrently with reversible
concurrent drops. `SeparateDatabaseAndState` keeps Django model state aligned
when raw concurrent SQL is required. Migration tests inspect forward and
reverse state and exact index definitions.

Behavior rollback order is:

1. turn off existing hybrid/lexical organization flags for an affected scope;
2. roll back the application adapter to the prior exact/Python-compatible path;
3. leave additive indexes present while code rollback is assessed;
4. reverse the lexical index migration only for demonstrated planner/write
   harm, using its concurrent drop;
5. never delete embeddings, retrieval documents, memory versions, audit rows,
   or context bundles.

The Python fallback remains sufficient for a short application rollback, but
it is not an accepted steady-state performance mode.

## Production-Shaped Benchmark Corpus

Create a seed-`909` benchmark command restricted to a database ending `_benchmark`; it has no remote/production mode:

```text
python manage.py engram_benchmark_retrieval --seed 909 --documents 200000 --warmups 50 --samples 500 --output /artifacts/retrieval-c9.json
```

The canonical corpus contains 200,000 retrieval documents:

- 10 organizations, 10 projects per organization, 2,000 documents per project;
- five teams per project;
- 70 percent project-visible, 25 percent team-visible, 5 percent lifecycle/visibility controls;
- 99.5 percent populated 1536-dimensional pgvector+JSON and 0.5 percent JSON-only;
- six memory kinds with fixed decision/gotcha/digest skew;
- deterministic exact signals, text/typos, semantic neighbors, ties, and controls;
- foreign-scope near neighbors intentionally closer than authorized rows.

The query set contains 100 fixed requests: 20 exact, 20 semantic-only, 20
full-text, 20 trigram, and 20 hybrid/fusion. Each has expected authorized ids,
leg membership, order, and explanation values. Ground truth is generated once
from exhaustive same-scope scoring and committed as
`apps/backend/engram/context/fixtures/retrieval_c9_ground_truth.json`.

Before measurement, run migrations, seed, `VACUUM (ANALYZE)`, 50 warm-up calls
per scenario, then 500 measured calls. Use immediate deterministic provider
stubs; provider/network latency is reported separately and excluded from these
database/application SLOs.

## SLO Contract

On the canonical corpus and the CP9 CI runner:

| Surface | Load | p95 | p99 |
|---|---:|---:|---:|
| scoped vector top 10 | 16 concurrent readers | <= 100 ms | <= 200 ms |
| lexical leg | 16 concurrent readers | <= 150 ms | <= 300 ms |
| authorized production search, limit 10 | 16 concurrent readers | <= 250 ms | <= 500 ms |
| context rank, pack, and local persistence | 8 concurrent requests | <= 350 ms | <= 700 ms |
| curation scoped shortlist, limit 1 | 8 concurrent readers | <= 100 ms | <= 200 ms |

Every surface also requires:

- zero unauthorized ids and zero cross-scope explanation values;
- zero request errors or timeouts in 500 measured calls;
- 100 percent exact/lexical accepted-corpus parity;
- 100 percent accepted semantic top-result parity and recall@10 at least 0.99
  against exhaustive same-scope vector ground truth;
- result and explanation determinism across three repeated seeded runs;
- no query without organization and project predicates;
- no vector call above the 100-row service ceiling;
- write throughput with approved lexical indexes at least 90 percent of
  baseline.

Wall-clock assertions do not run in the ordinary backend test suite. The
dedicated workflow owns a stable runner and uploads raw samples, percentiles,
plans, query counts, errors, fallback counts, recall, index sizes, and write
throughput. CP9 cannot merge on a relative improvement alone; all absolute
thresholds above must pass.

## Files And Serial Ownership

C9.1 shared owner creates `apps/backend/engram/context/retrieval.py` and
`apps/backend/engram/context/retrieval_tests.py`, then modifies
`apps/backend/engram/context/services.py` and
`apps/backend/engram/context/services_tests.py` only for the frozen seam.

After that interface commit, disjoint adapters may proceed:

- search: `apps/backend/engram/search/services.py`, `apps/backend/engram/search/services_tests.py`, and `apps/backend/engram/search/search_api_tests.py`;
- curation: `apps/backend/engram/memory/curation.py` and `apps/backend/engram/memory/curation_tests.py`;
- debug: `apps/backend/engram/console/search_debug_service.py`, `apps/backend/engram/console/search_debug_service_tests.py`, and `apps/backend/engram/console/views/search_debug_tests.py`;
- benchmark: `apps/backend/engram/core/management/commands/engram_benchmark_retrieval.py`, its adjacent `engram_benchmark_retrieval_tests.py`, `apps/backend/engram/context/fixtures/retrieval_c9_ground_truth.json`, and `.github/workflows/retrieval-performance.yml`;
- plan/index: `apps/backend/engram/context/retrieval_query_plan_tests.py`, the three `docs/performance/retrieval-c9/` evidence files, `apps/backend/engram/core/models.py`, and the conditional migration.

`context/services.py`, `search/services.py`, `curation.py`, debug service, and
`core/models.py` each have one writer at a time. The main orchestrator freezes
the interface commit before adapter branches and alone integrates Git history.

No frontend file is required because the debug HTTP shape remains stable. No
serializer/view change is owned unless a focused parity test proves the
existing adapter cannot pass the frozen input without one; such a change must
not expand a public field or limit.

## RED Tests Before C9.1 Cutover

1. Scoped top results include only eligible project/requested-team rows despite closer foreign/lifecycle controls.
2. Empty team scope emits project-visible results only.
3. Kinds, provenance, and exclusions occur in vector SQL before ordering.
4. SQL has organization/project predicates, distance order, and a limit; no unscoped constructor exists.
5. pgvector uses one distance query, loads no JSON vector, and renders the current explanation from raw similarity.
6. Mixed/null rows merge before limiting and report the exact fallback count.
7. pgvector absence uses scoped JSON fallback with parity ids/order/explanations.
8. Database timeout/error surfaces once without Python rescan.
9. Invalid limit/floor/vector/id fails before content query.
10. Floor/epsilon excludes a raw below-floor row even when rendering rounds up.
11. Distance ties are memory-id/document-id stable in PostgreSQL, Python, and mixed modes.
12. Search/context flag-off responses remain byte-equivalent.
13. Their semantic ids/order/citations/warnings/strategy/terms/explanations match the corpus.
14. Context confidence/digest ordering and public-search exact ordering remain distinct.
15. Curation keeps the same duplicate, gray-band, and below-floor candidate/score.
16. Foreign/lifecycle rows never affect curation judge, transition, or audit.
17. Denied search/context performs zero embedding and distance calls.

## RED Tests Before C9.2 Cutover

1. Debug fails while owning ranking imports and passes after exactly one production-core delegation.
2. Production/debug exact, semantic, lexical, and final ids/order match for identical authorized input.
3. Debug's four-decimal score and production's two-decimal reason derive from one raw candidate.
4. Debug preserves fields/types/kinds/confidence/scope/count/exclusions/flags.
5. Every auth denial precedes provider/vector/lexical calls.
6. Full-admin and team narrowing never create unscoped retrieval.
7. Baseline JSON plans cover FTS, trigram, combined, vector top 10, and curation top 1.
8. An approved index proves four-of-five use, 20 percent p95 gain, parity, and at most 10 percent write loss.
9. A non-beneficial candidate records `no index` and no schema diff.
10. Approved-index forward/reverse tests verify name, opclass/expression, concurrency, and model state.
11. Benchmark refuses a non-`_benchmark` database and emits its versioned schema.
12. Three seeded runs are deterministic and pass SLO/recall/leakage/fallback/error gates.

## CI And Verification Gates

Run focused correctness gates in the backend Compose container:

```text
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/context/retrieval_tests.py engram/context/services_tests.py \
  engram/search/services_tests.py engram/search/search_api_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/memory/curation_tests.py engram/console/search_debug_service_tests.py \
  engram/console/views/search_debug_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/context/retrieval_query_plan_tests.py \
  engram/core/management/commands/engram_benchmark_retrieval_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python manage.py makemigrations --check --dry-run
docker compose -f deploy/compose/docker-compose.yml run --rm api python manage.py check
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  ruff check engram/context engram/search engram/memory/curation.py \
  engram/console/search_debug_service.py
git diff --check
```

Run containerized `retrieval-performance` on the exact head SHA. It creates
`engram_benchmark`, migrates, benchmarks, checks ground truth/SLOs, and uploads
JSON/plans. Record command, exit code, counts, workflow URL/SHA, database/vector
versions, and artifact digest.

The full backend suite/migration check also pass. SQLite, mocked ORM, host-side
Python, or reduced-corpus timing is not acceptance evidence.

## Non-Goals

- No production query, traffic replay, data copy, deployment, SSH, D2, canary,
  or historical mutation.
- No new vector database, Qdrant adapter, model reranker, embedding model, or
  embedding dimension.
- No HNSW parameter, worker concurrency, retry, batch-size, queue, or provider
  tuning.
- No new public search/debug field, request limit, ranking weight, similarity
  floor, lexical threshold, RRF constant, curation threshold, or judge policy.
- No organization-wide retrieval and no implicit all-team scope.
- No semantic decision inside the vector primitive.
- No broad split of `context/services.py` beyond the shared scope/vector seam.
- No speculative full-text/trigram index and no migration when the baseline
  already passes.
- No frontend redesign or debug-only ranking path.

## Stop Conditions

Stop C9.1 if the shared service needs a nullable/global scope, if a call site
cannot authorize before embedding/query work, if direct database similarity
cannot reproduce accepted explanations, if curation would change a semantic
decision, or if fallback would hide a database failure.

Stop C9.2 if debug parity requires a second ranking algorithm, if an index is
not supported by before/after plans, if it changes accepted results, if write
throughput falls more than 10 percent, if the migration cannot reverse online,
or if any SLO is measured only by weakening the corpus or gate.

An SLO miss does not authorize threshold, HNSW, batch, worker, or concurrency
tuning. Record the decisive plan/load evidence and open a focused follow-up
design before changing those controls.

## Acceptance

C9.1 is accepted when the scope-first interface is frozen, all production
semantic callers use `SearchScopedVectors`, SQL is bounded and logically
scoped, pgvector scores are authoritative, explicit fallback remains scoped,
and retrieval/curation parity RED tests are green.

C9.2 is accepted when debug delegates production search, lexical before/after
evidence supports either the exact approved migration or a recorded no-index
decision, migration rollback is proved when applicable, the canonical load run
passes every absolute SLO and recall/leakage gate, and the full backend CI is
green on the same SHA.
