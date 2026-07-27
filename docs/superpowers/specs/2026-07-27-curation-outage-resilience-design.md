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
`lease_expires_at` long past and their latest run still `running` — a worker
died mid-run and nobody reclaimed the lease.

Root cause is an allowlist omission: `work_recovery_reconciler.py:23`
`_RECOVERABLE_WORK_TYPES` contains only `OBSERVATION_PROCESSING`, `DAILY_DIGEST`,
`WEEKLY_DIGEST` and `MEMORY_EMBEDDING`. The `recover-stranded-work` beat task
runs every 15 minutes and reclaims exactly this condition (`lease_expires_at <
now`), but **`SESSION_DISTILLATION` is not in the list**, so it never scans them.

Both stranded works are managed by a v1 `AgentSession`, so
`RetryFailedDistillations` — which by contract handles only non-v1 sessions —
skips them too. `session_work_reconciler` does classify the condition as
`LEASE_EXPIRED` with `proposed_action='reclaim_via_claim_work'`, but that surface
is a *report* consumed by `engram_audit_work_reconciliation`, not an actor.
The result is a work type with three overlapping reconcilers and no owner for
this case.

Fix: add `SESSION_DISTILLATION` to `_RECOVERABLE_WORK_TYPES`.
`CANDIDATE_DECISION` is deliberately left out — `candidate_work_reconciler`
`_repair_action` already dispatches on expired leases for it, and adding a second
actor would create redundant dispatch paths.

Test note: the reembed defect (D4) cannot be reproduced with
`django_db(transaction=True)` — `TransactionTestCase` truncates
migration-seeded tables, which breaks `Role` lookups in this test and pollutes
later tests in the same run (observed: `consistency_tests` failing with
`scanned=0`), and `serialized_rollback` is disallowed in this repo. The test
instead asserts that the service opens its **own** atomic block, by checking that
savepoint depth grows across the call. Verified genuinely red against the
unfixed code (`[0] == [1]`).

### D6 — WITHDRAWN, not a defect

`retry_failed_distillations` returned `{'retried': 0, 'reconciled': 0,
'unlinked': 54}` in all 48 runs over 24 h. This was initially read as idle churn.

Reading `distillation_reconciler.py:170`, `_unlinked_failed_run_ids` is a **pure
read**: it selects `SESSION_DISTILLATION` runs with `work__isnull=True` and
`status=FAILED` and returns their ids. It mutates nothing. The constant 54 is an
observability count of orphaned legacy failed runs, not repeated work.

No fix is warranted. The original claim was inferred from the constant value
without reading the implementation and is withdrawn.

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
candidate is still `PROPOSED`. `_repair_action` falls through to `skip` for
terminal works by design, so revival is an explicit, opt-in ops action — not a
silent reconciler behaviour change.

Implementation reuses the proven R2 machinery rather than duplicating it:
`distillation_backfill.py` is renamed `work_backfill.py` and parameterized by
`work_type` (defaulting to `SESSION_DISTILLATION`, so the R2 command is
unchanged in behaviour), and `BackfillTarget.session_id` becomes `subject_id`.
A sibling command `engram_redrive_candidate_decisions` drives the new type.

Two findings from production data shape this:

- **All 2 991 terminal works have no leftover `QUEUED` run**, so the R2
  "already redispatched" guard will not silently skip them.
- Their **latest** failure codes are historical, predating the D1 fix:
  `embedding_provider_unavailable` (2 756), `unexpected_exception` (232),
  `judge_policy_denied` (3). The account codes introduced by D1 match none of
  them. The command therefore defaults to the *safe* post-fix account codes, and
  the one-time historical recovery passes them explicitly:

  ```
  engram_redrive_candidate_decisions \
    --failure-codes embedding_provider_unavailable,unexpected_exception,judge_policy_denied \
    --limit 25 --sleep 2
  ```

  `unexpected_exception` is deliberately **not** a default: as a blanket default
  it would redrive genuinely broken works, not just outage casualties.

Expected cost of the redrive after topup: ~2 991 decisions × ~$0.0057 ≈ **$17**.

Scope note: 2 905 further `proposed` candidates have no decision work at all and
carry `decision_work_contract_version=0`. `_cp3_repair_candidates` filters to
version 1, so they are deliberately outside this machinery. They are a
pre-existing population, not outage damage, and are out of scope here.

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

| Item | Status |
|---|---|
| D1 classification | Done, PR #295 (`a34bd623`) |
| Redrive command | Done, PR #295 |
| D4 reembed transaction | Done, PR #295 |
| D5 stranded leases | Done, PR #295 |
| D6 idle churn | **Withdrawn — not a defect** |
| Slice 4 model override | Done, follow-up branch |
| Slice 3 embedding checkpoint | Done, follow-up branch (migration 0049) |

Cost levers other than the per-response-kind override are rejected in §4 with
reasons rather than deferred.

Observation, not addressed: `consistency_tests.py::test_authoritative_mismatches
_are_report_only_and_never_mutated[provenance-…]` failed once and then passed in
four consecutive runs (three file-level, one full-suite) with identical code. It
is flaky, not a regression from this work; no fix attempted, recorded here so the
next flake is not mistaken for a new break.

Enabling the override on the stand is a data change, applied after deploy:

```sql
UPDATE model_policy_modelpolicy
SET metadata = jsonb_set(metadata, '{model_overrides}', '{"distill_extract.v1":
  {"model":"deepseek-v4-flash",
   "pricing":{"input_per_mtok":"0.14","output_per_mtok":"0.28"}}}'::jsonb)
WHERE task_type = 'curation' AND active;
```

Reverted by deleting the `model_overrides` key. A/B the extraction quality
against the R3 golden samples before leaving it on.
