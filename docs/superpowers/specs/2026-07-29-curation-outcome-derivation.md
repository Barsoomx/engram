# Curation outcome derivation (contract inversion, slice 1)

Status: design
Owner: main agent
Date: 2026-07-29

## Problem

`_apply_evidence_policy` (curation_judge.py) admits a verdict only when the
model-chosen `outcome` satisfies a set of predicates over facts the model
cannot fully observe. The admissible set is therefore computable **before**
the provider call, yet we ask the model to guess into it and pay for every
guess.

Measured on the dogfood stand:

| signal | value |
| --- | --- |
| `judge_policy_denied` in 6h | 1380 |
| denial site (3h sample, n=345) | 100% the terminal `if not ok` |
| curation provider calls, 3d | 931 recorded, $3.58 |
| curation decisions written, 3d | 289 |
| discarded paid inference | ~69% |
| `revise_memory` / `supersede_memory` / `open_conflict` decisions, all time | 0 / 0 / 0 |
| `candidate_decision` works in `terminal_failure` | 2918 |

Two structural consequences:

1. `judge_policy_denied` is classified `PROVIDER_TRANSIENT`, so every denial
   retries and buys another discarded call. The failure is deterministic —
   the same candidate against the same corpus is denied identically forever.
2. Three of the six outcomes have never once been produced. They require
   `_candidate_precedes` / `_deterministic_precedence`, computed from
   `latest_evidence_at` timestamps that are **not in the prompt at all**. The
   model is asked to assert `temporal_order` it has no way to know.

## Thesis

`outcome` is a pure function of (per-target relation, deterministic facts).
The model's genuine contribution is the *relation* — a semantic judgement
about two claims. Let the model answer only that, and derive the outcome in
code. `judge_policy_denied` then cannot occur by construction.

## Scope of this slice

Slice 1 changes only **how the same response bytes are interpreted**. The v1
payload already carries per-target relations in `comparisons`, so no prompt
or schema change is required to start deriving. Fields the model still emits
but we now ignore (`outcome`, top-level `relation`,
`target_memory_version_id`, `temporal_order`, `reason_code`) are trimmed in
slice 2, which is where the token saving lands.

Explicitly **out of scope**: making MERGE evidence-only (separate slice; see
Risks), reclassifying `judge_policy_denied` to `INVALID_INPUT` (it
terminalizes on first failure — rejected in an earlier review).

## Deterministic facts

Per candidate: `tier ∈ {none, supported, corroborated}`, `refs`,
`latest_evidence_at`, effective `(visibility_scope, team_id)`.

Per shortlist entry: `tier`, `refs`, `latest_evidence_at`,
`has_open_conflict`, `(visibility_scope, team_id)`.

Global: `comparison_complete`.

Derived per entry:
- `same_visibility` — entry pair equals candidate pair
- `candidate_precedes` — both timestamps present and candidate strictly newer
- `deterministic_precedence` — both present and unequal

## Eligibility (mirrors `_apply_evidence_policy` exactly)

With `S = {supported, corroborated}`, entry `e`, model relation `r(e)`,
applicability `a`:

- `merge_evidence(e)`: `r=equivalent ∧ same_vis ∧ ¬conflict(e) ∧ cand∈S ∧ tgt∈S ∧ a=same`
- `revise_memory(e)`: `r=candidate_revises ∧ same_vis ∧ ¬conflict(e) ∧ cand=corroborated ∧ tgt∈S ∧ a=same ∧ candidate_precedes(e)`
- `supersede_memory(e)`: `revise conditions ∧ r=candidate_supersedes ∧ complete`
- `open_conflict(e)`: `r=mutually_incompatible ∧ same_vis ∧ cand∈S ∧ tgt∈S ∧ cand.refs≠∅ ∧ tgt.refs≠∅ ∧ complete ∧ a=same ∧ ¬deterministic_precedence(e)`
- `reject_redundant(e)`: `r=redundant ∧ tgt∈S` (no visibility constraint — a rejection is not a mutation)
- `publish_new`: `cand∈S ∧ complete`
- `reject_unsupported`: `cand=none`

