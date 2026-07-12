# Checkpoint 8 Temporal Revalidation And Context Enforcement

Date: 2026-07-11
Status: focused implementation specification
Roadmap: Checkpoint 8, MCI-5 followed by MCI-6

Authoritative source sections:

- `2026-07-09-autonomous-memory-loop-roadmap.md`, Checkpoints 4 through 8.
- `2026-07-09-memory-ci-feature-proposal.md`, state machines, revision coverage, revalidation, conflict, retrieval, MCI-5, and MCI-6.
- `docs/reliability/memory-loop-invariants.md` P8, P10, P11, P12, and P15.
- `docs/reliability/memory-loop-fault-matrix.md` F19.

This document freezes the CP8-facing contract. Reconciliation may rename an equivalent CP4–CP7 interface but may not weaken these transactions, states, fences, or retrieval order.

## Goal

MCI-5 turns CP7 shadow impact into evidence-aware autonomous revalidation. Deterministic evidence settles mechanical cases first; ambiguous, sufficiently evidenced cases use one bounded structured judge. Outcomes are fenced to one memory version and canonical revision and use CP4 atomic lineage. Operational failure never becomes semantic truth or human review.

MCI-6 makes that result authoritative for search and context: authorization, then revision coverage/temporal eligibility, then relevance. CP6 bundles bind requested/served revisions, exact memory versions, temporal decisions, policy, rendered bytes, and an optional compact delta.

MCI-5 and MCI-6 are serial PRs; MCI-6 cannot merge before MCI-5’s atomicity, fault, evaluation, and conflict-only gate.

## Dependency gate

Implementation starts from current `master` after these contracts merge:

- CP2 supplies stable work, attempts, leases, retry, reconciliation, and fences; CP8 only adds a work type.
- CP3 supplies complete provenance and durable decision-work coverage.
- CP4 supplies atomic semantic transitions, immutable history, exact projection updates, async embedding intent, sorted locking, and reconstruction.
- CP5 supplies posture-aware policy, scoped structured fallback, genuine-conflict predicate/identity, and conflict-only inbox.
- CP6 supplies fingerprints, immutable rendered/body/version snapshots, mismatch rejection, strict packing, authorized replay, and warnings.
- CP7 MCI-0 through MCI-4 supply ordered canonical revisions, contiguous coverage, trusted evidence, path/symbol anchors, impact plans, stable revalidation work, claim profiles, and fenced shadow assertions.

If a dependency is absent, CP8 stops at RED fixtures instead of recreating it.

## Non-goals and hard boundaries

- No production hosts, SSH, deploy, live migration/repair, D2, or production activation.
- No arbitrary checkout, code/test execution, or caller-supplied URL fetch in API/worker.
- No PR/feature-branch canonical mutation; only accepted default-branch revisions.
- No graph database, whole-program AST, policy language, custom CI runner, branch universe, or corpus copy per revision.
- No broader anchor types before separate post-MCI-4 contracts; v1 uses CP7 paths, symbols, trusted blobs, and fingerprints.
- No similarity/model-confidence/time-decay destructive authority or fail-open provider path.
- No human queue for uncertainty, missing evidence, provider failure, age, suspect state, or retry exhaustion.
- No deletion/rewrite of evidence, revisions, plans, validations, provider calls, transitions, bundles, or conflict links.
- No arbitrary historical reconstruction; MCI-6 only pins to the latest safely represented revision. General time travel is MCI-7B.
- No CP9 rewrite; context/search share eligibility and keep current relevance algorithms.

## Current code to replace or preserve

Current `Memory`/`MemoryVersion`/`RetrievalDocument` have coarse publishability but no repository validity, and version/supersession paths are split. CP8 uses CP4 and never mutates those pieces directly.

CP8 inserts shared temporal filtering after `authorized_retrieval_documents()` scope checks and before scoring, lexical recall, embeddings, or packing.

CP8 extends CP6 fingerprint-compatible immutable `BuildContextBundle` replay; it does not patch the legacy request-ID/current-body replay alone.

MCI-5 adds `TaskType.REVALIDATION` and response kind `memory_revalidation_decision`, reusing provider records, redaction, scope, timeout, fallback, and request identity.

## Three state machines remain separate

The following domains must never be collapsed into one status column:

1. operational work: `ready`, `leased`, `retry_wait`, `complete`, or fenced;
2. knowledge lineage: active, revised, narrowed/broadened, split, merged,
   superseded, restored, refuted, or conflict;
3. temporal validation: `current`, `impacted`, `revalidating`, `unknown`,
   `suspect`, or `transitioned` for a memory-version/revision evaluation.

Provider timeout changes operational work and may conservatively change the
temporal projection to `unknown`; it does not revise, refute, supersede, merge,
restore, split, or create a semantic conflict.

### Temporal validation states

