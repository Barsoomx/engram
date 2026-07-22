# R3 — Cross-Window Extract-Stage Reuse (same session)

Status: design (approved teamlead direction; this spec expands, does not redesign).
Date: 2026-07-21
Slice: `feat/distill-r3-extract-reuse`
Owner surface: `apps/backend/engram/memory/distillation_window.py`,
`apps/backend/engram/memory/distillation_provider_stage.py`,
`apps/backend/engram/core/models.py` (+ one migration).
Does NOT touch `distillation_reduction.py` (owned by slice R1).

Operator directive (dogfood): no prod deploy exists; no backward-compat / rolling
choreography; deploy is stop-the-world; in-flight distillation work is droppable.
Steady-state correctness (determinism, idempotency, fencing) is NOT waived.
TRAP: `contract_version=0` rows carry live semantics — this slice never touches any
existing contract-version default; new columns are additive only.

---

## 1. Problem and Evidence

Distillation provider spend is ~$103 / 7d (~190M input tokens, prod 2026-07-21).
The dominant waste is **re-extraction of unchanged observation chunks across the
repeated windows of one growing session**, not malformed-reduce retries
(~$2 / 7d; extract stages are already cached *within* a window by the fenced
stage engine).

Mechanism, verified against code:

- An active session is re-windowed repeatedly. Session `1c85464f` produced 5
  `DistillationWindow` rows in one hour (obs 288→328). Each new window is a fresh
  root: `materialize_distillation_window` (distillation_window.py:305) reads the
  full useful-observation prefix `[lower, upper]` (`_read_prefix` :131-144,
  ordered `session_sequence, id`), freezes a window manifest + `input_hash`
  (:319-320), and greedily char-packs it into chunks
  (`_plan_chunks` :192-209, `chunk_char_budget` default 40000).
- Every window then runs **all** its extract chunks through the provider.
  `next_distillation_stage` (distillation_window.py:342-354) returns each chunk
  that lacks a COMPLETE extract stage *in that window*, and
  `execute_distillation_stage` (distillation_provider_stage.py:1223) makes a
  provider call per chunk. ~22 extract calls × 10–15k input tokens, per window;
  ~110 near-duplicate extract calls/hour for the one session. Giant sessions
  (8248 obs) multiply this.
- The rendered extract *prompt* is a **pure deterministic function of the frozen
  chunk manifest** (the *output* is not — see below).
  `_render_stage_prompt` (distillation_provider_stage.py:682-716) builds the
  prompt solely from the chunk manifest's ordered observations — each block is
  `render_observation_block(observation, cap)` (distillation_window.py:147-161),
  keyed by `observation_id` and guarded so the live observation's
  `observation_content_digest` equals the frozen `content_digest`
  (distillation_provider_stage.py:699-708). Neither `window_input_hash` nor chunk
  `ordinal` enters the prompt.

So two chunks in two different windows that cover the **same ordered list of
`(observation_id, content_digest)` at the same `chunk_char_budget`** produce a
byte-identical prompt. (The budget matters because `render_observation_block`
truncates each block to `cap = window.chunk_char_budget`; the reuse key therefore
includes it — see §2.1.) The *output*, however, is **not** deterministic:
`_chat_completion` hardcodes `temperature=0.2` (model_policy/services.py:1719) with
no seed on every extract call (the `extra` dict only ever carries `thinking` /
`response_format` / `max_tokens`; deepseek is the prod extract provider). So today
window N and window N+1 each draw an *independent, equally-valid* temperature-0.2
sample of that identical prompt — their drafts (titles/bodies/confidence)
generally differ. Today we pay for one fresh sample per window. R3 does **not**
"cache an already-identical output"; it **deterministically substitutes** window
N's already-paid sample for window N+1's identical-content chunk. Because any valid
extraction of the same `(observation_id, content_digest)` set is equally valid, the
substitution is sound. It is **not output-neutral** versus a fresh sample: window
N+1's distilled output shifts from a fresh draw to N's draw (see §2.5). The team
accepts this shift — it is arguably beneficial (deterministic replay, less
cross-window memory churn/versioning) and is the mechanism by which reuse saves
spend, not an incidental side effect.

### Why the existing within-window cache does not help across windows

`_attempt_stage` already short-circuits to a completed sibling
(distillation_provider_stage.py:867-879, 924-936) but only for the **same
`window_id` + `target_key`**. `target_key` is built by `stage_target_key`
(distillation_provider_stage.py:199-224) from `window_input_hash`, `ordinal`, and
`chunk_ordinal`; and the persisted `stage.input_hash` equals `chunk.input_hash`,
which `_chunk_manifest` (distillation_window.py:212-218) derives from
`window_input_hash` **and** `ordinal`. Both are window-unique, so nothing matches
across windows.

### Evidence correction to the teamlead brief

The brief names the reuse key as `(chunk input_hash, prompt_contract, policy
id+version, model)`. `chunk.input_hash` **cannot** be the reuse key: it embeds
`window_input_hash` and `ordinal` (distillation_window.py:212-218), so it is
distinct in every window and would yield a 0% cross-window hit rate. This spec
uses a **new content-level reuse key** computed over the chunk manifest's ordered
`(observation_id, content_digest)` pairs plus `prompt_contract` and
`chunk_char_budget` — the exact inputs `_render_stage_prompt` consumes. Correctness is unchanged (the content
digest still guards staleness); only the hit rate is what the brief intended.

---

## 2. Design

Smallest sound design (Karpathy): one derived content hash, one additive lookup,
one copy-instead-of-call short-circuit inside the existing fenced completion path.
No change to stage identity (`target_key`/`stage_key`), so each window still owns
its own stage rows and the deterministic identity machine is preserved.

### 2.1 Content reuse key

New helper in `distillation_provider_stage.py`:

```python
_EXTRACT_REUSE_SCHEMA = 'distill_extract_reuse.v1'

def extract_reuse_key(chunk: DistillationChunk) -> str:
    observations = chunk.input_manifest['observations']
    projection = {
        'schema': _EXTRACT_REUSE_SCHEMA,
        'prompt_contract': EXTRACT_PROMPT_CONTRACT,
        'chunk_char_budget': chunk.window.chunk_char_budget,
        'observations': [
            {'observation_id': entry['observation_id'], 'content_digest': entry['content_digest']}
            for entry in observations
        ],
    }

    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
```

Deliberately excludes `session_sequence`, `window_input_hash`, and `ordinal`
(irrelevant to the rendered prompt) so the key is prefix-stable. Includes
`prompt_contract` in the projection so it is subsumed by `reuse_key`, which hashes
it (the §2.4 lookup has no separate `prompt_contract` filter — the key term carries
that disjointness). **Includes `chunk_char_budget`** because it is NOT prompt-irrelevant:
`_render_stage_prompt` (distillation_provider_stage.py:710-711) renders each
block with `cap = chunk.window.chunk_char_budget`, and
`render_observation_block` truncates via `truncate_with_marker(block, cap)`
(distillation_window.py:161). Two windows at different budgets can produce the
same ordered `(observation_id, content_digest)` list (e.g. a single
oversized-block observation forms its own solo chunk in `_plan_chunks`
regardless of budget) yet render byte-differently (truncated at B1 vs B2).
Excluding the budget would let window N+1 at budget B2 reuse window N's
B1-truncated output — a stale-reuse correctness bug. The budget is per-window,
so within one window the key is fully determined and prefix-stability across
windows at a *fixed* budget is preserved; only a mid-session budget change
(rare, ops-controlled, §7) shifts the key and collapses reuse, which is exactly
the correct behaviour.

Rejected alternative: reuse `chunk.input_hash` — window-unique, 0% hit rate.
Rejected alternative: recompute candidate keys by scanning every prior stage's
manifest at query time — unindexable, O(stages) per chunk on giant sessions.

### 2.2 Persist the key and the provenance link

Three additive changes on `DistillationStage` (core/models.py:1970), all
nullable/blank so existing rows and the REQUIRED→COMPLETE shape are unaffected:

1. `reuse_key = models.CharField(max_length=64, blank=True, default='')`
   — set for EXTRACT stages at creation; empty for REDUCE stages.
2. `reused_from = models.ForeignKey('self', null=True, blank=True,
   on_delete=models.PROTECT, related_name='reuse_children')`
   — set on a stage completed by copy; points to the source stage.
3. Change `accepted_provider_call` from **OneToOneField → ForeignKey**
   (`on_delete=models.PROTECT`, `null=True`, `blank=True`, related_name
   `accepted_distillation_stages`). The migration MUST retain the existing
   `on_delete=models.PROTECT` (core/models.py:2005): provenance correctness
   depends on the shared `ProviderCallRecord` being undeletable while any stage
   (executed or reused) still links it, mirroring the `reused_from` PROTECT
   symmetry (§5). Only the field type, multiplicity, and reverse accessor name
   change; `on_delete`, `null`, and `blank` are unchanged.

Why (3): the status-shape CheckConstraint (core/models.py:2060-2081) requires a
COMPLETE stage to have `accepted_provider_call__isnull=False`. A reused stage does
**not** make a provider call, so it must reference the source stage's
`ProviderCallRecord` to satisfy the invariant and keep provenance pointing at the
real spend. The current OneToOne forbids two stages sharing one call record; once
reuse exists, one provider call legitimately backs many stages, so FK is the
correct multiplicity. The OneToOne reverse accessor `accepted_distillation_stage`
is referenced only in `core/models.py` and migration `0036` (verified: no
application-code reader), so the rename is safe.

