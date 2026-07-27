# Curation Outage Resilience — Design

Date: 2026-07-27
Status: awaiting user approval
Origin: production audit of `engram.tools.byster.one` (stand on `sha-6087ca8`)

## 1. Problem

A DeepSeek account ran out of balance. Instead of pausing cheaply, the curation
pipeline spent money retrying, then permanently killed the work it could not do.

Observed on the stand (evidence collected 2026-07-27):

- DeepSeek `402` from 2026-07-20 (82 errors), escalating 07-23 (8 164),
  07-24 (16 751), 07-25 (4 690). No curation calls at all after 07-25.
- **2 991 `candidate_decision` works in `terminal_failure`** with
  `failure_streak=12`; 2 983 of them died on 07-25.
- **5 902 candidates stuck in `proposed`**; last `promoted`/`rejected` was 07-23.
- **109 `session_distillation` works in `blocked`** since 07-24.
- 34 785 embedding calls for 4 061 distinct `request_id` — **8.57× duplication**,
  30 724 wasted paid calls. This also drove OpenAI into `429`.
- 10-day spend $170.44, of which **$23.15 (13.6%) was literal duplicate
  re-billing** (`curation-decision` $15.34, `distill-stage` $7.81).

The money was a symptom. The durable damage is 2 991 dead works plus a recovery
path that would not have worked.

## 2. Root causes

### D1 — the 402 signal is swallowed inside curation

`work_failures.py:88` classifies `401/402/403` as
`CONFIGURATION / provider_account_unavailable`, which sends a work to `blocked`
with no retries. That is the correct designed behaviour, and
`session_distillation` reaches it — hence its 109 `blocked` works.

`candidate_decision` never reaches it, via two distinct leaks:

- `curation.py:1061` — `resolve_candidate_embedding` catches
  `(ModelPolicyError, ProviderSecretError)` and returns `None`; the caller then
  raises `MemoryTransitionError('embedding_provider_unavailable')`, which maps to
  `PROVIDER_TRANSIENT`. The HTTP status is discarded.
- `curation.py:1325` — the judge's `except` block builds the transition error as
  `getattr(error, 'code', None) or 'judge_provider_unavailable'`. For a
  `ModelPolicyError` that yields the **provider's own** code,
  `provider_http_error`, which is absent from `_CURATION_TRANSITION_CODE_MAP` and
  therefore classifies as **`UNEXPECTED`** — backoff `(300, 21600)`, up to six
  hours, still terminalizing at `failure_streak=12`.

Production run records confirm the second leak dominates:

| failure_class | failure_code | runs |
|---|---|---|
| `unexpected` | `unexpected_exception` | **29 513** |
| `provider_transient` | `embedding_provider_unavailable` | 2 773 |
| `provider_transient` | `judge_policy_denied` | 2 592 |
| `provider_transient` | `judge_invalid_output` | 829 |

The same 402, on the `session_distillation` path, was recorded correctly as
`configuration / provider_account_unavailable` (168 runs) — one outage, two
different fates, decided purely by which `except` block saw it.

The code passthrough is the more general defect: **any** provider error code
absent from the curation map silently degrades to `UNEXPECTED`. Fixing only the
402 case would leave that hole open, so the fix constrains the code to the known
curation vocabulary rather than trusting whatever the provider layer supplies.

Arithmetic confirms the spend: 2 991 works × 12 attempts ≈ 35 892 ≈ the
34 785 embedding calls actually observed.

### D2 — an account block never self-heals

`work_execution.py:224` derives `blocked_configuration_fingerprint` from model
policy, provider secret, envelope and organization settings. **Provider balance
is external state and is not part of it.**

A topup therefore changes no fingerprint, so `_repair_action`
(`candidate_work_reconciler.py:277`) returns `skip` and blocked works stay
blocked indefinitely. The 109 `session_distillation` works have been blocked
since 07-24 and would have remained blocked after the planned topup.

This defect is invisible until someone tops up and nothing recovers.

### D3 — no checkpoint between paid steps

`curation.py:1297` `_settle_model_decision` runs: paid embedding (1306) →
shortlist → paid judge (1323) → transition. Any failure below re-runs the whole
sequence from scratch. `MemoryCandidate` has **no embedding field**, so the
vector lives only in a local variable.