## Derivation ladder

Priority order. **Act on the strongest relation asserted about a target;
never let a weaker relation on another target suppress it — except that an
unresolved contradiction blocks everything.**

1. `open_conflict`
2. `supersede_memory`
3. `revise_memory`
4. `merge_evidence`
5. `reject_candidate` / redundant
6. `publish_new`
7. `reject_candidate` / unsupported

Ties within a rung resolve to the **first entry in shortlist order** — the
shortlist is relevance-ranked and its order is already part of
`manifest_hash`, so this is stable and reproducible.

Rung 1 above the rest: publishing, merging, or revising while the candidate
is known to contradict an existing memory writes into a corpus we already
know to be inconsistent.

Rung 5 above 6: a redundant target means publishing would duplicate. This
was already the prompt's intent; it now happens deterministically instead of
being denied when the model obeyed it and the target's tier disqualified it.

Derived fields:
- `relation` — the selected target's relation; targetless: `unrelated` if
  every comparison is `unrelated`, else `compatible_distinct`; rung 7:
  `unsupported`
- `temporal_order` — `candidate_newer` / `target_newer` from the timestamps,
  `unordered` when `¬deterministic_precedence`, `not_applicable` when targetless
- `reason_code` — fixed map from the derived outcome + relation

## Safety invariant

The derivation must be a **restriction** of the current policy: every
derived verdict passes `_apply_evidence_policy` unchanged. The gate is kept
in place as an assertion, and the property test proves it never fires.

This is the acceptance property, tested exhaustively over the lattice
(relations × candidate tier × target tier × comparison_complete × conflict ×
visibility × precedence), not by sampling:

- derived verdict always satisfies `_apply_evidence_policy`
- never mutate cross-visibility
- never mutate a target under open conflict
- never mutate without both tiers in `S`
- derivation is total: for every input it returns a decision or reports
  infeasible — it never raises

## Feasibility precheck (the money fix)

`feasible_outcomes(facts)` returns the outcomes admissible for *some*
relation assignment. It is empty **iff** `cand∈S ∧ ¬complete ∧` no target is
eligible for any targeted rung — dominated in practice by an incompletely
embedded corpus.

When the set is empty, **do not call the provider**. Raise
`CurationJudgeError('curation_infeasible')`, mapped to
`INFRASTRUCTURE_TRANSIENT` — it retries with backoff, for free, and clears
itself once the corpus finishes embedding. This is the same failure the
embedding cliff produced, made visible before it costs money.

## Verification

1. Unit + exhaustive property tests over the lattice (above).
2. `engram_curator_eval` against the frozen 120-case corpus. Thresholds are
   already committed (`forbidden_destructive_max=0`, `conflict_recall_min=1.0`,
   `destructive_precision_min=1.0`, `macro_f1_min=0.92`). A miss is evidence
   the ladder order is wrong — diagnose, do not tune blindly.
3. Offline census on the stand: for stuck candidates, build shortlist +
   evidence and report `feasible_outcomes` **without any provider call**.
   Proves the 2918 terminal works become actionable before spending on them.
4. Live: redrive after deploy, confirm `judge_policy_denied` goes to zero and
   the decision/call ratio rises from 31%.

## Risks

- **False `equivalent` still overwrites.** Derivation removes the *pressure*
  to misreport an outcome, not the *ability* to misjudge a relation. MERGE
  routes through `_execute_candidate_revision`, which writes the candidate's
  text into the target memory. Making MERGE evidence-only is a separate
  slice and is the real fix for that class.
- **Duplicate publish.** When the model calls a target redundant but that
  target's tier is `none`, rung 5 is ineligible and the ladder falls through
  to `publish_new`. The old gate admitted exactly this state, so the
  restriction invariant holds, but the outcome is a duplicate. Log the
  suppressed identity relations so it is observable.
- **Derivation may act more destructively than the model asked.** The model's
  own `outcome` is ignored; a `candidate_supersedes` in `comparisons` now
  supersedes even if the model wrote `reject_candidate` on top. The frozen
  eval's destructive-precision and forbidden-outcome thresholds are the guard.