`reuse_key` is added to `_IMMUTABLE_FIELDS` (it is derived from the already-frozen
`input_manifest` and never changes after create). `reused_from` is NOT immutable —
it transitions null→source exactly once at completion, alongside the existing
completion fields.

The status-shape CheckConstraint is **kept intact** — both the executed path and
the reused path satisfy `accepted_provider_call__isnull=False`. This is why we
prefer FK-share over a null-call branch: the invariant does not weaken.

Rejected alternative: null `accepted_provider_call` on reuse + widen the
CheckConstraint to a two-branch (executed XOR reused). Weakens the "every complete
stage links its spend record" invariant for no benefit; FK-share keeps it.

New index for the lookup:
`models.Index(fields=['organization', 'project', 'reuse_key', 'status'],
name='core_distill_stage_reuse_idx')`.

### 2.3 Populate the key at stage creation

In `_create_or_reuse_stage` (distillation_provider_stage.py:514-575), when
`target.stage_kind == DistillationStageKind.EXTRACT`, compute
`extract_reuse_key(chunk)` and add it to the `get_or_create` `defaults` only.
REDUCE stages set `reuse_key=''`.

`reuse_key` is **deliberately NOT added** to the `_stage_matches_target` equality
set (distillation_provider_stage.py:485-511). It is a pure function of
`input_manifest['observations']`, the constant `EXTRACT_PROMPT_CONTRACT`, and the
window's `chunk_char_budget` — and `_stage_matches_target` already pins
`input_manifest` (:506), `prompt_contract` (:507), and `window_id` (:499, which
fixes the budget). So for any row created post-deploy the check is strictly
redundant (it can never add discriminating power). It is also actively harmful:
the migration backfills every pre-existing row with `reuse_key=''` (§2.2), so a
session mid-distillation at deploy has persisted REQUIRED extract rows with
`reuse_key=''`; `defaults` do not apply on the `get` branch of `get_or_create`,
so on resume the stored `''` would be compared against a freshly computed
non-empty key → `raise ValueError('existing distillation stage does not match the
requested target')` (:573) on every attempt, a deterministic pre-provider-call
wedge that never drops cleanly. Omitting the match-set addition leaves such a row
with `reuse_key=''`; the reuse block is skipped (gated on truthy `stage.reuse_key`,
§2.4) and it executes normally.

### 2.4 Copy-instead-of-call short-circuit

Inside `_attempt_stage` (distillation_provider_stage.py:851-950), immediately
after the existing same-window completed-sibling check (:867-879) and **before**
`contract.prepare_call`, add a same-session cross-window reuse lookup, gated to
EXTRACT stages with a non-empty `reuse_key`:

```python
if stage.stage_kind == DistillationStageKind.EXTRACT and stage.reuse_key:
    source = (
        DistillationStage.objects.filter(
            window__session_id=window.session_id,
            organization_id=stage.organization_id,
            project_id=stage.project_id,
            team_id=stage.team_id,
            stage_kind=DistillationStageKind.EXTRACT,
            status=DistillationStageStatus.COMPLETE,
            reuse_key=stage.reuse_key,
            policy_id=stage.policy_id,
            policy_version=stage.policy_version,
        )
        .exclude(id=stage.id)
        .order_by('completed_at', 'id')
        .first()
    )
    if source is not None:
        locked = DistillationStage.objects.select_for_update().get(id=stage.id)
        if locked.status == DistillationStageStatus.COMPLETE:
            return _CompletedOutcome(locked)
        chunk = stage.chunk
        _verify_stage_manifest_live(chunk, stage=locked)
        try:
            _refresh_live_policy(stage)
        except (ModelPolicyError, ProviderSecretError) as error:
            return _ProviderErrorOutcome(error, started_calls=0)
        locked.status = DistillationStageStatus.COMPLETE
        locked.reused_from = source
        locked.accepted_provider_call_id = source.accepted_provider_call_id
        locked.response_hash = source.response_hash
        locked.response_size = source.response_size
        locked.output_snapshot = source.output_snapshot
        locked.output_hash = source.output_hash
        locked.completed_at = now
        locked.save()
        logger.info(
            'distill_extract_reused',
            stage_id=str(locked.id),
            reused_from_stage_id=str(source.id),
            session_id=str(window.session_id),
            window_id=str(window.id),
            chunk_ordinal=locked.ordinal,
        )

        return _CompletedOutcome(locked, provider_call_ids=(), started_calls=0)
```

This runs under the already-open `transaction.atomic()` + `lock_work_fence`
(distillation_provider_stage.py:861-865), so it is fenced and atomic with the same
work claim. `attempt_count` is intentionally NOT incremented (no attempt was
made). A structlog module logger is added to the file
(`logger = structlog.get_logger(__name__)`), matching the memory package
convention (e.g. distillation_reconciler.py:27).

**Execute-time live-manifest recheck (`_verify_stage_manifest_live`).** The normal
extract path re-fetches the *live* observations at execution time (inside
`contract.prepare_call` → `_render_stage_prompt`, distillation_provider_stage.py:699-708)
and raises `MemoryWorkerError(code='work_fingerprint_mismatch')` if any live
`observation_content_digest` has drifted away from the frozen manifest between
window freeze and execute. The reuse short-circuit returns **before**
`contract.prepare_call`, so without an explicit guard it would perform no
execute-time content validation at all — the frozen `reuse_key` (computed from the
already-frozen `input_manifest`) only detects drift that happened *before* the
window was frozen, never freeze-to-execute drift. This is reachable, not merely
defensive: `observation.source_metadata` is an input to `observation_content_digest`
(workflow_work.py:208) and is backfilled post-create when previously empty
(imports/services.py:1464-1468, `save(update_fields=['source_metadata'])`), so an
import reconcile racing distillation of a growing session can change the live
digest after window N+1 froze on the old digest but before its extract executes.
Without the recheck the two paths **diverge** on identical inputs: the normal path
terminates in `work_fingerprint_mismatch` while the reuse path terminates COMPLETE —
a determinism violation in the fenced stage machine (NOT waived by the directive).

Factor the digest-verification portion of `_render_stage_prompt` (:683-708 — live
fetch, scope check `work_scope_invalid`, and the per-observation
`work_fingerprint_mismatch` digest comparison, *excluding* the prompt-build/budget
lines :710-714) into `_verify_stage_manifest_live(chunk, *, stage)` and call it
from both `_render_stage_prompt` and the reuse block. The reuse block invokes it
immediately before mutating `locked` (after the idempotent `COMPLETE` early-return),
so freeze-to-execute drift raises `work_fingerprint_mismatch` identically to the
normal path and the two paths converge on the same terminal state for the same
inputs. The recheck is a single indexed observation read under the open fence —
orders of magnitude cheaper than the provider call it still avoids, so the reuse
benefit is preserved. (Only the reuse-*hit* branch needs it; on a reuse miss the
code falls through to the normal path, which rechecks in `prepare_call` anyway.)
`chunk` is bound from the preloaded `stage.chunk` (loaded via
`select_related('window','window__work','chunk')` at
`execute_provider_stage`, :1206); `_attempt_stage` otherwise carries only `window`
(:863), so the reuse block must bind `chunk = stage.chunk` rather than re-fetch it.

**Execute-time live-policy recheck (`_refresh_live_policy`).** The manifest recheck
is not the only execute-time gate the normal path runs that the reuse block would
otherwise skip. After `prepare_call`, the normal path calls `_refresh_live_policy(stage)`
(distillation_provider_stage.py:883) and converts its failures to
`_ProviderErrorOutcome` (:884-885) — a **hard error, not a completion** — when the
live policy has drifted from the stage's frozen `policy_version`: the policy or its
secret was deactivated (`DoesNotExist` → `model_policy_not_found`, :399-406), the
live `policy.version` no longer equals `stage.policy_version` (`model_policy_not_found`,
:408-409), or no active secret envelope remains (`ProviderSecretError`, :415-416).
The reuse `source` query is **content-and-pin only** (`reuse_key` + frozen
`policy_id/policy_version`, §2.1 projection carries no policy) and filters
`policy_version=stage.policy_version` = the *frozen* v1, so a v1 source still matches
even after ops bumped/deactivated the live policy to v2 — the reuse path would
terminate COMPLETE under v1 while the normal path for the identical stage errors and
never completes under v1. That is the same fenced-stage-machine divergence class as
the content-drift twin (determinism NOT waived by the directive), and it also
silently flips window finalization from a fresh v2 extract back to the reused v1
output via the `core_distill_stage_target_complete_uniq` sibling short-circuit
(:867-879). A "no provider call ⇒ liveness moot; v1 output valid for a v1-pinned
stage" acceptance is defensible, but the twin was reconciled by **convergence**, not
acceptance, so R3 converges here too: the reuse-hit branch calls
`_refresh_live_policy(stage)` before mutating `locked` and, on `ModelPolicyError` /
`ProviderSecretError`, returns `_ProviderErrorOutcome(error, started_calls=0)` —
byte-identical to the normal path's :884-885 branch. Both paths therefore reach the
same terminal state on identical inputs: COMPLETE only when the live policy is
present and unbumped, otherwise `_ProviderErrorOutcome` → **STAGE_BLOCKED**. The
drift codes all classify to the `configuration` failure class —
`model_policy_not_found` (deactivation / stale version) maps to
`(CONFIGURATION, 'model_policy_unavailable')` (work_failures.py:40) and
`ProviderSecretError` (no active envelope) maps to
`(CONFIGURATION, 'provider_secret_unavailable')` (:139-140) — and
`_status_for_failure` returns STAGE_BLOCKED for `CONFIGURATION`
(distillation_provider_stage.py:1026-1027), **not** STAGE_RETRY. This matches the
existing normal-path regression `test_policy_revalidation_blocks_call_when_policy_disabled`
(distillation_provider_stage_tests.py:1162), which asserts `status == 'blocked'`
with zero provider calls. The gate is a
single indexed policy read under the open fence, and it still makes **no provider
call**, so the reuse benefit is preserved. Placement mirrors the normal path
(manifest verify first, policy refresh second); `attempt_count` stays un-incremented
on this branch exactly as the normal path leaves it un-incremented when
`_refresh_live_policy` fails before the :888 increment.

