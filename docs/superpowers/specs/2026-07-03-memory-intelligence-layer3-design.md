# Memory Intelligence / Retrieval Layer 3 — Design

Date: 2026-07-03
Scope source: `private/engram-v1-gaps.md` section 2 ("Интеллект памяти / retrieval").
Status: approved for implementation (autonomous run, operator granted full autonomy).

## Problem

The docs-accuracy audit confirmed the memory-intelligence layer is the weakest
part of the product. Verified against code at `8e526f81`:

1. **Exact-match authority is inert.** No write path ever writes `symbols` or
   `exact_terms` into `Memory.metadata`, so `RetrievalDocument.symbols` is
   always `[]` and `exact_terms` reduces to the normalized title.
   The symbol tier (score 80) in `score_retrieval_document` is dead code and
   the exact-terms tier (score 60) only matches queries containing the title.
2. **Write-path integrity holes** (found during mapping, same domain):
   - console `approve_memory_candidate` and `edit_memory_body` never call
     `IndexMemoryVersion` — console-approved/edited memories are unsearchable;
   - `BuildWeeklyStructuredDigest` never creates a `MemoryVersion` or
     `RetrievalDocument` — weekly digests are structurally unretrievable;
   - console reject sets `Memory.status=REFUTED` while agent feedback sets the
     `Memory.refuted` boolean — two unsynchronized refutation signals, and
     neither console reject nor supersede syncs `RetrievalDocument` flags.
3. **Bundle carries no intelligence.** No `confidence`, no `kind`, `warnings`
   is a hardcoded `[]` in both `/v1/search` and `/v1/context/*`.
4. **No memory taxonomy.** `Memory.kind` only ever holds `'digest'`.
5. **Curator is dedup-only.** No contradiction detection, no confidence decay,
   escalation is a single confidence threshold, and curator audit is narrow
   (supersede/auto-reject only; auto-promote writes no audit at all).
6. **Reviewer decisions are not captured as reusable examples.**

## Goals

- Make exact-match retrieval an actual authority for symbols/identifiers.
- Close the write-path integrity holes so every approved memory is retrievable.
- Surface `confidence`, `kind`, and structured `warnings` on every retrieval
  surface (API, rendered context, MCP/CLI, console UI).
- Introduce a bounded memory-kind taxonomy written by real pipelines.
- Extend the curator with contradiction detection, deterministic escalation
  rules, confidence decay, and complete audit coverage.
- Persist human review decisions as immutable eval examples.

## Non-goals (explicit decisions)

- **No automated refute.** Refutation stays a human/feedback action. The
  automated part is contradiction *detection + escalation* (conservative:
  never silently removes an approved memory from retrieval).
- **No branch/environment filters on RetrievalDocument** (gaps §2 LOW item):
  deferred — memory is durable project knowledge; branch scoping has unclear
  product value. Recorded as a deliberate defer in the gaps file.
- No pgvector-accelerated near-dup search (perf follow-up, not behavior).
- No token-accurate packing, no ContextBundleStatus lifecycle work.

## Design

### Slice A — exact-term/symbol extraction + write-path integrity

**Deterministic extraction at index time.** New module
`apps/backend/engram/context/term_extraction.py`:

- `extract_symbols(title, body) -> tuple[str, ...]`: backtick-quoted
  identifiers, dotted paths (`a.b.c`), `name()` call forms, CamelCase and
  snake_case tokens (len >= 4). Cap 32, per-item length cap 120.
- `extract_exact_terms(title, body) -> tuple[str, ...]`: ticket ids
  (`#123`, `ABC-123`), error-class names (`*Error`, `*Exception`),
  UPPER_SNAKE env/const names, quoted literals. Min term length 4 after
  normalization (containment matching is bidirectional — short terms create
  false positives). Cap 32.

`IndexMemoryVersion` merges `memory.metadata['symbols'|'exact_terms']`
(explicit override channel, preserved) with extracted values, then
normalizes/dedupes. Extraction lives at the single RetrievalDocument
write choke point, so promotion, digest, import, and version-update paths
all benefit with one change.

**Integrity fixes** (each is a behavior fix with a reproducing test first):

- `approve_memory_candidate` calls `IndexMemoryVersion` (+ file_paths
  overwrite parity with `PromoteMemoryCandidate._index_memory_version`).
- `edit_memory_body` re-indexes the new version.
- `BuildWeeklyStructuredDigest` creates `MemoryVersion` v1 and indexes it at
  creation (parity with daily digest).
- Console reject also sets `refuted=True` and syncs `RetrievalDocument`
  rows (same `_sync_retrieval_documents` semantics as feedback); both
  supersede paths (curator + console) sync `RetrievalDocument.stale`.