| state | exact meaning | authoritative context default |
|---|---|---|
| `current` | posture-appropriate evidence certifies the version, or complete impact coverage has found no later applicable impact | eligible when applicability permits |
| `impacted` | a committed CP7 plan selected the version for the target revision and durable revalidation work exists | withheld |
| `revalidating` | the stable work is leased and its frozen evaluation window is active | withheld |
| `unknown` | required evidence or operational capability is unavailable, so Engram cannot certify the target state | withheld; retry when the reason is recoverable |
| `suspect` | available evidence is complete but inconclusive or weakly negative; no destructive outcome is justified | withheld unless server policy explicitly allows warned standard-risk use |
| `transitioned` | the evaluation atomically committed a lineage outcome; eligibility follows the resulting current version or conflict | old version withheld |

`superseded`, `refuted`, `conflict`, and `historical` are not temporal worker
states. They remain lineage, publishability, or posture/applicability facts.

### Allowed temporal transitions

Only these edges are valid:

```text
current -> impacted
impacted -> revalidating
revalidating -> current
revalidating -> unknown
revalidating -> suspect
revalidating -> transitioned
unknown -> revalidating
unknown -> impacted
suspect -> impacted
suspect -> revalidating
```

A newly created successor version enters `current` for the target revision in
the same transaction that marks its predecessor `transitioned`. A legacy active
version without CP7 state projects as `unknown/legacy_unvalidated`, never as
implicitly current, once MCI-6 enforcement is enabled.

`unknown -> current` and `impacted -> current` are forbidden shortcuts: work
must enter `revalidating`, reload current evidence, and pass fences. A new
revision may move `unknown` or `suspect` to `impacted` through a new plan.

### Applicability and posture

CP8 consumes CP7’s immutable version profile and extends it only with applicability/risk:

- `claim_posture`: `descriptive`, `prescriptive`, `historical`, or `unknown`;
- `code_sensitivity`: `code_sensitive`, `not_code_sensitive`, or `unknown`;
- `risk_class`: `high` or `standard`;
- `applicability`: normalized project/team/subsystem boundary captured as JSON;
- `profile_version`: `memory_claim_profile/v1`.

Applicability evaluation returns `applicable`, `not_applicable`,
`historical_only`, or `unknown`. Historical claims may be current as history but
are `historical_only` for retrieval: they require an incident/history intent and
are never rendered as current operational instruction.

CP8 does not infer risk from prose during retrieval. A CP7 `unknown` posture/sensitivity or unclassified legacy row defaults to high risk until an explicit trusted classification; unknown high-risk memory is always withheld.

## Canonical reason vocabulary

Stable codes drive branching; redacted explanations are display-only.

| class | allowed reason codes |
|---|---|
| CP7 impact | `path_added`, `path_modified`, `path_renamed`, `path_deleted`, `symbol_added`, `symbol_modified`, `symbol_renamed`, `symbol_deleted`, `semantic_recall_candidate` |
| current | `unchanged_fingerprint`, `rename_equivalent`, `direct_test_or_contract_passed`, `model_confirmed_with_direct_support`, `prescriptive_conformance_drift`, `historical_evidence_preserved`, `transition_successor_current`, `revert_prior_version_revalidated` |
| unknown/retry | `legacy_unvalidated`, `before_evidence_missing`, `after_evidence_missing`, `evidence_untrusted`, `evidence_truncated`, `validator_failed`, `provider_policy_missing`, `provider_unavailable`, `provider_rate_limited`, `provider_timed_out`, `provider_output_malformed`, `transition_lock_retry` |
| suspect | `complete_evidence_inconclusive`, `weak_negative_signal`, `unsupported_model_recommendation`, `destructive_threshold_not_met` |
| fenced/no-op | `superseded_target`, `superseded_revision`, `memory_version_advanced`, `decision_already_applied`, `noncanonical_revision`, `scope_fence_failed` |
| CP4 lineage | `revised_for_current_behavior`, `applicability_narrowed`, `applicability_broadened`, `claim_split`, `equivalent_claims_merged`, `newer_claim_superseded_old`, `prior_version_restored_by_revert`, `claim_refuted_by_direct_evidence`, `genuine_same_state_conflict` |

`semantic_recall_candidate` is recall-only and never transition authority.

## Persistence contract

CP7 owns `engram.memory_ci.models`, migrations 0001–0004, and append-only `ShadowValidationAssertion`. MCI-5 `memory_ci/migrations/0005_active_revalidation.py` adds active models without rewriting shadow history. MCI-6 `0006_stream_policy.py` adds stream fields; a sequential core migration adds CP6 bundle v2 fields.

### `MemoryValidationAssertion`

One row per `(memory_version, target_revision)` has same-scope organization/project/version/revision/plan-item/work FKs; optional source `ShadowValidationAssertion`; 64-character evaluation/evidence SHA-256; state/reason/applicability; decision source; bounded evidence/validator JSON; optional provider/CP4 transition FKs; policy/model IDs and completion time.

Scope/version/target/work identity is immutable. An unknown retry appends an attempt and may replace evidence/evaluation fingerprints only while non-final; any late decision with the old fingerprint is fenced. Final `current`, `suspect`, or `transitioned` data is immutable. Checks enforce scope, hashes, reason/completion, provider-only model source, and transition-only `transitioned`.

### `MemoryTemporalProjection`

One rebuildable row per memory version has same-scope version/latest-assertion FKs; evaluated revision sequence/head; state/reason/applicability; indexed posture/code-sensitive/risk/profile version; optional conflict ID; and `projection_version=1`. It updates atomically with assertion/CP4 transition and rebuilds from immutable sources, so it is not another semantic authority.