Ordering guarantee: `policy_id + policy_version` pin the model and prompt (a policy
version bump is a new `policy_version`), so a matched `source` is guaranteed to
have been produced by an identical model/prompt/content — stale outputs are never
reused; and the added `_refresh_live_policy` gate above pins the *live* policy state
to what the normal path demands, so a mid-flight deactivation / version bump /
secret rotation drives both paths to the same terminal state rather than diverging.
`policy_role` is intentionally not filtered: a same-content source under
the same `(policy_id, policy_version)` yields identical output regardless of role
(in practice fallback uses a *different* policy, so the role of a matched source is
primary anyway).

### 2.5 Data-flow invariance for reduce (no reduce edits)

Reduce reads drafts from the COMPLETE extract stages of its window:
`_evaluate_reduction_state` (distillation_reduction.py:641-660) gathers
`extraction[*]` by a single `window_id` and calls `_snapshot_drafts`
(distillation_reduction.py:417) which reads `stage.output_snapshot`. A reused
stage carries a byte-identical `output_snapshot`/`output_hash` copied from its
source, so the reduce pipeline consumes drafts whose **semantic** fields
(`title`, `body`, `confidence`, `kind`, `source_ids`, `source_output_hash`) are
byte-identical to the reused source. The drafts are **not** wholesale identical:
`_snapshot_drafts` derives `draft_id = stable_draft_id(stage.target_key,
output_hash, index)` and lineage `source_stage_ids=(stage.stage_key,)` /
`source_stage_key=stage.stage_key` from the *current* stage
(distillation_reduction.py:475, :483-485), so a window-B reused stage yields
window-B-rooted `draft_id`/lineage — exactly the per-window identity §2 preserves
by never touching `target_key`/`stage_key`. Reduce therefore
produces results identical **to that source** in every semantic respect while
each window keeps its own draft identity. This is *not* the same as "identical
to a fresh window-N+1 extraction": since extract is temperature-0.2 nondeterministic
(§1), a fresh draw would generally differ, and R3 deliberately replaces that fresh
draw with the reused sample. The "identical results" invariance here is the reuse
forcing the identity (source draft → reduce input), which is exactly what makes the
reduce path require **zero** edits — it is transparent *relative to the reused
source*, not output-neutral versus a re-extraction. R3 therefore requires **zero**
edits in `distillation_reduction.py` (R1's territory).

---

## 3. API / Contract Changes

- No public API, no CLI, no wire-contract change.
- Schema: additive columns `reuse_key`, `reused_from`, plus the field-type change
  `accepted_provider_call` OneToOne→FK and a new index — one migration.
- Stage identity (`target_key`, `stage_key`) unchanged. `input_hash`,
  `input_manifest`, `prompt_contract` unchanged. `contract_version` /
  `chunk_contract_version` untouched.
- New internal helper `extract_reuse_key(chunk)`; new structlog event
  `distill_extract_reused`.

---

## 4. Data Flow

1. `materialize_distillation_window` freezes window N+1 over the grown prefix and
   plans chunks (unchanged).
2. Worker loop: `next_distillation_stage` → `resolve_extraction_stage` →
   `_create_or_reuse_stage` creates the window-N+1 extract stage carrying its
   own `target_key`/`stage_key` **and** `reuse_key = extract_reuse_key(chunk)`.
3. `execute_distillation_stage` → `_attempt_stage`: same-window sibling check
   (miss, new window) → **same-session reuse lookup**. For a prefix chunk whose
   ordered `(observation_id, content_digest)` matches a COMPLETE stage from
   window N under the same policy id+version, `source` is found → copy output,
   set `reused_from` + shared `accepted_provider_call`, mark COMPLETE, log,
   return with `started_calls=0`. No `ProviderCallRecord` is created; no spend.
4. The trailing chunk that absorbed newly appended observations has a different
   `reuse_key` → miss → normal provider call.
5. Reduce for window N+1 reads identical drafts and proceeds unchanged.

---

## 5. Error Handling

- Fencing: the copy executes inside the existing atomic + `lock_work_fence`
  block; a stolen fence raises `StaleWorkFenceError` before commit, identical to
  the executed path.
- Idempotency: on retry, either the same-window completed-sibling check
  (:867-879) or `locked.status == COMPLETE` guard returns `_CompletedOutcome`
  without re-copying. The unique constraint `core_distill_stage_target_complete_uniq`
  (one COMPLETE per `window, target_key`) still holds.
- Missing/mismatched source: if no COMPLETE same-session same-content same-policy
  stage exists, the lookup returns `None` and execution proceeds to a real
  provider call. No new failure modes.
- Content drift (two distinct guards, both retained):
  - *Pre-freeze drift* — content that changed before window N+1 was frozen: the
    manifest `content_digest` differs → `reuse_key` differs → no match → real
    extraction. This is the guard the frozen `reuse_key` provides.
  - *Freeze-to-execute drift* — content that changed after window N+1 froze on
    digest D but before its extract executes (reachable via post-create
    `source_metadata` backfill, imports/services.py:1464-1468): the frozen
    `reuse_key` cannot see this (it is computed from the frozen manifest). The
    reuse block therefore calls `_verify_stage_manifest_live` (§2.4) before
    copying — the same live-digest recheck the normal path performs in
    `_render_stage_prompt` (:699-708) — so a drifted live digest raises
    `work_fingerprint_mismatch` in the reuse path exactly as in the normal path.
    Without this the two paths would diverge on identical inputs; with it they
    converge. Correctness is guarded by the content hash **plus** the shared
    execute-time live recheck.
- Policy/model/prompt change (two distinct cases, both handled):
  - *Frozen-source staleness* — a source produced under a different `policy_id` or
    `policy_version` is excluded from the lookup (the query pins both); a
    `prompt_contract` change alters the reuse key. Stale outputs are never selected.
  - *Live-policy drift* — the source and the reusing stage are both frozen at v1 and
    match, but the *live* policy drifted after freeze (deactivation, version bump to
    v2, or secret/envelope rotation). The frozen `reuse_key`/pin cannot see this; the
    normal path would hard-error in `_refresh_live_policy` (:883-885) and never
    complete under v1. The reuse-hit branch therefore calls `_refresh_live_policy`
    (§2.4) before copying and returns `_ProviderErrorOutcome(error, started_calls=0)`
    on `ModelPolicyError`/`ProviderSecretError`, so both paths converge on the same
    terminal state — **STAGE_BLOCKED / `configuration` failure class** (both drift
    codes classify to CONFIGURATION → STAGE_BLOCKED, work_failures.py:40,:139-140 and
    distillation_provider_stage.py:1026-1027; **not** retry) — instead of the reuse
    path completing a v1 output the normal path refuses. Correctness is guarded by
    the content hash **plus** the shared
    execute-time live-manifest recheck **plus** the shared live-policy recheck.
- Prompt-text / redaction / parser change — the `EXTRACT_PROMPT_CONTRACT` bump
  rule (required invariant): reuse copies the source stage's **already-normalized**
  `output_snapshot` (§2.4) and returns *before* `contract.prepare_call` /
  `_render_stage_prompt` AND before `contract.normalize_output`
  (distillation_provider_stage.py:915, which runs `parse_extraction_output`,
  :756). So the reuse-hit path bypasses **both** ends the normal path exercises
  every time:
  - *(i) rendered-prompt components* — the prompt depends on
    `redact_text`/`redact_value` (distillation_window.py:151-157, driven by the code
    constants `SECRET_STRING_RE`/`REDACTED_VALUE` in core/redaction.py) and on the
    extract system-prompt/schema-instruction *text* (`_EXTRACT_SYSTEM_PROMPT`,
    `curation_schema_prompt_prefix('distill_extract.v1')`). None of these is captured
    by `observation_content_digest` (workflow_work.py:197-216 hashes raw fields, no
    redaction) or by the reuse key, which tracks only the `EXTRACT_PROMPT_CONTRACT`
    literal.
  - *(ii) accepted-output parser/normalizer semantics* — the normal path validates
    every fresh body through `normalize_output` → `parse_extraction_output` (schema
    shape, `_MAX_MEMORIES`/`_MAX_TITLE`/`_MAX_BODY` caps, kind whitelist,
    observation-id membership). A reused stage copies a snapshot that already passed
    the parser **as it was at extraction time**, so a post-deploy parser tightening
    (stricter caps, new required fields, narrower kind set) is silently NOT applied
    to reused output.
  **Invariant:** any change that alters *either* the rendered extract prompt
  (redaction behavior, extract system prompt, or schema instructions) *or* the
  accepted-output parser/normalizer semantics (`parse_extraction_output` shape/caps,
  the `curation_schema_prompt_prefix('distill_extract.v1')` schema instructions, or
  the `distill_extract.v1` normalize contract) MUST bump `EXTRACT_PROMPT_CONTRACT`
  (e.g. `distill_extract.v1` → `.v2`). Because the reuse key includes
  `prompt_contract`, the bump changes every key and collapses reuse for the session,
  forcing re-extraction (and thus re-render **and** re-parse) under the new behavior.
  Today every window re-extracts, so such a change takes effect on the next window
  automatically; with cross-window reuse a mid-session stop-the-world deploy that
  changes prompt/redaction/parser semantics *without* bumping the contract would
  serve pre-change output for the rest of that session (a new staleness class). The
  bump is the guard; it is mandatory precisely because none of prompt text,
  redaction, or parser version is captured by the digest or the reuse key. §6 pins
  this with a fingerprint snapshot test.
- `reused_from` uses `on_delete=PROTECT`, consistent with the window/chunk FKs,
  so a source stage cannot be deleted out from under a reuse child.

---

## 6. Test Plan (TDD order)

All backend tests via docker compose with a unique project name, from the
worktree root:
`docker compose -p engram-r3 run --rm app pytest -q apps/backend/engram/memory/distillation_provider_stage_tests.py apps/backend/engram/memory/distillation_window_tests.py`

Colocated files, pytest functions, typed fixtures, `f_`/`m_` prefixes only when
passed as args, stubs over mocks, single quotes, absolute imports. The existing
`_StubGateway` (distillation_provider_stage_tests.py:215) exposes `.calls`; assert
its length for the zero-call proofs. Reuse existing helpers `_scope`,
`_observation`, `_session_work`, `_policy`, `_claim`, `_install_gateway`,
`_valid_body`.

Write tests red first, in this order.

`apps/backend/engram/memory/distillation_window_tests.py`

1. `test_extract_reuse_key_stable_across_windows_for_identical_prefix_chunk`
   — same ordered `(observation_id, content_digest)` in two windows' chunk-0
   manifests ⇒ equal `extract_reuse_key`; window/ordinal differences do not
   change it.
2. `test_extract_reuse_key_changes_when_observation_content_digest_changes`
   — a differing `content_digest` for one observation ⇒ different key.
3. `test_plan_chunks_full_chunks_are_prefix_stable_under_append`
   (prefix-stability characterization) — `_plan_chunks(entries[:N])` equals
   `_plan_chunks(entries[:N+k])` truncated to all-but-the-last chunk; assert the
   observation-id lists of every non-final chunk are identical, so their reuse
   keys match; assert only the trailing (partial) chunk differs.

`apps/backend/engram/memory/distillation_provider_stage_tests.py`

4. `test_extract_stage_reuses_prior_session_output_without_provider_call`
   (reuse-hit) — run window A chunk-0 extract (gateway.calls == 1). Materialize
   window B with the identical prefix chunk-0; resolve + execute. Assert
   `len(gateway.calls) == 1` (ZERO new calls), stage B status COMPLETE,
   `reused_from_id == stageA.id`, `output_snapshot == stageA.output_snapshot`,
   `output_hash == stageA.output_hash`,
   `accepted_provider_call_id == stageA.accepted_provider_call_id`,
   `attempt_count == 0` on the reused stage.
5. `test_window_reuses_all_unchanged_chunks_with_zero_calls_and_extracts_only_trailing`
   — multi-chunk session; window B appends observations into a new trailing
   chunk. Drive all window-B extract stages. Assert new gateway calls equal only
   the count of changed/trailing chunks (0 for the fully-reused prefix), and
   every prefix stage has `reused_from` set.
6. `test_extract_reuse_disabled_on_policy_version_bump`
   (no-reuse-on-policy-bump) — bump the primary policy version between windows;
   window B chunk-0 makes a real provider call (`len(gateway.calls) == 2`),
   `reused_from is None`.
7. `test_extract_reuse_records_provenance`
   (provenance recorded) — reused stage has `reused_from` set to the source and
   shares `accepted_provider_call`; assert the reused stage passes `full_clean`
   and satisfies the status-shape constraint (COMPLETE with non-null
   `accepted_provider_call`).
8. `test_reused_stage_drafts_semantics_identical_but_identity_is_b_rooted`
   (end-state drafts semantically identical, identity per-window) — for the
   reused chunk, `_snapshot_drafts(stageB)` and `_snapshot_drafts(stageA)` carry
   equal **semantic** fields (`title`, `body`, `confidence`, `kind`,
   `source_ids`, `source_output_hash`, `output_index`) because the copied
   `output_snapshot`/`output_hash` are byte-identical, **but** the B draft's
   `draft_id`, `source_stage_ids`, and `source_stage_key` are B-rooted and
   therefore differ from A's. This pins per-window identity (§2, §2.5):
   `_snapshot_drafts` derives `draft_id = stable_draft_id(stage.target_key,
   output_hash, index)` and lineage `source_stage_ids=(stage.stage_key,)` /
   `source_stage_key=stage.stage_key` from the *current* stage
   (distillation_reduction.py:475, :483-485), which for a reused stage are its
   own window-B `target_key`/`stage_key` — NOT the source's. Assert both:
   semantic equality AND `stageB` draft `draft_id != stageA` draft `draft_id`
   and `source_stage_key == stageB.stage_key`. (import from
   `engram.memory.distillation_reduction`; read-only, no reduce edits).
9. `test_extract_reuse_disabled_on_content_change`
   (correctness guard) — an observation whose content changed between windows
   yields a different `reuse_key` ⇒ window B chunk makes a real call,
   `reused_from is None`.
10. `test_extract_reuse_rejects_freeze_to_execute_drift`
   (divergence guard) — freeze window B on the SAME digest as window A's source
   (so `reuse_key` matches and the source lookup hits), then mutate the live
   observation (e.g. backfill `source_metadata` so `observation_content_digest`
   changes) after freeze but before executing window B's stage. Assert the reuse
   path raises `MemoryWorkerError` with `code='work_fingerprint_mismatch'` — the
   same terminal state the normal path produces — and that stage B is NOT marked
   COMPLETE and has `reused_from is None`. This pins the shared
   `_verify_stage_manifest_live` guard so the reuse and normal paths cannot
   diverge on freeze-to-execute drift.
11. `test_extract_reuse_rejects_live_policy_drift`
   (policy-liveness divergence guard) — freeze window B at the SAME
   `(policy_id, policy_version)` as window A's COMPLETE source (so `reuse_key` and
   the policy pin both match and the source lookup hits), then drift the *live*
   policy after freeze but before executing window B's stage (bump `policy.version`,
   or set `active=False`, or deactivate its secret). Assert the reuse-hit branch
   yields the SAME terminal outcome as the normal path — a **blocked** result
   (`result.status == 'blocked'`, `configuration` failure class; NOT retry —
   `model_policy_not_found`/`ProviderSecretError` map to CONFIGURATION →
   STAGE_BLOCKED, mirroring `test_policy_revalidation_blocks_call_when_policy_disabled`
   at :1162), `len(gateway.calls) == 1` (no new provider call), stage B
   NOT COMPLETE, `reused_from is None`. Distinct from test 6, which drifts the policy
   *before* window B freezes (frozen-source staleness, real call under the new
   version); this pins the shared `_refresh_live_policy` gate on the reuse-hit path.