- Backfill: management command `engram_backfill_retrieval_terms` re-derives
  symbols/exact_terms for existing documents (idempotent, bounded batches).

### Slice B — bundle intelligence surface: kind + confidence + warnings

**MemoryKind vocabulary** (module-level constant, NOT field choices — the
`TimestampedModel.save() -> full_clean()` behavior makes field choices a
production hazard for pre-existing rows): `decision`, `convention`, `gotcha`,
`architecture`, `incident`, `digest`; `''` = unspecified. Writers clamp to
the vocabulary; unknown values from providers become `''`.

Writers:
- `MemoryCandidate.kind` (new CharField, migration): distillation structured
  output gains an optional `kind` field (prompt + Anthropic tool schema +
  fake payload); observation path maps `observation_type` in
  {decision, architecture, convention, gotcha} to the same kind.
- Promotion copies candidate kind into `Memory.metadata['kind']`
  (`Memory.save()` already mirrors it into the column).

**Confidence + kind on surfaces:**
- `/v1/search` and `/v1/context/*` item payloads gain `confidence`
  (string decimal or null, matching inspection convention) and `kind`.
- `rendered_context` line becomes
  `- [M1] Title (kind, confidence 0.95)` with both parts optional.
- Inspection context-bundle item response, search-debug payload, MCP
  `engram_search` renderer, CLI `engram search` text mode, and the console
  pages (bundle detail, search debug) render kind + confidence using the
  existing `KindBadge`/`ConfidenceTrack` atoms. Frontend TS types updated in
  the same change.
- Optional `kinds` filter (list, validated) on search/context request
  serializers, applied as `memory__kind__in`.

**Structured warnings** — shared producer in context/search services;
shape `{code, message, memory_id|null}`:
- `budget_dropped`: N matches dropped by token budget (context only).
- `semantic_unavailable`: hybrid enabled but embedding resolution failed.
- `stale_match` / `refuted_match`: an excluded stale/refuted memory would
  have exact-matched the query (title + reason only, cap 3, second bounded
  query over excluded docs).
- `conflicting_memory`: an included memory has an unresolved
  `CONFLICTS_WITH` link (see Slice C).
Warnings are persisted in `ContextBundle.metadata['warnings']` (replay
returns identical payload), appended as a compact `> Warnings:` block to
`rendered_context`, and exposed via inspection.

### Slice C — curator: contradiction detection + escalation policy

**Contradiction detection** (requires `curator_llm_judge_enabled`, since it
extends the existing gray-band judge):
- Judge decision space gains `contradicts` (prompt, parser whitelist,
  Anthropic `emit_judgment` enum, fake payload unchanged: default stays
  `keep_both`; unknown decisions still fall back to `keep_both`).
- On `contradicts` (conservative flow — an LLM false positive must not
  silently hide approved memory):
  - the candidate is **held for review** (stays PROPOSED; conflict evidence
    entry `{'type': 'conflict', 'memory_id': ...}` appended to
    `candidate.evidence` so the review queue shows it);
  - `MemoryLink(existing_memory, CONFLICTS_WITH, target='candidate:<id>')`
    is created (LinkType gains CONFLICTS_WITH — migration; excluded from the
    public MemoryLink API choices like NARROWED_BY/SUPERSEDED_BY);
  - audit `MemoryConflictDetected` (see Slice D metadata contract);
  - the existing memory **stays APPROVED and retrievable** but now carries
    the warning marker from Slice B.
- Console review resolution: approving or rejecting the held candidate
  deletes the corresponding `CONFLICTS_WITH` link(s). `MemoryStatus.CONFLICT`
  remains a human-only status (documented).

**Escalation policy** — deterministic pre-promotion rules in
`CurateMemoryCandidate` (pure function `escalation_reason(candidate)`),
checked before auto-promote; a hit holds the candidate for review with an
audited reason:
- `security_sensitive`: title/body matches a sensitive-term list
  (settings-driven, env-overridable defaults: secret, credential, token,
  password, api key, private key, rbac, permission bypass, …);
- `org_wide_scope`: `visibility_scope == ORGANIZATION`;
- contradiction always escalates (above).
Escalation is fail-closed to review, never to auto-reject.

### Slice D — curator audit completeness

Standard writer `audit_curator_action(...)` in `curation.py`, used by ALL
curator outcomes — promote (passthrough/no-dup/judge-keep-both), supersede,
auto-reject, hold-for-review (threshold and escalation), conflict-detected:

- metadata (redacted via `redact_value`, consistent with the model-policy
  audit wrapper): `candidate_id`, `decision`, `reason`, `near_dup_score`,
  `threshold`, `source_observation_id`, `evidence_source_ids`, and for judge
  decisions the input window as `{policy_id, policy_version, provider,
  model, provider_call_record_id, candidate: {title (redacted, 120 chars),
  body_sha256, body_length}, existing_memory: {memory_id, title (redacted,
  120 chars), body_sha256, body_length}}`.