`ProviderCallRecord` stores no response payload (`prompt_retained: False`), so
gateway-level "return the cached result" is impossible — `_log_repeat_attempt`
(`services.py:734`, unchanged on `origin/master`) can only warn, and the call is
still made and billed. Any fix must checkpoint at the caller.

### D4 — `reembed_missing_embeddings` is 100% broken

`context/services.py:861` calls `create_embedding_work_and_signal` outside a
transaction, but `workflow_work.py:843` requires an active one, raising
`TransactionManagementError`. The `except` clause only catches
`(ContextIndexError, ValueError)`, so the whole task dies on the first document.
Broken since #230 (2026-07-10); 1 434 failed runs in 48 h. Impact is currently
small (4 eligible documents, 36 `RetrievalDocument` without pgvector) but it is
permanent log noise hiding real failures.

### D5 — stuck leases

Two `session_distillation` works have been `leased` since 07-18/07-19 with
`lease_expires_at` long past. `_repair_action` handles expired leases, but only
for `candidate_decision`; the session path does not reclaim them.

### D6 — idle reconciler churn

`retry_failed_distillations` returned `{'retried': 0, 'reconciled': 0,
'unlinked': 54}` in **all 48 runs** over 24 h — the same 54 works re-unlinked
every 30 minutes.

## 3. Approach

Chosen: **preserve the failure signal, and make recovery explicit.**

Rejected alternatives:

- *Provider health probe feeding the fingerprint* — would auto-unblock after a
  topup, but adds a scheduler and health state, and a flapping probe would mass
  unblock/reblock. Topups are rare and human-initiated; a manual command is
  proportionate.
- *Gateway circuit breaker* — protects all callers, not just curation, but does
  not by itself fix terminalization or recover works. Correct classification
  already yields fail-fast. Complementary, not required now.

## 4. Slices

### Slice 1 (P0) — make the topup actually restore the pipeline

**D1.** Stop discarding provider failure classes in curation. `ModelPolicyError`
and `ProviderSecretError` from the embedding and judge calls must reach
`translate_failure` with their `http_status` intact, so `402` classifies as
`CONFIGURATION / provider_account_unavailable` and the work blocks on the first
failure without paying for retries.

Constraint: `MemoryTransitionError` codes are a public contract of the work
failure map. Preserve existing codes for genuinely transient provider errors;
only account/auth-class errors change class.

**D2.** Add an ops path to clear account blocks after a topup:
`engram_clear_provider_blocks --organization <id> [--project <id>] [--dry-run]`.
It selects works in `blocked` whose block code is `provider_account_unavailable`,
clears the block (`execution_state=ready`, `blocked_configuration_fingerprint=''`,
`failure_streak=0`) and dispatches, reusing `_clear_block` semantics.

Requires recording *why* a work was blocked. Today only the fingerprint is
stored; the block reason must be persisted to select safely rather than
unblocking every blocked work indiscriminately.