Eligibility at R combines the projection with unresolved impact items in `(latest_evaluated_sequence, R]`; complete coverage with no item carries current forward without per-commit corpus writes.

### `MemoryRevalidationDecision`

One semantically immutable decision per project/evaluation fingerprint has same-scope assertion/work/version/revision FKs; optional provider call; schema/outcome/reason/evidence citations; normalized replacement payload; disposition (`pending`, `applied`, `fenced`, `precondition_rejected`); reason and CP4 transition. Only disposition moves once from pending. Replay reuses it; provider failure/malformed output creates none.

`MemoryRevalidationDecisionRelatedVersion` stores decision, same-scope memory version, role (`merge_source`, `supersede_target`, `conflict_peer`, `restore_source`), and ordinal, unique by decision/version/role; related UUID JSON is forbidden.

One serial CP8 core migration, a dependency of 0005, adds transition type `split` and extends `MemoryConflict` with nullable right memory/version, target revision, and applicability hash; exactly one of CP5 candidate or right version is set, and an open memory-pair is unique by scope, sorted versions, revision, and applicability. `MemorySplitResult` links a split transition to two or three same-scope unique successors by ordinal. Typed `SplitMemory` and `OpenMemoryVersionConflict` enter `engram.memory.transitions`; no generic API is introduced.

### Existing CP6 bundle extension in MCI-6

`ContextBundle` schema v2 reuses CP6 rendered hash and adds fingerprint v2, requested/served/optional previous revision FKs, `coverage_lag_policy`, `temporal_context/v1`, coverage result, and warnings. Item schema v2 extends CP6’s exact version/body snapshot with assertion, state/reason, applicability, posture/risk, validated revision, and explanation. Current-memory changes cannot alter replay.

`CanonicalRepositoryStream` gains project-scoped `revalidation_mode`, `temporal_context_mode`, `allow_warned_standard_risk_suspect` (false), and frozen policy versions; existing `disabled|shadow` stream mode remains the ingest switch.

## Stable work and evaluation identity

MCI-5 reuses CP7 work type `memory_revalidation`, subject `memory_version/<UUID>`, occurrence key equal to target revision UUID, snapshot `memory_revalidation_input/v1`, and id-only task `validate_memory_revision_work_v1`.

Its immutable CP7 snapshot contains only impact item, memory version, and target revision IDs; the work fingerprint is unchanged when supplemental evidence arrives.

Each attempt’s evaluation hash adds current CP4 version, prior/target revisions, effective evidence hash, validator/policy versions, and model policy ID, excluding attempt/lease/queue/latency. An old target persists `superseded_target`, completes work as `superseded_revision` only after replacement work exists, and never applies its answer.

## MCI-5 interfaces and ownership

Create focused modules under `apps/backend/engram/memory_ci/`; do not grow
`engram/memory/services.py`, `engram/memory/curation.py`, or
`engram/context/services.py` with the revalidation state machine.

```python
@dataclass(frozen=True)
class RevalidationWindow:
    work_id: UUID
    memory_version_id: UUID
    prior_revision_id: UUID | None
    target_revision_id: UUID
    evaluation_fingerprint: str
    claim_profile: dict[str, object]
    impact_reasons: tuple[str, ...]

@dataclass(frozen=True)
class DeterministicValidationResult:
    disposition: Literal['confirm', 'ambiguous', 'unknown', 'drift']
    reason: str
    evidence_ids: tuple[str, ...]
    validator_results: tuple[dict[str, object], ...]

@dataclass(frozen=True)
class ApplyRevalidationDecisionInput:
    work_id: UUID
    assertion_id: UUID
    decision_id: UUID
    expected_memory_version_id: UUID
    target_revision_id: UUID
    evaluation_fingerprint: str

def freeze_revalidation_window(work_id: UUID) -> RevalidationWindow: ...
def run_deterministic_validators(
    window: RevalidationWindow,
) -> DeterministicValidationResult: ...
def build_evidence_package(
    window: RevalidationWindow,
    deterministic: DeterministicValidationResult,
) -> dict[str, object]: ...
def adjudicate_revalidation(
    window: RevalidationWindow,
    evidence_package: dict[str, object],
) -> UUID: ...
def apply_revalidation_decision(data: ApplyRevalidationDecisionInput) -> UUID: ...
```

File ownership after the schema/interface commit:

- `memory_ci/evidence.py`: frozen window and bounded evidence package;
- `memory_ci/validators.py`: deterministic validators and reason mapping;
- `memory_ci/semantic_impact.py`: optional bounded recall-only expansion;
- `memory_ci/adjudication.py`: prompt/schema/parser/provider adapter;
- `memory_ci/policy.py`: posture precedence, thresholds, and genuine-conflict
  preconditions;
- `memory_ci/transitions.py`: the sole CP8 adapter into CP4 transitions;
- `memory_ci/tasks.py`: id-only task entry and CP2 retry/fence integration;
- `model_policy/models.py`, its next migration, `services.py`, and tests: revalidation task/structured response kind;
- focused test files named after each module.