- Event types: `MemoryCuratorPromoted` (new), `MemorySuperseded`,
  `MemoryAutoRejected`, `MemoryCandidateHeldForReview` (reason now includes
  escalation codes), `MemoryConflictDetected` (new).

### Slice E — confidence decay

Weekly beat task `decay_memory_confidence` (Monday 04:00 UTC, after weekly
digest):
- Applies to APPROVED, non-stale, non-refuted memories with non-null
  confidence and kind != 'digest' whose `updated_at` is older than
  `ENGRAM_CONFIDENCE_DECAY_MIN_AGE_DAYS` (default 30).
- Each run subtracts `ENGRAM_CONFIDENCE_DECAY_STEP` (default 0.050) down to
  `ENGRAM_CONFIDENCE_DECAY_FLOOR` (default 0.200). Decimal-quantized (3 dp).
- Decayed memories whose confidence crosses <= 0.300 naturally enter the
  existing low-confidence review queue — decay funnels stale knowledge to
  humans instead of deleting it.
- Saving the memory bumps `updated_at`, which by design spaces decay steps
  at least MIN_AGE apart per memory (documented staircase behavior).
- Gate: `OrganizationSettings.confidence_decay_enabled` (default True,
  migration). Audit: one `MemoryConfidenceDecayed` event per project per run
  with counts + first 200 memory ids + step/floor.

### Slice F — reviewer decisions as eval examples

New append-only model `MemoryReviewExample` (core app, migration):
`organization`, `project`, `team (null)`, `item_type` (candidate|memory),
`item_id`, `action` (approve/edit/narrow/supersede/reject/archive/restore),
`snapshot` JSON (title, body, confidence, kind, visibility_scope, evidence —
captured BEFORE mutation), `curator_context` JSON (held reason /near-dup
score/conflict memory id when derivable from candidate evidence + audit),
`reason`, `actor_id`. Written in the same transaction by every console
review action. Export: management command `engram_export_review_examples`
(JSONL, org/project filters, redacted).

Also adds the console `restore` action (REFUTED/CONFLICT/archived memory →
APPROVED + reindex) — needed so reviewers can undo refutation/conflict, and
it produces the negative examples the eval set needs.

## Data model changes (all additive)

| Change | Migration |
|---|---|
| `MemoryCandidate.kind` CharField(40, blank, default '') | yes |
| `LinkType.CONFLICTS_WITH` enum value | yes (AlterField choices) |
| `OrganizationSettings.confidence_decay_enabled` bool default True | yes |
| `MemoryReviewExample` model | yes |

No RetrievalDocument schema changes; no destructive migrations.

## Rollout / compatibility

- All wire changes are additive fields; `warnings` already exists as `[]` in
  the envelope, so consumers coded against the contract keep working.
- `rendered_context` format gains suffixes and an optional warnings block —
  context/e2e tests asserting exact strings are updated in the same slice.
- Contradiction detection only activates where `curator_llm_judge_enabled`
  (default False) — no behavior change for orgs that never enabled the judge.
- Decay defaults to enabled but touches nothing younger than 30 days.
- Backfill command is operator-run (documented in ops docs), not automatic.

## Testing

TDD per slice; conventions per repo: `<module>_tests.py` next to module,
ad-hoc helper functions, gateway stubs subclassing `FakeProviderGateway`
patched via `monkeypatch.setattr` on the factory, pgvector `skipif` guard,
`django_db(transaction=True)` only for concurrency tests.

Key regression tests: console approve creates RetrievalDocument; weekly
digest is retrievable; symbol query hits the score-80 tier end-to-end;
warnings replay-stable; contradiction path holds candidate + links + audits;
escalation holds sensitive candidates; decay quantization + floor + queue
funnel; review example snapshot immutability.

E2E (golden path, fake provider): after `engram_memory_version` updates a
body to contain a backticked symbol, `engram_search` by that symbol returns
the memory via the symbol tier; context response carries kind/confidence
fields and empty-or-valid structured warnings.

## Slice → PR plan (waves)

| Wave | PRs (parallel, disjoint files) |
|---|---|
| 1 | A (extraction + integrity) ∥ C (contradiction + escalation) |
| 2 | B (surfaces; needs A+C) ∥ D (audit completeness; after C) |
| 3 | E (decay) ∥ F (review examples + restore) |

Each PR: spec reference, tests-first commits, docs updated in the same PR
(`docs/search-and-retrieval.md`, `docs/ai-workflow-loop.md`,
`docs/api-reference.md` where applicable), CI green before merge. The gaps
file `private/engram-v1-gaps.md` is updated (done/deferred) at the end.
