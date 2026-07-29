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

## Measured feasible sets (30 stuck works, stand, zero judge calls)

Brute-forcing `_apply_evidence_policy` over every (outcome, relation, target,
applicability, temporal_order) tuple for 30 stuck `candidate_decision` works:

```
candidate tier      : supported x30      (never corroborated, never none)
comparison_complete : True x30           (the corpus is fully embedded)
shortlist size      : 4 x30
target tiers        : supported x93, corroborated x27

reachable outcomes:
  16/30  merge_evidence | publish_new | reject_candidate/redundant
  14/30  the same plus open_conflict
```

Two conclusions that reshape the problem:

1. **The feasible set is never empty here.** The embedding cliff is not the
   current cause; `comparison_complete` is true everywhere. The infeasibility
   precheck below is cheap insurance against a cliff regression, not the main
   saving. The main saving is the derivation itself.

2. **`revise_memory` and `supersede_memory` are structurally unreachable.**
   They require `candidate_tier == 'corroborated'`, and:

   ```
   candidates by distinct source window: 1 window -> 4243 of 4243
   curation decisions by evidence_tier : supported -> 1333, corroborated -> 0
   ```

   Every distillation candidate is produced from exactly one window, the
   independence union-find collapses that to exactly one group, so the tier is
   always `supported`. Two of the six outcomes are dead code for the only
   candidate producer that exists. The model is asked to choose among six
   outcomes when four are achievable — and pays for every miss.

   Whether single-window multi-observation evidence *should* count as
   corroboration is a separate design question and is **not** changed here.

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

   **The corpus as committed could not see the new decision surface.** Every
   case had 0 or 1 shortlist entries (`{1: 86, 0: 35}`), while production
   shortlists carry 4 (`{4: 30}` across 30 sampled works). Cross-target rung
   ordering — the entire behaviour the ladder introduces — had zero eval
   coverage, so an earlier draft of this spec was wrong to name the eval's
   destructive thresholds as the guard for the last risk below. Multi-target
   cases were added to close it: rung ordering beating the model's own target,
   a contradiction outranking a merge on another target, a cross-visibility
   identity relation never mutating, ties resolving to shortlist order, and an
   unsupported target failing to absorb a duplicate.
3. Offline census on the stand: for stuck candidates, build shortlist +
   evidence and report `feasible_outcomes` **without any provider call**.
   Proves the 2918 terminal works become actionable before spending on them.
4. Live: redrive after deploy, confirm `judge_policy_denied` goes to zero and
   the decision/call ratio rises from 31%.

## Redrive protocol (and why it is batched)

The ~2918 terminal `candidate_decision` works must be redriven in bounded
batches, letting embeddings drain between them. This is not caution for its
own sake — it is required by a second-order effect:

`write_exact_memory_projection` looks the document up by `memory_version_id`
(projections.py:147). Every write outcome — publish, merge, revise, supersede
— creates a **new** version, therefore a new `RetrievalDocument`, therefore
one with `embedding_pgvector` NULL until the embedding work lands. And
`corpus_fully_embedded` (curation_shortlist.py:240) is False if **any**
current document in the authorized corpus is unembedded.

So every successful decision briefly sets `comparison_complete=False` for the
whole project. At low volume this is invisible; under a mass redrive it would
stay false continuously, which starves the `publish_new` rung and makes
`derive_decision` return `None` — **after** the provider call was paid for,
since `feasible_outcomes` cannot know the model's relations in advance. The
`INFRASTRUCTURE_TRANSIENT` backoff throttles the repeat, but batching removes
the cause rather than damping it.

Between batches, check: unembedded current documents back to zero, the
decision outcome mix, and that `judge_policy_denied` stays at zero.

## Risks

- **False `equivalent` still overwrites — and this slice amplifies it.**
  Derivation removes the *pressure* to misreport an outcome, not the *ability*
  to misjudge a relation. MERGE routes through `_execute_candidate_revision`,
  which sets `memory.title` / `memory.body` to the **candidate's** text: it is
  a full content replacement wearing the name "merge". Today it fires 69 times
  all-time because most attempts are denied. After derivation it sits at rung
  4 and is reachable for every `supported` candidate — against a backlog of
  5,824 mostly-duplicate proposed candidates. Shipping derivation alone would
  knowingly amplify a destructive path, so **MERGE must become evidence-only
  before either change reaches the stand**: keep the prior version's title and
  body, attach only the candidate's sources. That is a separate PR, merged
  immediately after this one and deployed together.
- **Duplicate publish.** When the model calls a target redundant but that
  target's tier is `none`, rung 5 is ineligible and the ladder falls through
  to `publish_new`. The old gate admitted exactly this state, so the
  restriction invariant holds, but the outcome is a duplicate. Log the
  suppressed identity relations so it is observable.
- **`applicability` is top-level in v1 but the ladder is per-target.** The
  model reports applicability for *its own* chosen target; the ladder may
  select a different one. The conservative direction (model said `different`,
  ladder blocks every mutation rung) is safe. The unsafe direction — model
  said `same` about target A while the ladder acts on target B — is real but
  narrow, since the ladder picks the strongest identity relation, which is
  usually the target the model chose. Slice 2 moves `applicability` into the
  comparison object and closes it.
- **Revision signal degrades to a duplicate.** Because candidates are always
  `supported`, a `candidate_revises` or `candidate_supersedes` relation can
  never reach its rung; the ladder falls through to `publish_new`, adding a
  competing memory rather than updating the existing one. This is what the old
  gate admitted too, so the restriction invariant holds and nothing is
  overwritten — but the corpus accumulates near-duplicates instead of
  revisions. `suppressed_identity_relations` exists to measure exactly this;
  the fix belongs in the evidence-tier design, not here.
- **`open_conflict` is structurally unreachable, so contradictions publish
  silently.** `_conflict_eligible` requires `¬deterministic_precedence`, i.e.
  the candidate's and target's `latest_evidence_at` are equal or one is
  missing. In production both are always populated and essentially never
  equal, which is why `open_conflict` has never fired. This mirrors the old
  gate exactly — the unreachability is pre-existing, not introduced here — but
  the consequence changes: a model-asserted `mutually_incompatible` used to be
  denied and eventually terminalise, writing nothing; now it falls through to
  `publish_new` and a knowingly contradictory memory enters the corpus with no
  conflict marker. Widening the rung would break the restriction invariant, so
  it is deliberately not done here. `suppressed_identity_relations` will
  measure how often it happens. Tracked as B-008.

- **Derivation may act more destructively than the model asked.** The model's
  own `outcome` is ignored, so an identity relation on a target the model did
  *not* select now drives the outcome. Concretely: shortlist `[A, B]`, model
  says `reject_candidate` against A with `comparisons=[A:redundant,
  B:equivalent]`. The old parser inspected only A and rejected, writing
  nothing; the ladder fires rung 4 on B before rung 5 on A and merges into B.
  With MERGE made evidence-only this is no longer a content overwrite — B
  keeps its text and gains the candidate's sources, a faithful action on the
  model's own assertion that B is equivalent — but it is still an action the
  model did not request, applied at backlog scale. It is now pinned as an
  explicit eval case (C1) rather than left as an emergent accident.

- **`judge_cross_visibility_denied` becomes unreachable.** The ladder never
  selects a cross-visibility target for a mutation rung, so the terminal
  `INVALID_INPUT` stop can no longer fire from the parse path. On redrive,
  works that previously died there now derive `publish_new` and create a
  memory. That is a data-creating change applied to an existing backlog, so
  the redrive is done in bounded batches with the outcome mix checked between
  them.