One schema owner serializes memory-CI/core migrations. One transition owner edits `memory_ci/transitions.py` and only the typed split/version-conflict additions in `memory/transitions.py`; CP5 curation remains untouched.

## Deterministic-before-model execution order

For each stable work identity:

1. CP2 claims the work and creates an attempt. The package payload contains
   only `work_id`.
2. Resolve organization/project/team from the work subject. Reject any scope
   mismatch before evidence reads or provider resolution.
3. Load the target revision and require canonical default-branch identity.
4. Freeze the memory version, claim profile, prior assertion, impact reasons,
   policy versions, trusted anchor fingerprints, and target evidence IDs.
5. Create/reuse the assertion as `revalidating`; compute the evaluation and
   evidence fingerprints.
6. Run every required deterministic validator in a stable name order.
7. If unchanged/equivalent evidence confirms, apply `current` without a model.
   A rename closes the old anchor interval and opens its exact replacement in
   the same validation transaction.
8. If a prescriptive contract drifts, keep the prescription current and record
   `prescriptive_conformance_drift`; code drift cannot refute the rule.
9. If required trusted evidence is missing/unavailable, set `unknown`, record
   the exact reason, schedule CP2 retry, and stop before semantic expansion or a
   model call.
10. Only an ambiguous result with complete required evidence may continue.
11. Optional semantic expansion runs inside resolved scope after exact causal
    reasons exist. It contributes recall candidates only.
12. Build the bounded package and call the resolved revalidation policy through
    the existing provider gateway/fallback.
13. Parse and validate `memory_revalidation_decision/v1`. Malformed output or
    provider failure records operational retry and no semantic decision.
14. Enforce evidence thresholds, posture precedence, outcome shape, and the
    genuine-conflict test outside the model.
15. Persist the immutable decision, then enter the short atomic application
    transaction and recheck every fence.
16. Apply through CP4, update assertion/projection/exact retrieval/audit/work,
    and create async embedding intent atomically.

No provider or embedding call holds database row locks. Embedding failure after
commit degrades semantic recall but not exact retrieval or temporal truth.

## Bounded evidence and semantic recall

`memory_revalidation_evidence/v1` contains only:

- exact claim title/body snapshot and immutable profile;
- original provenance and supporting evidence IDs;
- trusted before/after snippets or fingerprints for selected anchors;
- normalized changed paths/symbols and deterministic results;
- up to four related current memory-version summaries;
- source precedence, truncation, redaction, and attestation metadata.

Hard v1 bounds are 24 evidence items, 64 KiB canonical JSON after redaction,
8 KiB per snippet, four related memories, and three proposed split successors.
Exceeding a bound fails closed as `evidence_truncated` unless deterministic
selection can produce a complete in-bound package.

Semantic impact expansion is capped at 20 memory versions per project/revision,
uses the already authorized project queryset, and stores score plus
`semantic_recall_candidate`. It cannot mark a projection impacted, supply a
required evidence citation, satisfy a destructive threshold, or create a
conflict without exact/corroborated evidence acquired by normal CP7 rules.
Embedding unavailability skips optional expansion and records a metric; it does
not block an already exact, complete evaluation.

## Structured decision contract

The provider emits exactly one object with:

```json
{
  "schema_version": "memory_revalidation_decision/v1",
  "outcome": "confirm",
  "reason_code": "model_confirmed_with_direct_support",
  "evidence_ids": ["after:path:apps/backend/example.py"],
  "related_memory_version_ids": [],
  "replacement": null,
  "split_successors": []
}
```

Allowed outcomes are `confirm`, `revise`, `narrow`, `broaden`, `split`,
`merge`, `supersede`, `restore`, `refute`, `conflict`, and
`insufficient_support`. Unknown keys, invalid UUIDs, uncited evidence, prose
outside limits, disallowed outcome/profile combinations, or missing required
replacement fields make the output malformed.

`insufficient_support` with complete evidence becomes `suspect`; it never
becomes conflict or retry. A recoverable missing capability is classified
before/around the provider call as `unknown` plus retry, not as a model outcome.

## Evidence thresholds and semantic rules

- `direct support` means CP5 `corroborated`: two independent provenance groups or one authoritative CP7 repository validator. The provider cannot assign/upgrade a tier; inference and similarity have zero destructive authority.
- Confirm requires at least one posture-appropriate direct support item or an
  unchanged trusted deterministic fingerprint.
- Revise requires direct current evidence for the same durable subject.
- Narrow requires direct support for the narrower boundary. Broaden requires
  direct support for every newly included boundary.
- Split requires two or three independently supported successor claims; the old
  claim changes only if every successor passes.
- Merge requires equivalent subject, posture, applicability, and supported
  content. A deterministic winner is the earliest-created memory, UUID as tie
  break; CP4 creates the integrated successor version/provenance.
- Supersede requires clear temporal ordering and direct current evidence that a
  newer behavior or decision replaces the older one.
- Restore requires an exact trusted match to a preserved prior version plus the
  new revert revision. Text similarity cannot restore.
- Refute means direct evidence proves the claim was wrong for its claimed
  interval. Mere obsolescence uses revise/supersede, never refute.
- Prescriptive drift keeps the rule current. Only a newer explicit decision of
  equal or stronger authority may supersede/refute a prescription.