12. `test_extract_prompt_contract_pins_prompt_and_parser_fingerprint`
   (bump-obligation guard, mirrors the R1 REDUCE-contract fingerprint pattern) —
   because reuse copies an already-normalized snapshot and skips BOTH
   `_render_stage_prompt` and `normalize_output` (§5), a mid-session change to the
   rendered prompt OR the accepted-output parser semantics without bumping
   `EXTRACT_PROMPT_CONTRACT` silently serves pre-change output. Pin a fingerprint:
   `sha256(canonical_json_bytes({...}))` over the load-bearing literals —
   `dps._EXTRACT_SYSTEM_PROMPT`, the schema instructions
   `curation_schema_prompt_prefix('distill_extract.v1')`, and the parser semantics
   (`dps._MAX_MEMORIES`, `dps._MAX_TITLE`, `dps._MAX_BODY`, the sorted allowed-kind
   set, and the `EXTRACT_PROMPT_CONTRACT` literal itself). Assert it equals a
   hard-coded expected hex digest; the test docstring/name states the obligation:
   *if this fails you changed the rendered prompt or the accepted-output
   parser/normalizer — you MUST bump `EXTRACT_PROMPT_CONTRACT`
   (`distill_extract.v1` → `.v2`) and then update this snapshot.* This makes the §5
   invariant mechanically enforced rather than prose-only, exactly as R1 pins its
   `REDUCE_PROMPT_CONTRACT` bump with a prompt-fingerprint snapshot.