**D3-redrive.** Revive `terminal_failure` `candidate_decision` works whose
candidate is still `PROPOSED` and which have no unresolved `MemoryConflict`.
`_repair_action` currently falls through to `skip` for terminal works by design,
so revival must be an explicit, opt-in ops action — not a silent reconciler
behaviour change. Batched with `--limit` / `--sleep` like
`engram_backfill_distillations` (#288).

Expected cost of the redrive after topup: ~2 991 decisions × ~$0.0057 ≈ **$17**.

### Slice 2 (P1) — independent bug fixes

- **D4** wrap the `create_embedding_work_and_signal` call in `transaction.atomic()`
  per document, keeping per-document failure isolation.
- **D5** reclaim expired `session_distillation` leases.
- **D6** stop the idle `unlinked: 54` churn.

### Slice 3 (P2) — checkpoint the candidate embedding

**D3.** Persist the candidate embedding keyed by `content_hash` so a retry does
not re-pay. After D1, ordinary retries show only 1.15× duplication, so this is
defence-in-depth rather than the fix for the incident — included because the
user asked for maximum scope, and it is the only remaining source of duplicate
provider billing once D1 lands.

### Slice 4 (P2) — cost lever: per-response-kind model override

User directive 2026-07-27: decide autonomously, maximum scope. Decisions below
are recorded with their reasoning; three of four candidate levers are rejected.

Pricing from `policy.metadata`: `deepseek-v4-pro` $0.435/$0.87 per Mtok,
`deepseek-v4-flash` $0.14/$0.28 — flash is exactly 3.1× cheaper on both sides.

**Blocking constraint.** `distill-stage` and `curation-decision` both resolve
`task_type='curation'` (`curation.py:634`) and therefore share **one policy row**.
Switching that row to flash would move the judge as well. The lever needs code,
not a data edit.

**Adopted:** an optional per-response-kind model override in `ModelPolicy.metadata`,
e.g. `{"model_overrides": {"distill_extract.v1": "deepseek-v4-flash"}}`, resolved
at the gateway alongside `resolve_max_tokens`. Chosen over introducing a
`DISTILLATION` task type because it needs no migration, does not touch task-type
semantics or the console surface, and is reverted by deleting one metadata key.

Note: `model_policy` is part of the execution configuration fingerprint
(`work_execution.py:229`), so adding an override changes the fingerprint. This is
correct — blocked works re-evaluate — but it must not be mistaken for a
substitute for the D2 fix.

Expected: `distill-stage` $138 → ~$45 over a comparable 10-day window.

**Rejected levers, with reasons:**

| Lever | Verdict |
|---|---|
| Cap judge reasoning output | **Rejected.** `curation_decision_v1` is already fixed at 16 384 (`_FIXED_MAX_TOKEN_KINDS`), average output 4 991, max observed 12 361. Cutting the cap invites truncation → `judge_invalid_output` → `PROVIDER_TRANSIENT` → paid retries — exactly the loop R1 just eliminated. Saving ~$20/10d is not worth re-creating the incident. |
| Judge → flash | **Rejected.** The judge gates what enters memory permanently. It is 19% of spend; degrading promotion precision to save it is a bad trade. |
| Reduce distill chunk budget | **Rejected.** R1 deliberately made batching output-budgeted with truncation as a first-class signal. Cutting input budget churns that design for a linear saving already obtained more safely via the model override. |

## 5. Testing

TDD per repo convention — the failing test comes first in every case.

- **D1** the decisive test: a judge/embedding call raising `ModelPolicyError`
  with `http_status=402` must leave the work `blocked` with zero retries, and
  must not produce a second `ProviderCallRecord`. This test fails today.
- **D2** a work blocked with `provider_account_unavailable` is cleared and
  dispatched by the command; a work blocked for any other reason is untouched;
  `--dry-run` mutates nothing.
- **D3-redrive** a terminal work with a still-`PROPOSED` candidate is revived;
  one whose candidate was already settled, or which has an unresolved conflict,
  is not.
- **D4** the task completes and creates embedding work for eligible documents;
  a single bad document does not abort the batch.
- **D5** an expired session lease is reclaimed and redispatched.

Backend tests run through the root compose stack from the worktree with a unique
project name, per CLAUDE.md.

## 6. Verification on the stand

Slices are verified against the stand only after the user's topup:

1. `provider_account_unavailable` blocks cleared; works dispatch.
2. Candidate decisions resume: `proposed` count falls, `promoted`/`rejected` grow.
3. `ProviderCallRecord` duplication factor for `curation-decision:*:embedding`
   returns to ~1.0.
4. No new `terminal_failure` from the redriven cohort.
5. `reembed_missing_embeddings` logs a completion, not a traceback.

## 7. Open questions

- **Redrive breadth** — 5 902 candidates are `proposed` but only 2 991 works are
  terminal. The remainder must be triaged during Slice 1 to determine whether
  existing reconciliation already covers them or they need their own repair path.
  Resolve with data before writing the redrive selector; do not guess.
- **Flash extraction quality** — Slice 4 must be A/B'd against the R3 golden
  samples before the override is enabled on the stand, and is trivially reverted
  by deleting the metadata key if extraction degrades.

## 8. Status

Design approved by user directive 2026-07-27 ("сам всё решай, делаем максимум").
Scope is all four slices. Cost levers other than the per-response-kind override
are rejected above with reasons rather than deferred.