- Historical claim text is immutable. Current code may change its retrieval
  applicability and links, not rewrite or refute the recorded history.
- Unknown posture or code sensitivity remains high-risk unknown until trusted classification; a model cannot classify and transition it in one decision.
- Conflict requires two supported, materially exclusive claims for the same
  project, scope, posture-relevant state, revision, environment, dependency
  boundary, and applicability after CP5’s ordered automatic explanations fail.

Evidence below these thresholds yields `suspect/destructive_threshold_not_met`
or `suspect/unsupported_model_recommendation`. It never routes to a person.

## Retry versus semantic outcome matrix

| condition | assertion/work result | lineage mutation |
|---|---|---|
| missing trusted before/after blob or required attestation | `unknown`, CP2 `retry_wait` | none |
| validator exception | `unknown/validator_failed`, retry | none |
| policy/credential/provider unavailable, timeout, or rate limit | `unknown` with exact provider reason, retry | none |
| malformed structured output | `unknown/provider_output_malformed`, retry or approved fallback | none |
| complete but inconclusive evidence | `suspect/complete_evidence_inconclusive`, work complete | none |
| weak negative signal | `suspect/weak_negative_signal`, work complete | none |
| supported unchanged/equivalent evidence | `current`, work complete | none |
| supported semantic outcome | `transitioned`, work complete | one CP4 transition |
| stale memory-version or target-revision fence | old work fenced/no-op; ensure newest work | none |
| concurrent already-applied decision | replay stored transition/result | none duplicated |
| genuine same-state contradiction | `transitioned` to durable conflict | one deduplicated conflict |

Retry exhaustion never changes semantic handling. CP2 may increase backoff and
surface health, but the assertion remains conservatively unknown and the human
conflict inbox remains unchanged.

## Atomic application contract

`apply_revalidation_decision()` performs one short `transaction.atomic()`:

1. Lock the work/assertion and target project coverage row.
2. Lock every affected memory and current memory version in sorted memory UUID
   order using the CP4 lock helper.
3. Require same organization/project/team scope, canonical target revision,
   expected current memory version, matching evaluation fingerprint, complete
   required evidence, unapplied decision, and unsettled work.
4. Re-run the non-provider precondition policy against locked current rows.
5. Invoke exactly one CP4 typed transition for a lineage outcome; confirm or suspect writes no semantic transition.
6. Persist the final assertion, temporal projections, anchor intervals,
   immutable transition relation, exact retrieval representation, audit event,
   decision disposition, and terminal work disposition.
7. Create only the CP4 package-owned id-only embedding/projection intent needed
   after commit.

An exception at any boundary rolls back every item above. The provider record
and work attempt created before application remain evidence but confer no
semantic effect.

Outcome application is exact:

- `confirm`: no new memory version; assertion/projection become current.
- `revise`, `narrow`, or `broaden`: CP4 creates one successor version and moves
  the authoritative pointer; predecessor becomes transitioned.
- `split`: CP4 creates two or three supported current memory/version identities
  and links them to the transitioned source atomically.
- `merge`: CP4 creates/updates the deterministic survivor and transitions all
  losers in one lock-ordered transaction.
- `supersede`: CP4 links the older version to its supported replacement and
  removes the old version from current eligibility.
- `restore`: CP4 makes the preserved prior version applicable through a new
  audited restore transition; future version numbering uses max historical
  version, never `current_version + 1` assumptions.
- `refute`: CP4 records direct refutation and withholds the claim while
  preserving its versions and evidence.
- `conflict`: CP4 creates one sorted-pair/revision/applicability conflict,
  withholds both claims as settled truth, and exposes only that durable item to
  CP5’s human inbox.

## MCI-6 revision resolution and coverage gate

Add these request fields to CP6 `ContextBundleInput` and the context/search
serializers:

```python
repository_revision: str = ''
coverage_lag_policy: Literal['withhold', 'pin_last_processed'] = 'withhold'
previous_repository_revision: str = ''
include_memory_delta: bool = False
exclude_suspect_context: bool = False
retrieval_intent: Literal['current', 'history'] = 'current'
```

Under `enforce`, an empty repository revision makes code-sensitive state unresolved and withheld with `repository_revision_required`; `pin_last_processed` also requires a revision. A supplied value must resolve to that project’s latest accepted head; an older accepted revision fails as `historical_revision_not_supported` except for immutable replay. Arbitrary SHA, branch/project mismatch, or ambiguity fails before retrieval. Client `branch` is metadata, not authority.

The CP7 `CanonicalRepositoryStream` exposes contiguous cursors:

- `latest_accepted_revision`;
- `latest_impact_planned_revision`;
- `latest_revalidation_accounted_revision`.

`latest_impact_planned_revision` advances only when the complete plan, every impacted
projection state, and every required work intent commit atomically. A gap or
out-of-order revision stops the cursor. `latest_revalidation_accounted_revision`
advances only when every required item is either final or durably represented
by conservative `impacted`, `revalidating`, or retryable `unknown` state.

For requested revision R:

- if R is at or before `latest_impact_planned_revision`, item-level temporal state may
  be evaluated for R;