Migration/constraint coverage (may live in the existing model/migration test
module): a COMPLETE reused stage (null-call would violate; FK-share must pass)
satisfies `core_distill_stage_status_shape_ck`, and `reused_from` PROTECT blocks
deleting a source stage that has reuse children.

---

## 7. Ops

### Expected savings

Extract calls dominate the ~$103 / 7d spend. For an active-growing session with
`C` chunks re-windowed `W` times, today's cost is `~C·W` extract calls. With R3,
all full (non-trailing) chunks are reused after the first window, so cost falls to
`~C + (W−1)·t` where `t` is the number of chunks the trailing growth touches
(typically 1–2). For session `1c85464f` (≈22 chunks, 5 windows/hour): ~110 extract
calls/hour → ~22 + 4·(1–2) ≈ **26–30 calls/hour, ~70–75% fewer extract calls**.
Giant sessions (8248 obs) benefit proportionally more. Estimate: **60–80%
reduction of extract-stage provider spend** for active-growing/giant sessions;
sessions distilled exactly once see no change (correct — nothing to reuse).

Honest caveats on hit rate (all correctness-neutral):
- The trailing partial chunk of window N is re-packed in window N+1 (absorbs new
  observations) → different content → re-extracted. Expected, counted as a miss.
- Changing `ENGRAM_DISTILL_CHUNK_CHAR_BUDGET` mid-session shifts all chunk
  boundaries AND changes `chunk_char_budget` in the reuse key (§2.1) → total reuse
  collapse until the session ends. Correctness-neutral precisely because the key
  carries the budget: a solo oversized-block chunk that survives a boundary shift
  still gets a fresh key, so window N+1 never serves window N's differently
  truncated output. Ops-controlled, rare.

### How to observe reuse rate on prod

- Structured log `distill_extract_reused` (with `session_id`, `window_id`,
  `chunk_ordinal`, `reused_from_stage_id`) vs. the count of executed extract
  stages; log-derived reuse rate.
- SQL, per session/day:
  `select count(*) filter (where reused_from_id is not null) as reused,
   count(*) filter (where reused_from_id is null) as executed
   from core_distillationstage where stage_kind = 'extract'`.
- Provider spend: `ProviderCallRecord` volume for the extract `task_type` should
  drop; no new records are created for reused stages.
- Reuse count per window: `window.stages.filter(stage_kind='extract',
  reused_from__isnull=False).count()`.

---

## 8. Out of Scope

- Caching/reusing REDUCE stages — their inputs (the window's full draft set)
  change across windows.
- Cross-session dedup — reuse is strictly same-session (`window__session_id`
  filter).
- Any edit to `distillation_reduction.py` — owned by slice R1. R3 only reads
  `_snapshot_drafts` in tests.
- Changing chunk-packing to raise the hit rate (e.g. sealing full chunks) — a
  separate optimization; R3's correctness does not depend on it.

Coordination note (R1):

- **Migrations:** R1 adds **no** migration (R1 §7 — truncation/generation state
  rides existing columns `level` / `last_failure_class`, and R1's rejected
  alternatives explicitly forbid any new table/column). R3's single migration
  therefore descends directly from the current `master` leaf; there is no R1
  migration to rebase on or collide with.
- **Source-file overlap DOES exist and must be sequenced** (not independent):
  both R3 and R1 edit `distillation_provider_stage.py`, and both edit the
  `_attempt_stage` body plus the module-level outcome-dataclass / constant region.
  As implemented on `feat/distill-reduce-rework`, R1 adds
  `PROVIDER_OUTPUT_TRUNCATED` and the `_TruncatedOutcome` dataclass
  (`response_hash`, `response_size`, `response_prefix`, `provider_call_ids=()`,
  `started_calls=1`; distillation_provider_stage.py:192-197), and gives
  `_MalformedOutcome` its final shape `(response_hash, response_size,
  response_prefix='', error_detail='', provider_call_ids=(), started_calls=1)`
  (:182-188) — i.e. **two** new trailing fields (`response_prefix` AND
  `error_detail`), not just `response_prefix`. It inserts a truncation check
  (`is_truncated_finish_reason(result.finish_reason)`) in `_attempt_stage` at :935,
  *before* `normalize_output` (:942-943); adds a `_TruncatedOutcome` branch in
  `_run_stage` (:1230, inside `_run_stage` at :1209); and widens the
  `_attempt_stage` return-type annotation to
  `_CompletedOutcome | _MalformedOutcome | _TruncatedOutcome | _ProviderErrorOutcome`
  (:877). (Line numbers are from the current r1-wt checkout and will shift once
  R1 lands on master; the shapes are load-bearing, the numbers are not.)
  R3 §2.4 inserts the reuse short-circuit into `_attempt_stage`
  (after :867-879, before `contract.prepare_call`) and adds a module logger plus
  `_EXTRACT_REUSE_SCHEMA`. These are same-file, same-function edits: land one slice
  first and rebase the other; do not treat them as mergeable in parallel.
- **Non-overlapping surface:** R3 additionally edits `distillation_window.py` and
  `core/models.py` (+ its migration), which R1 does not touch; R1 owns
  `distillation_reduction.py`, which R3 does not touch.

---

## 9. Review Reconciliation

Adversarial review 2026-07-21 (fresh-context audit against code). All four
findings verified against source; all four FIXED in place.

- **Finding 1 [MAJOR] — reuse key omitted `chunk_char_budget` (stale-reuse bug):
  CONFIRMED, FIXED.** Verified `_render_stage_prompt` renders each block with
  `cap = chunk.window.chunk_char_budget` (distillation_provider_stage.py:710-711)
  and `render_observation_block` truncates via `truncate_with_marker(block, cap)`
  (distillation_window.py:161; `truncate_with_marker` returns a `cap`-length
  string when `len(text) > cap`, candidate_parsing.py:32-39). Verified an
  oversized-block observation forms a solo chunk in `_plan_chunks` at any budget
  (block_chars == budget after truncation forces a split when the chunk is
  non-empty; distillation_window.py:196-205) — so two windows at budgets B1≠B2 can
  yield the same ordered `(observation_id, content_digest)` list yet different
  rendered prompts, and the old key would produce a false reuse hit serving the
  B1-truncated output for a B2 render. `_MIN_CHUNK_CHAR_BUDGET = 8000` makes
  ≥budget blocks plausible. Fix: added `chunk_char_budget` to the §2.1 projection;
  corrected the §1 "byte-identical prompt" and "exact inputs" prose and the §7
  budget caveat. This also makes the pre-existing §7 "budget change → reuse
  collapse" claim actually true. Directive check: this is steady-state
  determinism/correctness, explicitly NOT waived — mandatory fix.

- **Finding 2 [MAJOR] — `reuse_key` in `_stage_matches_target` is redundant and
  poisons pre-existing REQUIRED rows: CONFIRMED, FIXED.** Verified
  `_stage_matches_target` already compares `input_manifest` (:506),
  `prompt_contract` (:507), and `window_id` (:499); `reuse_key` is a pure function
  of exactly those (manifest observations + constant contract + per-window
  budget), so the extra check adds no discriminating power on any post-deploy row.
  Verified the harm: the §2.2 migration backfills existing rows with
  `reuse_key=''`, `get_or_create` `defaults` do not apply on the `get` branch
  (:542-563), and a mismatch raises ValueError at :573 — a deterministic
  pre-provider-call wedge. Under the dogfood directive in-flight work is
  droppable, but a wedge-until-terminalization is strictly worse than the clean
  execution you get by simply not adding the check, and the check is useless
  anyway. Fix: §2.3 now adds `reuse_key` to `defaults` only and documents why it is
  deliberately excluded from `_stage_matches_target`. (`reuse_key` remains in
  `_IMMUTABLE_FIELDS` per §2.2 — that does not poison: the immutable check compares
  the persisted value to itself, '' vs '', on the untouched `get` branch.)

- **Finding 3 [MAJOR] — "No source-file overlap" between R1 and R3 is false:
  CONFIRMED, FIXED.** Verified against the R1 spec
  (`2026-07-21-distill-reduce-rework-design.md` §3.4, lines 343-392): R1 edits
  `distillation_provider_stage.py` — adds `PROVIDER_OUTPUT_TRUNCATED` /
  `_TruncatedOutcome`, a `response_prefix` field on `_MalformedOutcome`, a
  truncation check in `_attempt_stage` (:911-917), a `_TruncatedOutcome` branch in
  `_run_stage` (:1107-1191), and widens the `_attempt_stage` return annotation
  (:857). R3 §2.4 inserts into the same `_attempt_stage` (after :867-879) and adds
  a module logger/constant. Both editing `_attempt_stage` and the dataclass/constant
  region is a real merge hazard. Fix: §8 coordination note rewritten to state the
  overlap explicitly and require sequencing (land one, rebase the other).

- **Finding 4 [MINOR] — coordination note asserts R1 adds a migration; it does not:
  CONFIRMED, FIXED.** Verified R1 §7 line 726: "No migration. Generation and
  truncation state ride existing columns (`level`, `last_failure_class`)", and R1's
  rejected alternatives forbid any new table/column. Fix: §8 now states R1 adds no
  migration and R3's migration descends directly from the current `master` leaf —
  removing the pointless rebase-on-nonexistent-migration instruction.

No finding was refuted; no directive-based dismissal applied (all four are
steady-state correctness or factual-accuracy defects, none waived by the dogfood
directive). No finding weakened the spec — every fix strengthens correctness or
coordination accuracy. `contract_version` / `chunk_contract_version` defaults
untouched (TRAP respected); all schema changes remain additive.

---

### Second adversarial review 2026-07-21 (fresh-context, post-first-reconciliation)

Three findings; all three verified against source and FIXED in place. None
refuted; none weakened the spec; TRAP re-checked (no contract-version default
touched; all schema changes still additive).

- **Finding 1 [MAJOR] — reuse path bypassed the execute-time live-observation
  digest guard; §5's "guarded entirely by the content hash" was a false overclaim:
  CONFIRMED, FIXED.** Verified the normal path re-fetches live observations and
  raises `work_fingerprint_mismatch` on freeze-to-execute drift inside
  `_render_stage_prompt` (distillation_provider_stage.py:699-708), reached via
  `prepare_call` (:745) at :881 — *after* the sibling check. The §2.4 reuse block
  returns before `prepare_call` (:881), so it performed no execute-time content
  validation; the frozen `reuse_key` is computed from the already-frozen
  `input_manifest` (§2.1), so it can only detect *pre-freeze* drift. Verified
  reachability: `observation.source_metadata` feeds `observation_content_digest`
  (workflow_work.py:208) and is backfilled post-create when previously empty
  (imports/services.py:1464-1468, `save(update_fields=['source_metadata'])`), so an
  import reconcile racing a growing session mutates the live digest between window
  freeze and execute. Result: on identical inputs the normal path terminates in
  `work_fingerprint_mismatch` while the reuse path terminated COMPLETE — a
  determinism divergence in the fenced stage machine, NOT waived by the directive.
  Fix: §2.4 now factors the live-digest verification out of `_render_stage_prompt`
  into `_verify_stage_manifest_live(chunk, *, stage)` and calls it in the reuse-hit
  branch before mutating the stage, so both paths raise `work_fingerprint_mismatch`
  identically; §5's "Content drift" bullet rewritten to name both guards (frozen
  `reuse_key` for pre-freeze drift, shared live recheck for freeze-to-execute
  drift) and the false "entirely by the content hash" sentence removed; §6 adds
  `test_extract_reuse_rejects_freeze_to_execute_drift` to pin the convergence.