- if R is newer, coverage is lagging and every code-sensitive memory is unknown
  for R regardless of an older current label;
- non-code-sensitive memory still passes normal posture/applicability rules;
- `withhold` returns no code-sensitive item and stores/emits
  `revision_coverage_lag` with requested and covered revision identifiers;
- `pin_last_processed` explicitly serves
  `latest_revalidation_accounted_revision`, stores both requested and served
  revisions, and emits `revision_pinned_to_last_processed`;
- if no represented revision exists, an explicit pin fails with
  `processed_revision_unavailable`; it never silently falls back.

The pin is the only new historical mode in MCI-6. It reconstructs current
memory at the latest represented boundary because no later canonical transition
has applied; it does not offer arbitrary older-revision reconstruction.

## Temporal eligibility before relevance

The shared order for context and search is immutable:

```text
resolve API-key scope and project
-> resolve requested/served canonical revision and coverage policy
-> select coarse publishable rows inside authorized scope
-> evaluate temporal state, applicability, posture, and risk
-> collect compact conflict/coverage warnings
-> exact/lexical/semantic relevance
-> strict deterministic packing
-> immutable bundle/audit snapshot
```

The initial `temporal_context/v1` eligibility table is:

| condition at served revision | result |
|---|---|
| coverage lag + code-sensitive | withheld before ranking |
| current + applicable | eligible authoritative item |
| current prescriptive + conformance drift | eligible with visible drift warning |
| historical posture + history/incident intent | eligible as historical, never current instruction |
| historical posture + current/task intent | withheld |
| impacted or revalidating | withheld with pending reason |
| unknown high risk | always withheld |
| unknown standard risk | withheld |
| suspect high risk | always withheld |
| suspect standard risk | eligible only when server project policy enables warned suspect context and caller did not disable it |
| not applicable or applicability unknown | withheld |
| superseded, refuted, stale, or transitioned predecessor | withheld |
| unresolved conflict | neither claim eligible as truth; compact warning only |

The request cannot elevate policy. It may opt out of warned suspect context but
cannot enable it when project policy is false. Coverage lag overrides every
suspect option.

Conflict warnings contain conflict ID, affected memory IDs, applicability, and
served revision, but no claim is rendered as settled. Warning matching reuses
authorized exact terms/paths/symbols and is capped at five; it does not make a model
call or expose cross-team evidence.

## MCI-6 interfaces and file ownership

```python
@dataclass(frozen=True)
class ResolvedRepositoryState:
    requested_revision_id: UUID | None
    served_revision_id: UUID | None
    coverage_lagged: bool
    coverage_lag_policy: str
    warnings: tuple[dict[str, object], ...]

@dataclass(frozen=True)
class TemporalEligibilityResult:
    eligible_documents: tuple[RetrievalDocument, ...]
    decisions: tuple[dict[str, object], ...]
    warnings: tuple[dict[str, object], ...]

def resolve_repository_state(
    *, project_id: UUID, repository_revision: str, coverage_lag_policy: str,
) -> ResolvedRepositoryState: ...

def filter_temporal_eligibility(
    *, documents: tuple[RetrievalDocument, ...],
    repository_state: ResolvedRepositoryState,
    purpose: str, allow_warned_suspect: bool,
) -> TemporalEligibilityResult: ...
```

Owned files after the MCI-6 interface commit:

- `memory_ci/eligibility.py` and tests: repository resolution and temporal gate;
- `memory_ci/delta.py` and tests: scoped ordered memory delta;
- `engram/context/services.py`, serializers, views, and focused tests: call the
  gate and persist the CP6 extension;
- `engram/search/services.py`, serializers/views, and tests: use the same gate;
- CLI canonical sources under `packages/cli/engram_cli/` plus lifecycle tests;
- generated Claude/Codex plugin copies only through the existing synchronization
  command and their contract tests;
- console inspection serializers/services and frontend timeline/context pages
  after backend response contracts freeze;
- P11/P15 invariant evaluators and F19 tests in dedicated reliability modules.

Context and search owners do not edit `memory_ci/eligibility.py` concurrently.
CLI is the canonical client source; plugin copies are not hand-edited.

## Fingerprint, immutable replay, and response contract

CP8 introduces CP6-compatible fingerprint/snapshot schema v2 (`engram-context-request-v2\n`) while retaining v1 replay readers. V2 adds:

Set `CONTEXT_REQUEST_FINGERPRINT_VERSION=2`, `CONTEXT_SNAPSHOT_SCHEMA_VERSION=2`, and `CONTEXT_RETRIEVAL_POLICY_VERSION='temporal_context/v1'`.

- requested and served repository revision IDs/head identifiers;
- coverage-lag policy and temporal coverage result;
- `temporal_context/v1` and claim-profile/policy versions;
- effective server suspect policy and caller opt-out;
- previous revision and delta inclusion flag;
- exact authorization scope, purpose, kinds, query, paths, symbols, limit,
  token budget, and CP6 retrieval policy fields.

Same request ID plus a changed behavior input returns CP6 idempotency conflict. For an existing bundle, canonicalization uses its stored served revision/coverage outcome rather than re-resolving current coverage; a matching fingerprint reauthorizes scope and returns exact stored bytes, warnings, delta, versions, validations, and revisions.