- **Finding 2 [MINOR] — reuse key omits redaction behavior and extract prompt/schema
  text; relied on an unstated "render-affecting change ⇒ contract bump" invariant:
  CONFIRMED, FIXED.** Verified `render_observation_block` applies
  `redact_text`/`redact_value` (distillation_window.py:151-157), driven by the code
  constants `SECRET_STRING_RE`/`REDACTED_VALUE` (core/redaction.py:7,18), which are
  in neither `observation_content_digest` (workflow_work.py:197-216, raw fields) nor
  the §2.1 reuse key; the extract prompt text is keyed only by the
  `EXTRACT_PROMPT_CONTRACT='distill_extract.v1'` literal (:54), not versioned by its
  own text. Under cross-window reuse a mid-session stop-the-world deploy that changes
  redaction or prompt text without bumping the literal would serve pre-change output
  for the rest of the session — a staleness class absent today (every window
  currently re-extracts). Fix: §5 now states the explicit invariant — any change to
  redaction code or extract system-prompt/schema-instruction text MUST bump
  `EXTRACT_PROMPT_CONTRACT`; because the reuse key carries `prompt_contract`, the
  bump collapses reuse and forces re-extraction. Low likelihood, but documented
  because redaction is in neither the digest nor the key.

- **Finding 3 [MINOR] — §2.2 item 3 left the new FK's `on_delete` unspecified:
  CONFIRMED, FIXED.** Verified the existing field is
  `accepted_provider_call = OneToOneField(..., on_delete=models.PROTECT,
  related_name='accepted_distillation_stage', null=True, blank=True)`
  (core/models.py:2003-2009). A `ForeignKey` requires an explicit `on_delete`, and
  provenance correctness (a shared `ProviderCallRecord` must stay undeletable while a
  reuse child links it, mirroring the `reused_from` PROTECT symmetry, §5) depends on
  retaining `PROTECT`. Fix: §2.2 item 3 now specifies `on_delete=models.PROTECT`,
  `null=True`, `blank=True` explicitly and states only the field type, multiplicity,
  and reverse-accessor name change.

---

### Third adversarial review 2026-07-21 (fresh-context, post-second-reconciliation)

Three findings; all three verified against source and FIXED in place. None
refuted; none weakened the spec; TRAP re-checked (no contract-version default
touched; all schema changes still additive).

- **Finding 1 [MAJOR] — reuse path bypassed the execute-time `_refresh_live_policy`
  liveness gate; normal-vs-reuse divergence on mid-flight policy drift
  (deactivation / version bump / secret rotation): CONFIRMED, FIXED.** Verified the
  normal path calls `_refresh_live_policy(stage)` at distillation_provider_stage.py:883,
  *after* `prepare_call` (:881), and converts its failures to `_ProviderErrorOutcome`
  at :884-885 — a hard error, never a completion — for a deactivated policy/secret
  (`DoesNotExist` → `model_policy_not_found`, :399-406), a stale live version
  (`policy.version != stage.policy_version`, :408-409), or no active envelope
  (`ProviderSecretError`, :415-416). Verified the §2.4 reuse block is placed after
  the sibling check (:867-879) and before `prepare_call` (:881), so it never reached
  `_refresh_live_policy`. Verified the divergence: the reuse `source` query pins only
  `reuse_key` + the *frozen* `policy_id/policy_version` (§2.1 projection carries no
  policy), so a v1 source still matches after ops drifted the live policy to v2 — the
  reuse path would terminate COMPLETE under v1 while the normal path for the identical
  stage returns `_ProviderErrorOutcome` and never completes under v1, and the
  `core_distill_stage_target_complete_uniq` sibling short-circuit (:867-879) then flips
  window finalization from a fresh v2 extract back to the reused v1 output. Same
  fenced-stage-machine divergence class as the second review's content-drift twin
  (determinism NOT waived by the directive). Fix: chose **convergence** (option b),
  consistent with how the twin was reconciled — §2.4 reuse-hit branch now calls
  `_refresh_live_policy(stage)` before mutating `locked` and returns
  `_ProviderErrorOutcome(error, started_calls=0)` on `ModelPolicyError`/`ProviderSecretError`,
  byte-identical to :884-885; both paths now reach the same terminal state (COMPLETE
  only when the live policy is present and unbumped, STAGE_BLOCKED / `configuration`
  otherwise — `model_policy_not_found`/`ProviderSecretError` → CONFIGURATION →
  STAGE_BLOCKED per work_failures.py:40,:139-140 and
  distillation_provider_stage.py:1026-1027, matching the normal-path regression at
  distillation_provider_stage_tests.py:1162; NOT STAGE_RETRY). §2.4 prose
  adds the live-policy-recheck paragraph and extends the ordering guarantee; §5's
  Policy/model/prompt bullet split into frozen-source-staleness vs live-policy-drift,
  the latter naming the shared gate; §6 adds `test_extract_reuse_rejects_live_policy_drift`
  (test 11) to pin the convergence and distinguish it from the frozen-source test 6.
  The gate makes no provider call, so the reuse benefit is preserved.

- **Finding 2 [MINOR] — §2.4 pseudocode referenced an undefined `chunk`: CONFIRMED,
  FIXED.** Verified `_attempt_stage` (distillation_provider_stage.py:851) binds only
  `window = stage.window` (:863) and has no `chunk` in scope, while the reuse block
  called `_verify_stage_manifest_live(chunk, stage=locked)`. Verified the chunk is
  reachable as the preloaded `stage.chunk` via
  `select_related('window','window__work','chunk')` at `execute_provider_stage` (:1206).
  Fix: §2.4 pseudocode now binds `chunk = stage.chunk` in the reuse-hit branch before
  the manifest recheck, and the prose states the binding and why (no re-fetch, no
  mis-scope).

- **Finding 3 [MINOR] — §2.1 prose falsely claimed `prompt_contract` is "also filtered
  in the query": CONFIRMED, FIXED.** Verified the §2.4 lookup (spec lines 213-228) has
  no `prompt_contract` filter; disjointness comes solely from `prompt_contract` being
  hashed into `reuse_key`. Fix: §2.1 reworded to state `prompt_contract` is subsumed by
  `reuse_key` (which hashes it) and that the lookup carries no separate
  `prompt_contract` filter — removing the factually wrong justification without changing
  behavior (the key term already subsumes it).

No finding was refuted; no directive-based dismissal applied (Finding 1 is
steady-state determinism, Findings 2-3 are pseudocode/prose accuracy — none waived by
the dogfood directive). No finding weakened the spec — the convergence fix strengthens
determinism and the two precision fixes remove implementer traps.
`contract_version` / `chunk_contract_version` defaults untouched (TRAP respected); all
schema changes remain additive.