Under `enforce`, a live context collision with CP6 schema v1 returns 409 `context_snapshot_temporal_upgrade_required` and requires a new request ID; v1 stays immutable and authorized-inspection-readable but is never injected without temporal evidence. CP8 v2 bundles replay byte-for-byte.

The response adds:

```json
{
  "repository_state": {
    "requested_revision": "head-r",
    "served_revision": "head-p",
    "coverage_lagged": true,
    "coverage_lag_policy": "pin_last_processed"
  },
  "memory_delta": [],
  "warnings": []
}
```

Every item includes `memory_version_id`, `validated_revision`, `temporal_state`,
`validity_reason`, `posture`, `applicability`, and `risk_class` from the bundle
snapshot. Token accounting includes rendered warnings and delta. Existing hard
budget behavior remains strict; temporal metadata never causes an over-budget
first item to bypass CP6 packing.

## Memory delta contract

`build_memory_delta(scope, from_revision, to_revision)` reads append-only CP4
transitions and CP8 assertions in `(from, to]`; it never diffs mutable current
rows. Both revisions must be accepted for the same authorized project and
`from.sequence <= to.sequence`.

The compact categories are:

- `newly_applicable`;
- `revised` (including narrow/broaden/split/merge/restore details);
- `superseded_or_refuted`;
- `prescriptive_drift`;
- `conflicts`;
- `pending_high_impact`.

Each item contains category, memory ID, optional from/to version IDs, transition
or assertion ID, target revision, stable reason, and citation metadata. It
contains no unbounded body or source snippet. Scope/team visibility is applied
before rows enter the delta. Ordering is target revision sequence, category
order above, memory UUID, then transition/assertion UUID.

The context endpoint includes delta only when explicitly requested with a previous revision. Console timeline reads the same internal service through inspection; MCI-6 adds no second public delta algorithm or endpoint.

## RED, fault, and revert test matrix

Every row begins as a focused failing test against the merged prerequisite
branch and then becomes GREEN:

| case | required assertion |
|---|---|
| unchanged exact file fingerprint | deterministic current; zero provider calls |
| identical symbol/path rename | anchor interval changes and current confirms atomically |
| descriptive symbol deletion | withheld while revalidating; destructive result only after threshold |
| prescriptive implementation drift | rule remains current with drift warning; no refute |
| historical incident plus current edit | history text/version unchanged; current-task context excludes it |
| historical memory with history intent | eligible only as visibly historical context |
| missing before blob | unknown/retry; no model and no lineage mutation |
| missing attestation | unknown/retry, never confirm |
| validator raises | unknown/retry with recorded reason |
| optional semantic embedding unavailable | exact evaluation continues; expansion skipped |
| semantic-only near match | recall record only; no projection or transition change |
| provider secret missing/outage/timeout/rate limit | retry; no decision/transition/conflict |
| malformed/unknown-key/uncited provider JSON | no decision; retry/fallback only |
| complete inconclusive evidence | suspect; no human item |
| model proposes refute without direct evidence | suspect threshold failure; no mutation |
| provider proposes prescriptive refute from code drift | rejected to suspect; rule stays current |
| valid revise | one new version, transition, exact document, assertion, audit, and work completion |
| split with one unsupported successor | entire split rejected; source remains unchanged |
| concurrent merge decisions | deterministic survivor and one semantic effect |
| two environments | no conflict; applicability separates claims |
| genuine same-state contradiction | one deduplicated durable conflict and only inbox item |
| provider finishes after memory version advances | decision fenced; newest work ensured |
| provider finishes after newer canonical revision | old target fenced; no semantic mutation |
| crash before CP4 application | provider/attempt retained; semantic rows unchanged |
| exception after transition row before commit | all transition/projection/work changes roll back |
| crash after commit before task acknowledgement | replay returns same transition; no duplicate |
| cross-project work/evidence/related-memory ID | denial before evidence/provider/transition |
| empty revision under enforce | code-sensitive memory withheld with `repository_revision_required` |
| revision accepted before plan | P15 withholds all code-sensitive memory for R |
| coverage lag with standard suspect policy on | coverage still withholds code-sensitive memory |
| explicit pin during lag | served revision is last represented and both revisions are labeled |
| pin with no represented revision | `processed_revision_unavailable`; no silent fallback |
| same context ID/fingerprint | byte-identical replay after memory/coverage changes |
| same context ID/different requested or served revision | idempotency conflict |
| unknown high-risk memory | never selected, scored, embedded, or packed |
| standard suspect, server policy off | withheld |
| standard suspect, policy on and caller permits | warned inclusion with immutable snapshot |
| conflict exact match | compact warning; neither claim rendered as truth |
| temporal-ineligible high semantic similarity | never reaches relevance ranking |
| delta A to B | exact ordered categories from append-only assertions/transitions |
| delta cross-team/cross-project | foreign rows absent or request denied |
| strict small token budget with warnings/delta | response remains within CP6 limit |

### Revert sequence fixture

The required fixture has ordered revisions A, B, and C:

1. At A, memory version v1 is current and supported by exact fingerprint F1.
2. B changes the implementation to F2 and atomically revises/supersedes v1.
3. C is a repository revert whose trusted after fingerprint is exactly F1.
4. Deterministic validation identifies the preserved prior version but does not
   copy it or alter state before fences.
5. CP4 commits one `restore` transition for C, revalidates the prior version as
   current/applicable, updates exact eligibility, and preserves B history.
6. Duplicate C delivery, worker retry, and crash-after-commit create no second
   restore.
7. Context for a bundle captured at B remains byte-stable; new context at C
   selects the restored version and delta B→C reports restore.

A revert with matching text but mismatched trusted fingerprint/applicability is
ambiguous and uses the normal evidence-aware path; it cannot auto-restore.

## Invariant and evaluation gates

MCI-5 closes only when:

- deterministic unchanged/rename cases avoid provider calls at the MCI-0
  threshold;
- every provider/validator failure case produces zero semantic transitions;
- destructive outcomes satisfy the fixed evidence thresholds;
- old/new versions, evidence packages, assertions, and transitions reconstruct;
- concurrency/fault tests prove exactly one semantic effect;
- only genuine conflicts enter the CP5 human inbox, keeping P12 healthy;
- shadow decisions meet the MCI-0 impact, false-stale, stale-injection, and
  destructive-decision thresholds measured before enforcement.

MCI-6 closes only when:

- P10 remains healthy with the extended fingerprint/snapshot;
- P11 proves no temporally ineligible item was injected;
- P15 proves the acceptance-to-impact-plan lag interval with withholding and
  explicit pin behavior;
- F19 proves failed revalidation remains withheld until recovery;
- temporal eligibility executes before all relevance paths in context/search;
- high-risk unknown is always withheld and suspect policy is explicit/audited;
- obsolete instructions disappear while CP8 v2 historical bundles replay and CP6 v1 remains inspection-only;
- memory deltas are scoped, deterministic, compact, and revision ordered;
- token budgets and degraded exact retrieval remain CP6-compatible;
- revert, duplicate, out-of-order, rapid revision, cross-project, and newer-head
  fencing scenarios pass end to end.

Queue depth, test count, or an old `current` flag is never a substitute for P11
or P15 evidence.

## CI commands

All Python/backend tests run in the repository’s container or equivalent GitHub
CI image, never directly on the host. From the container working directory
`/srv/app` run:

```text
poetry check
poetry run ruff check .
poetry run ruff format --check .
poetry run python manage.py migrate --noinput --settings=settings.test_settings
poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
poetry run pytest engram/memory_ci -q
poetry run pytest engram/context/services_tests.py engram/context/context_api_tests.py \
  engram/search/services_tests.py engram/search/search_api_tests.py \
  engram/memory/curation_tests.py engram/memory/memory_versioning_tests.py \
  engram/memory/invariant_queries_tests.py -q
poetry run pytest -q
```

Run CLI and generated-plugin contract suites in their containerized CI jobs,
then frontend typecheck/lint/tests for the timeline and bundle surfaces. Record
exact commands, exit codes, focused/full counts, migration freshness, MCI-0
evaluation metrics, P10/P11/P12/P15 results, F19 evidence, and review findings.
Do not claim a gate that could not execute.

## Rollout and rollback contract

Project-scoped CP7 configuration exposes separate modes:

- revalidation: `shadow`, `active`, or `paused`;
- temporal context: `shadow`, `enforce`, or `safe_withhold`.

MCI-5 starts shadow, compares deterministic/model results to the frozen corpus,
then canaries non-destructive confirmations/revisions before destructive
transitions. MCI-6 first records would-withhold decisions, then enables enforce
for canary projects only after MCI-5 passes.

Rollback is ordered and non-destructive:

1. Set revalidation to `paused` so no new semantic transition applies; retain
   work, evidence, assertions, attempts, and provider records.
2. Set temporal context to `safe_withhold`, which withholds code-sensitive
   memory rather than falling back to legacy fail-open retrieval.
3. Leave additive schema and immutable bundles readable/replayable.
4. Repair derived temporal projections from assertions/transitions with the CP4
   scoped dry-run/rebuild primitive.
5. If an applied semantic decision was wrong, correct it through a new audited
   CP4 restore/revision transition after evidence review; never delete history.

Before active canary, additive code/migrations may be reverted normally if no
consumer depends on them. After active transitions exist, code rollback must
retain compatible readers for every stored schema version. Returning an active
project to legacy context that can inject uncertified memory requires explicit
data-risk approval and is not the default rollback.

## Stop conditions

Stop and escalate if implementation requires weakening CP4 atomicity, bypassing
CP2 work/fences, inventing another retrieval path, fetching arbitrary source,
executing repository code, changing the MCI-0 threshold contract, treating a
provider/model/similarity score as destructive authority, putting uncertainty
in the human queue, serving a coverage-lagged code-sensitive claim as current,
mutating a replayed CP6 bundle, deleting history, crossing project/team scope,
editing production/deployment state, or merging MCI-6 before MCI-5 passes.

Completion is the two serial implementation slices, their RED/GREEN and fault
evidence, evaluation results, invariant closure, bounded correctness and
simplicity reviews, and recorded CI. Deployment and production activation are
outside this campaign task.