---

### Fourth adversarial review 2026-07-21 (fresh-context, post-third-reconciliation)

One finding; verified against source and FIXED in place. Not refuted; not weakened;
TRAP re-checked (no contract-version default touched; all schema changes still additive).

- **Finding 1 [MAJOR] — the §1/§2.5 "extract is a pure deterministic function /
  identical output we already pay for repeatedly" premise is false against code:
  CONFIRMED, FIXED (reframed + acceptance recorded).** Verified `_chat_completion`
  hardcodes `'temperature': 0.2` (model_policy/services.py:1719) on every chat
  completion including extract (`response_kind == 'distill_extract.v1'`), that this is
  the *only* temperature reference in the file, that no seed is ever set, and that the
  `extra` dict is populated solely by `deepseek_thinking_override` (`thinking` only),
  `openai_json_mode_override` (`response_format` only), and `max_tokens` — none touch
  temperature (services.py:1142-1150, 1168-1172, 1574-1577). deepseek is the prod
  extract provider. So at temperature 0.2 two real extractions of a byte-identical
  prompt do **not** return identical output: today window N and window N+1 each draw an
  *independent* sample, and cross-window reuse does not "cache an identical output" — it
  *substitutes* window N's sample into window N+1's distilled memory, a real, observable
  change to product output (fresh draw → reused draw) that the old prose denied.
  Materiality: this does **not** break the feature — any valid extraction of the same
  `(observation_id, content_digest)` set is equally valid, so deterministic substitution
  is sound and arguably beneficial (replayability, less cross-window churn) — but it
  falsified the foundational premise and a behavioral-transparency claim, so it required
  correction, not dismissal. Fix: §1 line 43 now scopes determinism to the rendered
  *prompt* (not the output); §1's "identical output we pay for repeatedly" paragraph
  rewritten to state extract is temperature-0.2 nondeterministic, that each window draws
  an independent equally-valid sample today, and that R3 **deterministically substitutes**
  the first window's sample for later identical-content chunks — with explicit **team
  acceptance** that window N+1's distilled output shifts to N's sample; §2.5 rewritten to
  distinguish "identical to the reused source" (true, and what makes reduce edit-free)
  from "identical to a fresh re-extraction" (false). No test change: §6 tests 4/5/8
  assert byte-identity only because `_StubGateway`/fake provider are deterministic — they
  validate the *copy*, not provider-level determinism, and the spec does not cite them as
  determinism evidence. Directive check: steady-state determinism framing corrected;
  in-flight droppability irrelevant to a documentation-accuracy defect; no schema/default
  touched.

No finding was refuted; no directive-based dismissal applied. The reframe does not
weaken the spec — it removes a code-falsified premise and records the previously implicit
behavior-change acceptance, strengthening honesty about product-output impact. The reuse
correctness argument (equally-valid samples ⇒ sound substitution; content-digest, live
manifest, and live-policy convergence guards intact) is unchanged.
`contract_version` / `chunk_contract_version` defaults untouched (TRAP respected); all
schema changes remain additive.

---

### Cross-model cross-check 2026-07-21 (round: codex-xcheck)

Four findings from a fresh-context cross-model reviewer; all four verified against
source and FIXED in place. None refuted; none weakened the spec; TRAP re-checked
(no contract-version default touched; all schema changes still additive).

- **Finding (a) [MAJOR] — the draft-equality test (§6 test 8) contradicted
  per-window identity: CONFIRMED, FIXED.** Verified `_snapshot_drafts` derives
  `draft_id = stable_draft_id(stage.target_key, output_hash, index)` and lineage
  `source_stage_ids=(stage.stage_key,)` / `source_stage_key=stage.stage_key` from the
  *current* stage (distillation_reduction.py:475, :483-485). A reused window-B stage
  keeps its own B-rooted `target_key`/`stage_key` (§2 leaves stage identity
  untouched), so its drafts MUST carry B-rooted `draft_id`/lineage — the old test's
  `_snapshot_drafts(stageB) == _snapshot_drafts(stageA)` full-equality assertion was
  false and would fail against a correct implementation. Only the semantic fields
  (`title`, `body`, `confidence`, `kind`, `source_ids`, `source_output_hash`,
  `output_index`) are identical, because the copied `output_snapshot`/`output_hash`
  are byte-identical. Fix: §6 test 8 renamed
  `test_reused_stage_drafts_semantics_identical_but_identity_is_b_rooted` and now
  asserts semantic equality AND B-rooted `draft_id`/`source_stage_key` divergence;
  §2.5 prose corrected from "consumes drafts byte-identical to the reused source" to
  "byte-identical draft *semantics* with window-B-rooted `draft_id`/lineage", keeping
  the reduce-is-edit-free conclusion intact.

- **Finding (b) [MAJOR] — the stale-reuse bump rule omitted parser/normalizer
  semantics: CONFIRMED, FIXED.** Verified the reuse block copies the source's
  already-normalized `output_snapshot` and returns before `contract.normalize_output`
  (distillation_provider_stage.py:915 → `parse_extraction_output` :756), so a
  post-deploy parser tightening (caps `_MAX_MEMORIES`/`_MAX_TITLE`/`_MAX_BODY`, kind
  whitelist, schema shape) is silently bypassed on the reuse path exactly as prompt
  text is. The §5 bump rule covered only rendered-prompt components. Fix: §5 rule
  rewritten to mechanically cover BOTH (i) rendered-prompt components and (ii)
  accepted-output parser/normalizer semantics — any change to either MUST bump
  `EXTRACT_PROMPT_CONTRACT`; §6 adds test 12
  `test_extract_prompt_contract_pins_prompt_and_parser_fingerprint`, a sha256
  snapshot over `_EXTRACT_SYSTEM_PROMPT` + `curation_schema_prompt_prefix(
  'distill_extract.v1')` + the parser-cap literals + the contract literal, mirroring
  the R1 `REDUCE_PROMPT_CONTRACT` fingerprint pattern, with a name/docstring stating
  the bump obligation.

- **Finding (c) [MAJOR] — live-policy drift on the reuse path was documented as
  STAGE_RETRY, but the code blocks: CONFIRMED, FIXED.** Verified
  `model_policy_not_found` → `(CONFIGURATION, 'model_policy_unavailable')`
  (work_failures.py:40) and `ProviderSecretError` →
  `(CONFIGURATION, 'provider_secret_unavailable')` (:139-140), and
  `_status_for_failure` returns STAGE_BLOCKED for CONFIGURATION
  (distillation_provider_stage.py:1026-1027) — the existing normal-path regression
  `test_policy_revalidation_blocks_call_when_policy_disabled` asserts
  `status == 'blocked'` (distillation_provider_stage_tests.py:1162). Fix: §2.4, §5's
  live-policy-drift bullet, §6 test 11, and the third-review reconciliation note all
  now state the convergent terminal state is **STAGE_BLOCKED / `configuration`** (not
  retry), with the classification chain and the :1162 regression cited. Normal-vs-reuse
  convergence is preserved — only the terminal-state label was wrong.

- **Finding (d) [MINOR] — the R1-sequencing note pinned an outdated `_MalformedOutcome`
  shape: CONFIRMED, FIXED.** Read the current implemented dataclass on
  `feat/distill-reduce-rework`: `_MalformedOutcome(response_hash, response_size,
  response_prefix='', error_detail='', provider_call_ids=(), started_calls=1)`
  (distillation_provider_stage.py:182-188) — R1 adds **two** trailing fields
  (`response_prefix` AND `error_detail`), not just `response_prefix`; `_TruncatedOutcome`
  is `(response_hash, response_size, response_prefix, provider_call_ids=(),
  started_calls=1)` (:192-197); truncation check at :935 before `normalize_output`
  (:942); `_TruncatedOutcome` branch in `_run_stage` at :1230; return annotation at
  :877. Fix: §8 note updated to the final signatures and current r1-wt line numbers,
  flagged as shift-on-merge.

No finding was refuted; no directive-based dismissal applied (a/c are steady-state
correctness/determinism, b is a real cross-window staleness class, d is
factual-accuracy — none waived by the dogfood directive). No finding weakened the
spec — every fix strengthens correctness (a/b/c) or coordination accuracy (d).
`contract_version` / `chunk_contract_version` defaults untouched (TRAP respected); all
schema changes remain additive.

---

### Round 5 (post-implementation code review)

- **Round-2 finding — §6 test 12
  (`test_extract_prompt_contract_pins_prompt_and_parser_fingerprint`) was mandated
  by this spec (lines 615-631) but never implemented in
  `distillation_provider_stage_tests.py`: CONFIRMED, FIXED.** Test-implementation
  pass added the test with a projection over `dps._EXTRACT_SYSTEM_PROMPT`,
  `curation_schema_prompt_prefix('distill_extract.v1')`, the parser caps
  (`dps._MAX_MEMORIES`/`_MAX_TITLE`/`_MAX_BODY`), the sorted allowed-kind set
  (`MEMORY_KINDS` minus `'digest'`), and `dps.EXTRACT_PROMPT_CONTRACT`, hashed with
  `hashlib.sha256(canonical_json_bytes(...))` and pinned to a hard-coded hex digest,
  mirroring the R1 `REDUCE_PROMPT_CONTRACT` fingerprint precedent
  (`model_policy/services_tests.py::test_reduce_prompt_components_pinned_change_forces_contract_version_bump`).
  Same pass also strengthened `test_extract_reuse_rejects_live_policy_drift` to
  assert `result.failure.failure_class == CONFIGURATION` and
  `result.failure.code == 'model_policy_unavailable'`, closing round-3 Finding (c)'s
  residual gap (status was asserted but not the failure classification).

- **Round-3 finding (Codex) — the test-12 fingerprint enumerated only literals and
  stayed blind to renderer/parser BEHAVIOR: CONFIRMED, FIXED.** A change to
  `render_observation_block`'s field labels or order, to the redaction it applies
  (`redact_text`/`redact_value` calling `core.redaction.redact_value`, driven by
  `SECRET_STRING_RE`/`REDACTED_VALUE`, core/redaction.py:7,18,36), or to
  `truncate_with_marker`'s marker text (candidate_parsing.py:32-39) would not move
  the digest, yet the reuse path (§2.4) skips `_render_stage_prompt` entirely and
  serves the source's already-rendered/parsed snapshot — exactly the behavior a
  mid-session change to any of those would silently leave stale. Symmetrically,
  `parse_extraction_output`'s structural rules (required/unknown-key validation,
  confidence bounds, the silent drop of a `supporting_observation_ids` entry not in
  the chunk set at `_parse_supporting_ids`, distillation_provider_stage.py:306-326,
  and the `no_signal_observation_ids` complement recomputed from
  `chunk_observation_ids - supporting_union` rather than the input array,
  :390-398) were also unpinned. Fix (mine, binding): extended the SAME
  `test_extract_prompt_contract_pins_prompt_and_parser_fingerprint` with three
  golden samples added to the hashed projection instead of listing more literals:
  (1) `render_observation_block` at a generous cap (10000) over a fixed, unsaved
  `Observation` whose body embeds a secret-shaped token (`sk-...`) matched by
  `SECRET_STRING_RE`, pinning field labels/order and redaction end-to-end; (2) the
  same observation at a tiny cap (80), pinning the `truncate_with_marker` marker
  text; (3) a plain-dict projection of `parse_extraction_output` over a fixed raw
  body and a fixed 3-id chunk set, constructed to exercise two memories (one with
  `kind`, one without), one supporting id outside the chunk set (pins the silent-drop
  rule), and a non-empty no-signal complement (pins the recomputation, not a
  pass-through of the input array). Re-snapshotted the hard-coded digest after
  verifying each sample's actual output by hand (redaction fired, truncation marker
  present, drop and complement behaved as designed) before hard-coding.
  Structural source-hashing of `parse_extraction_output`/`render_observation_block`
  (e.g. hashing the function's AST or source text) was **considered and REJECTED**
  as brittle: a behavior-preserving refactor (rename a local, reorder an unrelated
  helper, restructure control flow with identical outputs) would trip the tripwire
  for no reason, creating snapshot churn with no correctness signal. The
  golden-sample approach only trips when *observable* behavior changes, which is
  exactly the invariant §5 needs mechanically enforced. Net effect: the §5 bump
  obligation is now mechanically enforced for BOTH rendered-prompt components
  (labels, order, redaction, truncation) and accepted-output parser semantics
  (validation rules, coverage-drop, no-signal computation) — closing the gap the
  round-3 finding identified, with no product code touched (test-only change).

No finding was refuted; no directive-based dismissal applied (both are test-coverage
completeness/correctness gaps in the fingerprint's mechanical enforcement, not
waived by the dogfood directive). No finding weakened the spec — both fixes
strengthen the §5 invariant's enforcement. `contract_version` /
`chunk_contract_version` defaults untouched (TRAP respected); all schema changes
remain additive; no product code was modified by either fix.

---

### Round 6 (final verification)

- **Round-4 finding (a) — the golden rendered-block sample embedded a secret in
  `body` only, leaving every other field's redaction unpinned: CONFIRMED, FIXED.**
  `render_observation_block` calls `redact_text`/`redact_value` independently per
  field — `title`, `body`, `facts`, `narrative`, `concepts`, `files_read`,
  `files_modified` (distillation_window.py:151-157) — so a secret only in `body`
  proved nothing about the other six call sites; each is a distinct opportunity to
  drop or mis-wire the redaction call in a future edit. Related and more severe:
  `observation_content_digest` hashes the RAW (pre-redaction) observation fields
  (workflow_work.py:196-216) and the reuse key is a projection of `observation_id` +
  that raw content digest (§2.1) — redaction never touches either, so a change to
  `SECRET_STRING_RE`/`REDACTED_VALUE`/the per-field redaction wiring cannot ever
  invalidate a `reuse_key` or force re-extraction. The test-12 fingerprint is
  therefore the ONLY tripwire in the system for redaction regressions on the reuse
  path — it cannot lean on the reuse key as a second line of defense the way
  content changes can. Fix: `_fingerprint_sample_observation` now embeds a distinct
  `sk-`-shaped token (matching `SECRET_STRING_RE`) into `title`, `body`, both
  elements of `facts`, `narrative`, both elements of `concepts`, both elements of
  `files_read`, and both elements of `files_modified` — eight independent
  redaction sites. Verified by hand (rendered the sample outside pytest) that all
  eight tokens come back `[REDACTED]` and zero `sk-` substrings survive.

- **Round-4 finding (b) — the golden output-snapshot sample was a hand-rolled
  projection, not the production serializer, and silently diverged from it:
  CONFIRMED, FIXED.** The reuse path copies `source.output_snapshot`
  (distillation_provider_stage.py:946), which is produced by
  `_normalize_output(output)` (:770-783, called from `normalize_output` at :811) —
  and `_normalize_output` omits the `kind` key entirely when a memory has no kind
  (`if memory.kind: entry['kind'] = memory.kind`, :779-780). The prior hand-rolled
  helper always included a `'kind'` key (even `''` for kind-less memories), so a
  future change to that omission rule (e.g. always emitting `kind: ''`) would not
  have moved the fingerprint even though it changes exactly the bytes reuse copies
  verbatim. Fix: `_fingerprint_parsed_output_sample` renamed
  `_fingerprint_output_snapshot_sample` and now returns
  `dps._normalize_output(parsed)` directly — the real production function — instead
  of a parallel hand-built dict. Verified by hand that the real serializer omits
  `kind` for the three kind-less memories and includes it only for the one with
  `kind='decision'`.

- **Round-4 finding (c) — confidence boundary acceptance (0 and 1 inclusive) was
  unpinned: CONFIRMED, FIXED.** `_parse_confidence` rejects only `< 0` or `> 1`
  (distillation_provider_stage.py:289-290), so `0` and `1` are valid boundary
  values a provider could legitimately emit, but no golden sample exercised either.
  Fix: the golden raw body gained two more memories — Memory C at `confidence=0`
  (supported by the previously-unsupported third chunk id) and Memory D at
  `confidence=1` (reusing chunk id A's id, which is permitted: the duplicate-free
  rule in `_parse_supporting_ids` is scoped to one memory's own list, not across
  memories) — bringing the golden body to 4 memories, still well under
  `_MAX_MEMORIES` (12). Verified by hand that both parse to `Decimal('0')` and
  `Decimal('1')` without error. Side effect accepted as correct, not a regression:
  because Memory C now supports the third chunk id, `no_signal_observation_ids`
  recomputes to empty rather than the previously-pinned single id — the golden
  simply pins whatever the real complement computation yields, which is the whole
  point of using the production functions instead of asserting a preconceived
  shape.

Re-snapshotted `_EXTRACT_PROMPT_CONTRACT_FINGERPRINT` after all three fixes
(placeholder → run → hard-code actual digest, per the established procedure). No
finding was refuted; no directive-based dismissal applied — all three are
mechanical strengthenings of test coverage, not product-code changes, not waived by
the dogfood directive. `contract_version` / `chunk_contract_version` defaults
untouched (TRAP respected); all schema changes remain additive; no product code was
touched by this pass.

**Scope settlement.** The fingerprint's coverage is now: the system prompt: the
schema instructions; the parser limits (`_MAX_MEMORIES`/`_MAX_TITLE`/`_MAX_BODY`);
the sorted allowed-kind set; the `EXTRACT_PROMPT_CONTRACT` literal; and golden
end-to-end samples through the REAL renderer (all seven fields secret-bearing,
both a plain and a truncated render) and the REAL normalizer (kind-present,
kind-absent, boundary confidences 0 and 1, the out-of-chunk-set silent-drop rule,
and the no-signal complement recomputation). Residual semantic axes that cannot be
expressed as one of these samples — e.g. a wholesale rewrite of the parser's
control flow that happens to reproduce every golden sample's output byte-for-byte —
remain governed by the process-level `EXTRACT_PROMPT_CONTRACT` bump obligation
(§5), which is a human/review discipline, not a mechanical one, and that is an
accepted, intentional boundary. Structural source/AST-hashing of the renderer or
parser to close that residual gap was considered in round 5 and is
**SETTLED-REJECTED**: it would trip on behavior-preserving refactors with no
correctness signal. This scope is final — further fingerprint enumeration is
SETTLED-REJECTED and future reviews must not re-litigate it.
