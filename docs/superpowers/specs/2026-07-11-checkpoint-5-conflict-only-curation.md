# Checkpoint 5 - Conflict-Only Autonomous Curation

Date: 2026-07-11 Status: implementation-ready design Repository baseline: `master` at `79ddb15a` Roadmap authority: `docs/superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md`, Checkpoint 5 and invariants P6, P8, P9, and P12. Required dependency: Checkpoint 4 atomic memory transitions and rebuildable projections are merged and green before C5.1 implementation begins.

## Executive Decision

Checkpoint 5 replaces the current inline, confidence-gated curator with one durable decision workflow per candidate. Every healthy non-conflict candidate settles automatically. The only semantic state that waits for a person is an open conflict between materially supported, mutually incompatible claims in the same effective scope and applicability. Provider outage, missing policy, missing embedding, invalid model output, stale comparison state, and transaction contention remain operational work. They never become rejection, publication, merge, supersession, conflict, or a human inbox item. Exact identity may drive deterministic deduplication. Vector, lexical, and trigram similarity may only select comparison targets. No similarity score, threshold crossing, candidate age, or model confidence may directly authorize a semantic transition. Memory-changing/conflict outcomes use typed CP4 transitions; ordinary rejection is the narrow fenced CP5 disposition exception because no memory changes. The current `CurateMemoryCandidate` wrapper is removed during serial cutover.

## Why This Shape

Three implementation shapes were considered.

1. Patch the current inline curator in place. This preserves direct calls from observation and distillation writers, split transition boundaries, and implicit fail-open defaults.
2. Add a candidate-decision workflow with narrow gate, shortlist, judge, and transition modules. This cleanly separates operational retry from semantic outcome and reuses CP2 work recovery plus CP4 atomic transitions.
3. Introduce a generic policy/rule engine for all memory lifecycle decisions. This adds a second orchestration framework before the concrete CP5 policy is stable and is not justified by the checkpoint.

Option 2 is selected.

## Current Behavior Replaced By This Checkpoint

At the baseline revision:

- missing embedding policy promotes through `embedding_unavailable`;
- a high cosine score supersedes without semantic adjudication;
- malformed or unavailable judge output defaults to `keep_both`;
- low confidence leaves a candidate proposed for a person;
- sensitive terms and organization scope create human escalation;
- candidate TTL rejects old low-confidence proposals and clears conflict links;
- confidence decay sends approved memories into human review;
- the admin review API mixes proposals, conflicts, refuted memories, and low-confidence memories;
- the frontend offers bulk approve, reject, and threshold archive;
- a conflict is inferred from a proposed candidate plus a mutable link rather than represented by one canonical durable record.

Every item in that list is directionally superseded by this specification.

## Goals

- Make automatic decision work durable, retryable, fenced, and explainable.
- Settle all healthy non-conflict candidates without human action.
- Define deterministic noise, scope, redaction, exact-identity, and TTL rules.
- Query a bounded, authorized PostgreSQL comparison shortlist.
- Make the semantic judge strict, evidence-aware, and failure-safe.
- Apply every terminal result through CP4 atomic transition primitives.
- Make open conflicts durable across cleanup, retry, restart, and repair.
- Expose only open conflicts through the human inbox API and UI.
- Freeze hermetic eval thresholds before any active rollout.
- Prove provider and process failures cannot create semantic state.

## Non-Goals

- Memory CI, repository-revision validity, and branch overlays belong to CP7 and CP8.
- Query and curation performance consolidation beyond the bounded shortlist belongs to CP9.
- Historical backlog mutation and semantic repair belong to CP10.
- CP5 does not bulk-promote, bulk-reject, or bulk-supersede existing rows.
- CP5 does not infer truth from a model's self-reported confidence.
- CP5 does not add a second broker, relay, retry table, or dead-letter queue.
- CP5 does not make embeddings authoritative state.
- CP5 does not let organization-wide memory be created from ordinary agent evidence.
- CP5 does not hard-delete candidate, memory, version, decision, conflict, evidence, or transition history.
- CP5 does not expose operational retry work in the semantic conflict inbox.
- CP5 does not redesign general memory CRUD outside conflict resolution.

## Locked Terminology

`decision work` is the CP2 logical work item whose subject is one candidate. `attempt` is one leased execution of that stable work identity. `candidate snapshot` is the immutable CP3 input and evidence manifest bound to the decision work fingerprint. `effective scope` is the project or team scope permitted after deterministic narrowing. `comparison shortlist` is a bounded set of current authorized memory versions selected for semantic comparison. `judge verdict` is strict structured provider output. `semantic decision` is a validated terminal outcome recorded atomically with a CP4 transition. `conflict` is a canonical CP4 record joining two incompatible supported claim snapshots until an explicit resolution transition closes it. `operational failure` means required computation or state was unavailable. It changes no semantic truth. `semantic rejection` means the candidate projection was deterministically noise, unsafe after redaction, unsupported, or redundant. Its raw source evidence remains durable.

## Invariants Added By CP5

1. A proposed candidate has one active `candidate_decision` work identity or is repaired to one by reconciliation.
2. A candidate with unresolved canonical conflicts has no active decision work; the relations, not another candidate enum, are authoritative.
3. A settled generation has one `CurationDecision`; memory/conflict outcomes reference one CP4 transition, while ordinary rejection references none.
4. No provider failure or parser failure creates a semantic decision row.
5. No similarity-only branch calls a CP4 destructive transition.
6. Every target version in a decision was authorized before ranking and is still current when the transition locks commit.
7. Open conflict claim snapshots and evidence remain reconstructable after cleanup and worker restart.
8. The human inbox query is exactly the set of scoped open conflicts.
9. Candidate age and confidence never decide human routing.
10. Replaying a completed work id returns the same transition result.

## End-To-End Flow

```text
CP3 candidate + candidate_decision work commit
        |
        v
CP2 claim(work_id, fence)
        |
        v
C5.1 load immutable candidate snapshot
        |
        +-- invalid scope/provenance -> deterministic rejection
        +-- noise/redaction-empty -> deterministic rejection
        +-- exact same-scope claim -> merge evidence
        |
        v
C5.2 resolve embedding and authorized shortlist
        |
        +-- capability unavailable -> operational retry
        |
        v
strict evidence-aware judge
        |
        +-- malformed/untrusted output -> operational retry
        |
        v
C5 policy validates verdict and evidence threshold
        |
        +-- precondition advanced -> operational retry
        |
        v
CP4 atomic transition + semantic decision + work completion
        |
        +-- publish / merge evidence / revise / supersede / reject
        +-- open canonical conflict
```

Provider calls and embedding computation occur outside database lock-holding transactions. The final transition transaction locks and rechecks all behavior-relevant state.

## Prerequisite Interfaces From CP2, CP3, And CP4

CP5 consumes these contracts rather than creating alternative mechanisms.

### CP2 Work Runtime

CP2 provides `claim_work`, `heartbeat_work`, `lock_work_fence`, `finish_work_claim`, `fail_work_claim`, and `queue_work_attempt` with id-only delivery, bounded leases, fencing, retry-wait, blocked configuration state, attempt history, and idempotent completion. CP3 registers `WorkflowWorkType.CANDIDATE_DECISION` and `WorkflowSubjectType.MEMORY_CANDIDATE`; CP5 consumes them. The worker entry point is:
```python
process_candidate_decision_work_v1(work_id: UUID, workflow_run_id: UUID | None = None) -> None
```
Celery carries only work/run ids, never candidate body, policy, scope, or target. Before CP5 merges, the handler calls `fail_work_claim` with class `configuration`, code `candidate_decision_capability_unavailable`, a configuration fingerprint, and blocked state. It makes zero semantic mutations and never delegates to the legacy curator. CP5 changes the fingerprint, replaces the handler, and resumes the same work generations.

### CP3 Candidate Handoff

CP3 creates candidate and the first decision-work generation in the same transaction. The work input snapshot has this exact shape:
```json
{
  "schema": "candidate_decision_input/v1",
  "candidate_id": "uuid",
  "candidate_content_hash": "sha256-hex",
  "organization_id": "uuid",
  "project_id": "uuid",
  "team_id": "uuid-or-null",
  "evidence_manifest_hash": "sha256-hex",
  "policy_version": 1
}
```
The input fingerprint is canonical JSON SHA-256. The ordered evidence entry is `(window.input_hash, observation.session_sequence, observation_id, observation_digest, stage.stage_key, anchors_hash)`, sorted by those fields and hashed. `policy_version` is the deterministic CP5 contract, not the model policy resolved per attempt. The snapshot has no raw text, confidence, secrets, or kind. Content, scope, policy, and manifest are verified before attempts and CP4 commit. New evidence creates an immutable generation; post-terminal evidence uses `AttachPromotedCandidateSource`. CP5 executes only `decision_work_contract_version=1`; version 0 is shadow-reported and CP10-owned.

### CP4 Transition Runtime

CP4 remains the only writer for publication, version, lineage, exact projection, conflict opening, and conflict resolution. CP5 imports only `PromoteMemoryCandidate`, `MergeMemoryCandidate`, `ReviseMemoryFromCandidate`, `SupersedeMemoryWithCandidate`, `OpenMemoryConflict`, `ResolveMemoryConflict`, and late-provenance `AttachPromotedCandidateSource` from `engram.memory.transitions`.

Every call composes `TransitionRequest`, `CandidateFence(candidate_id, candidate_content_hash, evidence_manifest_hash)`, and each affected `MemoryFence(memory_id, current_transition_id, current_version_id, state_hash)`. The request uses `decision-work:<work_id>:settle:v1`. Settlement calls CP2 `lock_work_fence`, locks candidate, memories in UUID order, versions/conflicts, revalidates fences, executes the typed transition (or ordinary rejection), writes `CurationDecision`, and calls `finish_work_claim`. All affected authoritative rows commit or roll back together. CP4 raises retryable `stale_decision`; same-key/different-fingerprint raises `idempotency_collision` with no writes.

## Data Model Additions

CP5 uses additive migrations only.

### Candidate Semantic State

CP5 adds no redundant conflict status. `PROPOSED` requires active latest decision work unless unresolved `MemoryConflict` rows exist. `PROMOTED` and `REJECTED` are terminal. `OpenMemoryConflict` completes work while leaving the candidate proposed behind the canonical relation; only `ResolveMemoryConflict` may promote or reject it.

### `CurationDecision`

`CurationDecision` is append-only and belongs to the CP4 transition commit. Required columns:

- `organization`, `project`, and nullable `team`;
- one-to-one `work` and foreign key `candidate`;
- `contract_version`, fixed to `1` for this checkpoint;
- `input_fingerprint`, `evidence_manifest_hash`, and `comparison_manifest_hash`;
- `outcome` and stable `reason_code`;
- `effective_visibility_scope` and nullable `effective_team`;
- nullable protected `target_memory_version` FK;
- `evidence_tier`;
- nullable `provider_call_record`, `policy_id`, and `policy_version`;
- nullable `transition` and `conflict` references;
- `payload_hash` over the canonical validated decision;
- `created_at`.

The database enforces one decision per work and one payload per transition idempotency key. It stores identifiers, hashes, bounded reason codes, and redacted explanatory text; it does not duplicate raw provider prompts or secret-shaped bodies.

### Canonical Conflict Dependency

CP4 exposes canonical `MemoryConflict` with protected scope, candidate, memory, exact compared `memory_version`, `semantic_link`, `opened_transition`, and `evidence_hash`; nullable resolved transition/resolution/time form its only close operation. Those protected rows plus `CurationDecision` hashes are the two reconstructable claim snapshots, so CP5 does not duplicate bodies in conflict JSON. Open uniqueness is `(candidate, memory)`. An unresolved `MemoryConflict`, not mutable candidate JSON or link presence, is the authority. The protected `CONFLICTS_WITH` link remains immutable history after resolution.

## C5.1 - Deterministic Gates

### Gate Result Contract

Every gate returns one of:

- `continue` with a sanitized candidate view and effective scope;
- `terminal` with `merge_evidence` or `reject_candidate`;
- `retry` with an operational reason and no semantic payload.

Gate ordering is fixed and versioned as `deterministic_policy.v1`. Changing ordering or rule meaning increments the version and creates new work for affected candidates; it never reinterprets completed work in place.

### 1. Scope And Provenance Gate

The loader starts from organization and project ids frozen in the work input. It rejects the attempt before reading target memories if the project no longer belongs to the organization or the candidate scope does not match the work. All candidate evidence references must resolve inside that organization and project. A team-scoped candidate requires one matching team on candidate, work, and all team-bound evidence. Cross-organization or cross-project evidence is a security invariant failure: the attempt is recorded, no provider is called, and no candidate disposition is changed until scoped repair removes the impossible relation. Missing same-scope evidence that was never durably present is semantic reason `unsupported_provenance` and rejects the derived candidate. Evidence temporarily unreadable because of database or projection failure is operational reason `evidence_unavailable` and retries. Ordinary agent evidence may produce only project or team memory. Requested organization scope narrows to team when every source is bound to the same authorized team; otherwise it narrows to project. Requested project scope remains project. Requested team scope remains team only when the team checks pass. Requested session scope rejects with `non_durable_session_scope`; CP5 never widens session-only evidence automatically.

### 2. Redaction Gate

The candidate persisted by CP3 is already redacted. CP5 runs the shared redactor again on title, body, evidence excerpts, exact terms, file paths, and judge input as defense in depth. The gate records only the sanitized hash and redaction codes. If redaction leaves a non-empty durable claim, processing continues with the sanitized view. If title and body become empty, placeholder-only, or secret material is still detectable after redaction, the candidate rejects with `unsafe_content_after_redaction`. Sensitive-looking content never routes to a person merely because it is sensitive.

### 3. Narrow Noise Gate

Deterministic noise rules are intentionally high precision. The v1 rules reject:

- empty or whitespace-only title and body;
- body equal to title after Unicode normalization and whitespace folding;
- redaction-placeholder-only text;
- a provider parse-fallback wrapper with no resolved source claim;
- a lifecycle/control envelope containing no non-lifecycle observation;
- an exact duplicate candidate already terminal for the same work identity.

The v1 rules do not reject short claims, unfamiliar terms, low confidence, old candidates, or prose merely because it resembles a transcript. Ambiguous value is decided by the evidence-aware judge, not a growing regex list. Stable semantic reason codes are `noise_empty`, `noise_title_echo`, `noise_redaction_only`, `noise_parse_wrapper`, and `noise_lifecycle_only`.

### 4. Exact Identity Gate

The canonical claim hash is SHA-256 over normalized redacted title, body, kind, effective visibility scope, and effective team id. The query is scoped by organization, project, current active version, and the exact effective scope key. A match is verified by canonical byte comparison. `MergeMemoryCandidate` creates a new target version with identical content and provenance equal to the prior target version plus candidate sources. If every source is already attached, reason `exact_duplicate_no_new_evidence` settles idempotently without another version. Same content in another scope enters the shortlist because authorization differs. Hash collision or multiple current exact matches is an invariant failure and retries.

### 5. Conflict-Preserving TTL

`ExpireStaleCandidates` no longer rejects candidates. The periodic job becomes `reconcile_candidate_decision_work` and performs only idempotent recovery of missing active work for scoped proposed candidates. Age remains a lag metric and scheduler ordering input. No age threshold changes candidate semantic state. Cleanup queries must exclude:

- candidates or versions referenced by any open conflict;
- conflict claim snapshots and evidence manifests;
- curation decisions and CP4 transitions.

Conflict links are protected immutable history and are never cleared by TTL, ordinary rejection, projection cleanup, or resolution. Only `ResolveMemoryConflict` may close the canonical conflict rows.

## C5.2 - Authorized Shortlist And Failure-Safe Judge

### Shortlist Boundary

`BuildCurationShortlist` is dedicated and never materializes every document in Python. PostgreSQL authorizes before ranking: same organization/project current CP4 versions, project-visible rows, and team-visible rows only for the effective team; session/organization/foreign-team rows are excluded. Stale, refuted, archived, superseded, and non-current rows are also excluded. Open-conflict versions appear only as tagged context and never authorize destruction. The preferred query uses pgvector plus bounded lexical recall:

- vector leg: top 8 by cosine distance, distance at most `0.45`;
- lexical leg: top 4 with full-text rank `> 0` or trigram word similarity `>= 0.30`;
- exact-term overlap leg: top 4 by exact terms and symbols;
- union: de-duplicate by current memory version and cap at 12;
- final order: exact overlap, vector distance, lexical rank, version id.

Thresholds only control recall and cost. They never map directly to merge, revise, supersede, reject, or conflict. The shortlist manifest stores each version id, current transition id, scope key, rank signals, conflict tag, and canonical body hash. The manifest hash is bound to the judge request and final transition.

### Embedding Failure And Safe Fallback

The primary embedding policy may use its explicitly configured provider fallback. If both fail, a non-empty project corpus cannot be declared comparison-free. The work retries with `embedding_unavailable`. Lexical similarity alone may still find an exact comparison target, but a lexical-only empty result cannot authorize blind publication. The only no-embedding publication case is an authorized database proof that the effective scope has zero current memory versions. Deterministic rejection and exact-identity evidence merge do not require an embedding.

### Evidence Manifest And Tiers

Evidence tiers are computed from the frozen CP3 manifest and immutable CP4 provenance; the provider cannot upgrade them. An eligible source is same-scope, attributable, durable, non-lifecycle evidence whose observation digest and anchors hash verify. The v1 independence key is `window.input_hash`: repeated delivery, chunks/stages, and multiple observations in one window are one group. CP5 defines no automatic authority shortcut; model output, confidence, command text, and commit-like anchors cannot substitute for a second independent window.

The tiers are:

- `none`: no eligible source;
- `supported`: at least one eligible provenance group;
- `corroborated`: at least two distinct eligible `window.input_hash` groups.

Publication of a new non-destructive memory requires `supported`. Evidence merge requires candidate and target each be at least `supported`. Revision requires candidate `corroborated`, target `supported`, same subject, and deterministic newer-window evidence. Supersession requires candidate `corroborated`, target `supported`, complete comparison of current target evidence, and deterministic temporal precedence. Conflict requires both claims `supported`, the same applicability, and no safe precedence. Missing target provenance causes operational retry and CP4 consistency repair, never override or human routing.

### Judge Input

The prompt contains only redacted bounded claim snapshots and opaque evidence reference tokens. It includes the candidate, all shortlist entries, applicability/scope facts, source-type and time metadata, and current transition ids. It excludes provider credentials, raw secrets, unrelated project memories, and unbounded observations. The judge compares all shortlist entries in one call and selects at most one target in v1.

### `CurationJudgeVerdictV1`

The provider must return one JSON object with exactly these keys:
```json
{
  "schema_version": 1,
  "outcome": "publish_new|merge_evidence|revise_memory|supersede_memory|reject_candidate|open_conflict",
  "relation": "unrelated|compatible_distinct|equivalent|candidate_revises|candidate_supersedes|redundant|unsupported|mutually_incompatible",
  "target_memory_version_id": "uuid-or-null",
  "candidate_evidence_refs": ["opaque-token"],
  "comparisons": [{"memory_version_id": "uuid", "relation": "relation-enum", "target_evidence_refs": ["opaque-token"]}],
  "applicability": "same|different",
  "temporal_order": "candidate_newer|target_newer|unordered|not_applicable",
  "reason_code": "distinct_claim|equivalent_claim|same_subject_revision|ordered_replacement|redundant_claim|unsupported_claim|same_scope_contradiction",
  "reason": "redacted bounded explanation"
}
```
`additionalProperties` is false recursively. `comparisons` contains every shortlist version exactly once in manifest order; its relation enum is closed, and a selected target's relation equals the top-level relation. `reason` is required/redacted/capped at 500 but never authorizes. Evidence arrays are de-duplicated, capped at 16, and reference only manifest tokens. A non-null target must be a shortlist member.

The allowed combinations are exact:

| Outcome | Relation | Target | Additional policy |
|---|---|---:|---|
| `publish_new` | `unrelated` or `compatible_distinct` | null | candidate supported and comparison complete |
| `merge_evidence` | `equivalent` | required | both claims supported and same applicability |
| `revise_memory` | `candidate_revises` | required | candidate corroborated and newer |
| `supersede_memory` | `candidate_supersedes` | required | destructive threshold and precedence pass |
| `reject_candidate` | `redundant` | required | target supported |
| `reject_candidate` | `unsupported` | null | candidate evidence tier is none |
| `open_conflict` | `mutually_incompatible` | required | both supported, same applicability, unordered |

Any other combination is invalid. Unknown enums, missing or extra keys, invalid ids, unlisted targets, invented evidence refs, over-limit arrays, incompatible fields, or invalid JSON produce `judge_invalid_output` and operational retry. There is no parser default and no `keep_both` or `skip` outcome.

### Provider Fallback

The curation policy's explicit fallback setting controls whether a second configured policy may be attempted. Primary and fallback use response kind `curation_decision_v1` and the same strict schema. Malformed primary output may use the configured fallback after the failed call is recorded. If fallback is disabled, resolves to the same policy, or fails validation, the attempt ends retryable with no semantic decision. Provider-call provenance identifies the policy that produced the accepted verdict.

## C5.3 - Work-Driven Automatic Decision Orchestration

### Orchestrator Interface

`DecideMemoryCandidate.execute(work_id, fence_token)` is the sole CP5 semantic orchestrator. Observation and distillation services do not call curation synchronously; they only create candidate plus work through CP3.

The orchestrator:

1. validates the CP2 claim and immutable CP3 input fingerprint;
2. verifies the frozen evidence manifest and runs C5.1 gates;
3. returns an exact deterministic terminal decision when possible;
4. resolves embedding and builds the authorized shortlist;
5. calls the strict judge with explicit fallback;
6. validates the verdict against evidence thresholds;
7. opens one short transaction;
8. locks work, candidate, target current versions, and related conflict rows;
9. rechecks fence, candidate hash, frozen manifest, latest generation, scope, target transitions, and shortlist manifest;
10. calls the mapped typed CP4 service, or ordinary-rejection settlement, with the work idempotency key;
11. commits decision, transition, exact projection, audit, and work completion;
12. emits asynchronous embedding projection intent after commit.

Provider calls occur outside lock-holding transactions. A changed CP4 fence raises `stale_decision`, discards the verdict, and retries. A newer candidate/evidence generation atomically settles the older work as `superseded_generation` with no semantic decision, then the newer generation runs. No stale verdict is patched to fit new state.

### Persisted Semantic Outcome Contract

The canonical decision payload is:
```json
{
  "contract": "curation_decision.v1",
  "work_id": "uuid",
  "candidate_id": "uuid",
  "input_fingerprint": "sha256-hex",
  "outcome": "publish_new|merge_evidence|revise_memory|supersede_memory|reject_candidate|open_conflict",
  "reason_code": "noise_empty|noise_title_echo|noise_redaction_only|noise_parse_wrapper|noise_lifecycle_only|unsupported_provenance|unsafe_content_after_redaction|non_durable_session_scope|exact_identity|exact_duplicate_no_new_evidence|distinct_claim|equivalent_claim|same_subject_revision|ordered_replacement|redundant_claim|unsupported_claim|same_scope_contradiction",
  "effective_scope": {"visibility_scope": "project|team", "team_id": "uuid-or-null"},
  "target_memory_version_id": "uuid-or-null",
  "evidence_tier": "none|supported|corroborated",
  "evidence_manifest_hash": "sha256-hex",
  "comparison_manifest_hash": "sha256-hex",
  "judge": {"status": "not_required|succeeded", "provider_call_record_id": "uuid-or-null", "policy_id": "uuid-or-null", "policy_version": "integer-or-null", "response_hash": "sha256-hex-or-null"}
}
```
`transition_id`, `conflict_id`, and resulting memory/version ids are relational results attached in the same transaction, not caller-supplied fields.

### Semantic Outcomes

Outcome mapping is exact: `publish_new` -> `PromoteMemoryCandidate`; `merge_evidence` -> `MergeMemoryCandidate` with a new target version and combined provenance; `revise_memory` -> `ReviseMemoryFromCandidate`; `supersede_memory` -> `SupersedeMemoryWithCandidate`; `open_conflict` -> `OpenMemoryConflict`; ordinary `reject_candidate` -> fenced CP5 disposition only. Each mapped service owns projection/audit. Conflict open creates protected rows and CP5 read filters withhold claims. Every outcome preserves history and leaves no proposed candidate unless canonical open conflicts explain it.

CP2 completion mapping is exact: publication, merge, revision, supersession, and conflict use `product_succeeded`; candidate rejection and an older `superseded_generation` use `product_no_signal`; a fence/precondition refresh uses `continue_required` plus `queue_work_attempt`; dependency failures use `fail_work_claim`.

### Operational Reasons

The stable vocabulary is `candidate_decision_capability_unavailable`, `evidence_unavailable`, `embedding_policy_unavailable`, `embedding_provider_unavailable`, `embedding_invalid_result`, `shortlist_query_failed`, `judge_policy_unavailable`, `judge_provider_unavailable`, `judge_invalid_output`, `judge_reference_invalid`, `stale_decision`, `transition_contention`, `transition_dependency_unavailable`, `superseded_generation`, and `rollout_not_enabled`.

These reasons live on CP2 attempt/work observability, not candidate status. Configuration absence may become operationally blocked with bounded backoff and health alerting, but never semantic rejection.

CP2 classification is exact: missing policy/secret/capability is `configuration` plus blocked and a configuration fingerprint; provider timeout/rate limit/5xx is `provider_transient` plus retry-wait; database or projection outage is `infrastructure_transient`; lease loss is `worker_lost`; schema-valid call with invalid verdict is `invalid_input` plus retry-wait; an unclassified defect is `unexpected`. Semantic outcomes do not call `fail_work_claim`.

### Failure Classification

| Condition | Classification | Candidate state | Work action |
|---|---|---|---|
| deterministic noise | semantic rejection | rejected after CP4 transition | complete |
| durable same-scope evidence absent | semantic rejection | rejected | complete |
| equivalent current claim | semantic merge | promoted to existing memory | complete |
| model says unsupported and code confirms tier `none` | semantic rejection | rejected | complete |
| missing embedding on non-empty scope | operational | proposed | retry/blocked |
| provider timeout, rate limit, missing secret, or 5xx | operational | proposed | retry/blocked |
| malformed or invented judge references | operational | proposed | retry |
| target version changed after judgment | operational | proposed | retry |
| CP4 transaction rolls back | operational | proposed | retry |
| two supported same-scope incompatible claims | semantic conflict | proposed plus open conflict | complete |

### Reconciliation And Idempotency

The reconciler uses `CandidateDecisionWorkBuilder.expected_input(candidate_id)` to create the current missing generation and never duplicates a fingerprint. A work whose snapshot differs from that exact current input settles `superseded_generation`. Concurrent delivery may create attempts, but only the winning fence calls CP4. Crash after provider return leaves no decision; crash after commit replays idempotency. Competing candidates lock target versions in CP4 order and the loser re-shortlists. Post-terminal new evidence uses `AttachPromotedCandidateSource` or a CP3 successor candidate, never reopened work or direct provenance mutation.

## Conflict Semantics And Durability

### Genuine Conflict Predicate

`open_conflict` is allowed only when all conditions are true:

1. both claims have at least `supported` evidence;
2. both claims concern the same durable subject;
3. both apply to the same project/team scope and applicability known to CP5;
4. the claims cannot both be true in that applicability;
5. neither is merely a narrower case;
6. neither is a historical statement compared with a current statement;
7. no deterministic temporal precedence safely selects one;
8. neither is unsupported model inference;
9. the target version is current and comparison-complete;
10. no existing open conflict already represents the claim pair.

Different team, environment, time interval, posture, or subject means revise, publish separately, narrow, supersede, or reject, not conflict. CP8 later adds repository revision to applicability without weakening this predicate.

### Retrieval During Conflict

Claims participating in an open conflict are not injected as settled current truth. CP4 intentionally leaves the memory current pointer unchanged; CP5 adds an authorization-stage `NOT EXISTS` filter for unresolved `MemoryConflict` before shortlist, search, or context ranking. Exact historical inspection remains possible. Context may emit a compact conflict warning after CP6, but CP5 never chooses one side for retrieval.

### Resolution Operations

The inbox groups all unresolved `MemoryConflict` rows for one candidate. It exposes exactly the four CP4 resolution outcomes:

- `publish_candidate`: publish a second active memory and keep every compared memory active;
- `merge_candidate`: select one compared target, create its new version with combined provenance, and leave other compared memories active;
- `supersede_memory`: select one compared target to stale/link, publish the candidate result, and leave other compared memories active;
- `reject_candidate`: reject the candidate and leave every compared memory unchanged.

Every action requires a reason, `memories:admin`, and the conflict-set ETag. Merge/supersede require a target id from that set; other ids are rejected before writes. `ResolveMemoryConflict` locks the candidate's complete open set and every current memory version, revalidates CP4 fences, closes every row against one terminal transition, retains protected links, records the human example/audit, and updates projections atomically. There is no bulk resolution across candidates.

## C5.4 - Conflict-Only Backend Surface

The existing `/v1/admin/memory-review/` namespace remains the compatibility path but its resource type becomes `conflict`.

### List

`GET /v1/admin/memory-review/` returns one item per scoped candidate with at least one unresolved conflict. Supported filters are `project_id`, `team_id`, `opened_at__gte`, and bounded text `search` over redacted claim titles/bodies. Ordering is `-opened_at,-id` or `opened_at,id` with cursor pagination. Confidence, status, threshold, source-type, and archive filters are removed. The list response item is:
```json
{
  "id": "candidate-uuid",
  "type": "conflict",
  "state": "open",
  "conflict_ids": ["uuid"],
  "project_id": "uuid",
  "team_id": "uuid-or-null",
  "visibility_scope": "project|team",
  "reason_code": "same_scope_contradiction",
  "opened_at": "rfc3339",
  "candidate_claim": {"title": "redacted", "kind": "string", "body_hash": "sha256-hex"},
  "existing_claims": [{"memory_id": "uuid", "version_id": "uuid", "title": "redacted", "kind": "string", "body_hash": "sha256-hex"}]
}
```
### Detail

`GET /v1/admin/memory-review/<candidate_id>/` returns the list fields plus:

- complete redacted candidate and compared-claim bodies;
- memory, version, candidate, decision, and transition ids;
- effective applicability and scope evidence;
- ordered bounded evidence summaries with immutable reference ids;
- judge reason and provider-call provenance without secrets or raw prompt;
- resolution actions allowed for the current claim shape;
- `etag` equal to SHA-256 over candidate id plus sorted open conflict ids, opened transition ids, evidence hashes, and compared memory fences.

The response header carries the same `ETag`. Unknown, resolved, or foreign-scope conflicts return 404 to avoid existence leakage. Read requires `memories:review`.

### Resolve

`POST /v1/admin/memory-review/<candidate_id>/resolve/` accepts:
```json
{
  "action": "publish_candidate|merge_candidate|supersede_memory|reject_candidate",
  "reason": "required 1..1024 chars",
  "target_memory_id": "conditional uuid",
  "merged_title": "conditional string",
  "merged_body": "conditional string"
}
```
`If-Match` is required. Missing precondition returns 428, a changed/resolved set returns 412, invalid action/target shape returns 400, and unknown/foreign candidate returns 404. Success returns candidate id, all closed conflict ids, state `resolved`, resolution code, CP4 transition id, and resulting memory/version ids. Write requires `memories:admin`. Old generic `action`, `bulk-action`, `bulk-archive`, and `diff` routes are removed in the backend/frontend cutover PR.

## C5.4 - Conflict-Only Frontend

The route remains `/memory-review` for navigation compatibility and is titled `Memory Conflicts`. The page contains:

- open conflict count and project/team filters;
- candidate summary plus compared-claim count and first compared summary per row;
- conflict age and evidence-strength badges;
- a detail drawer with the candidate beside a selectable compared claim;
- evidence and provenance lists for both sides;
- explicit warning that neither claim is settled truth;
- one resolution form using the four actions and a compared-target selector only for merge/supersede;
- stale-ETag handling that reloads before another submission;
- a link to workflow health for provider/retry lag.

The page contains no proposal list, confidence threshold, bulk selection, bulk approve/reject, archive control, or operational error item. Errors preserve the form only for retryable network failure. HTTP 404, 412, or a terminal authorization error clears stale conflict detail after showing the bounded error. Frontend types use a discriminant `type: 'conflict'`; legacy candidate/memory review unions and generic action payloads are deleted. The pure action-shape and ETag helpers live outside the page component and use Node's existing `node:test` pattern.

## Offline Evaluation Contract

### Corpus

Committed JSONL fixtures live under `apps/backend/engram/memory/evals/curation_v1/`. Each case contains redacted candidate/evidence input, authorized current versions, expected deterministic gate, allowed semantic outcome set, forbidden outcomes, expected target ids, minimum evidence tier, and whether an open conflict is valid. The initial corpus has at least 120 cases and these minimum buckets:

- 15 exact identities and duplicate-evidence cases;
- 15 deterministic noise/redaction/scope cases;
- 20 compatible distinct or new-publication cases;
- 15 equivalent merge-evidence cases;
- 15 revisions;
- 10 safe supersessions;
- 15 genuine conflicts;
- 10 same-looking but different-scope/time/posture non-conflicts;
- 5 provider/structured-output fault cases.

Every semantic bucket includes cross-project and cross-team negative controls. Cases record authorship and immutable source hashes, not raw production data.

### Scorer

`engram_curator_eval` runs in two modes. `--engine fixture` uses a deterministic fixture judge and exercises gates, validation, orchestration, and CP4 transition fakes without network access. `--responses <jsonl>` scores captured provider verdicts against the same corpus without mutating product state. The scorer emits JSON with corpus hash, contract version, bucket counts, confusion matrix, forbidden-transition count, conflict precision/recall, destructive precision, target accuracy, and automatic convergence rate.

### Frozen Thresholds

The hermetic CI and provider qualification thresholds are:

- cross-scope leakage: exactly 0;
- operational failure producing semantic decision: exactly 0;
- forbidden or similarity-only destructive transition: exactly 0;
- deterministic gate accuracy: 100%;
- destructive outcome precision: 100%;
- conflict recall: 100%;
- conflict precision: at least 95%;
- target-version accuracy for merge/revise/supersede: at least 95%;
- macro F1 across six semantic outcomes: at least 0.92;
- healthy non-conflict convergence: fixture engine 100%, provider single-pass at least 98%, with any remainder safe operational retry;
- unresolved `skip`-equivalent outcomes: exactly 0.

Any corpus change updates the corpus hash and must pass the same or stricter thresholds in the PR that changes it. A model/policy version cannot enter active rollout until its captured response artifact passes the provider qualification gate.

## RED, Fault, And Concurrency Tests

Each serial PR begins with the named regression failing for the current behavior.

### C5.1 RED Tests

- `test_low_confidence_candidate_gets_decision_work_not_human_review`;
- `test_sensitive_candidate_redacts_or_rejects_without_human_escalation`;
- `test_org_scope_is_deterministically_narrowed_before_comparison`;
- `test_candidate_ttl_never_rejects_or_unlinks_open_conflict`;
- `test_exact_identity_merges_provenance_without_new_version`;
- `test_cross_scope_evidence_calls_no_provider_and_mutates_nothing`.

### C5.2 RED Tests

- `test_pgvector_shortlist_authorizes_before_distance_ordering`;
- `test_similarity_point_999_cannot_choose_destructive_outcome`;
- `test_missing_embedding_on_nonempty_scope_retries_without_publication`;
- `test_malformed_judge_output_has_no_default_semantic_outcome`;
- `test_judge_cannot_reference_memory_or_evidence_outside_manifest`;
- `test_fallback_verdict_must_pass_the_same_strict_schema`;
- `test_destructive_verdict_below_evidence_threshold_is_not_applied`.

### C5.3 Fault And Concurrency Tests

- `test_crash_after_embedding_preserves_proposed_candidate_and_retryable_work`;
- `test_crash_after_judge_response_creates_no_semantic_decision`;
- `test_target_version_advance_fences_stale_judgment`;
- `test_cp4_fault_at_each_transition_boundary_rolls_back_work_completion`;
- `test_crash_after_commit_replays_one_decision_and_transition`;
- `test_concurrent_candidate_decisions_on_one_target_relist_the_loser`;
- `test_expired_worker_fence_cannot_apply_provider_result`;
- `test_reconciler_restores_one_missing_decision_work_identity`.

### C5.4 Backend And Frontend Tests

- `test_human_inbox_returns_only_open_conflicts`;
- `test_low_confidence_refuted_and_proposed_rows_never_enter_inbox`;
- `test_conflict_detail_contains_candidate_and_all_compared_claims`;
- `test_conflict_resolution_requires_current_etag`;
- `test_conflict_resolution_closes_complete_candidate_set_in_one_cp4_transition`;
- `test_resolved_conflict_disappears_without_deleting_evidence`;
- `test_foreign_conflict_list_detail_and_resolve_return_no_existence`;
- `memory-conflict-actions.test.ts` covers conditional payloads and ETags;
- frontend typecheck proves no legacy generic review action remains.

### Reliability Matrix Extension

Implementation adds CP5 rows to the fault matrix:

- F20: embedding/provider capability fails before judgment;
- F21: malformed verdict or invented target/evidence reference;
- F22: process dies after judgment before CP4 transition;
- F23: target version advances between shortlist and commit;
- F24: process dies after CP4 commit before task acknowledgement;
- F25: TTL/cleanup/restart encounters an open conflict.

Each target outcome is retry with no semantic change, idempotent replay of one transition, or durable conflict preservation.

## Rollout-Independent CI Gate

CI calls the domain services directly and does not depend on organization feature flags, canary cohorts, production state, network providers, Celery beat timing, or an existing backlog. All Python, backend, eval, and E2E commands run inside Compose containers. The minimum CP5 gate is:

```powershell
docker compose -f deploy/compose/docker-compose.yml run --rm api `
  poetry run pytest -q `
  engram/memory/deterministic_gates_tests.py `
  engram/memory/curation_shortlist_tests.py `
  engram/memory/curation_judge_tests.py `
  engram/memory/curation_tests.py `
  engram/memory/candidate_ttl_tests.py `
  engram/console/views/memory_review_tests.py

docker compose -f deploy/compose/docker-compose.yml run --rm api `
  poetry run python manage.py engram_curator_eval --engine fixture --format json

docker compose -f deploy/compose/docker-compose.yml run --rm frontend-ci `
  sh -ec "pnpm typecheck && pnpm lint && pnpm build && node --test src/lib/memory-conflict-actions.test.ts"
```

`frontend-ci` is a test-only Compose service/target containing source plus dev dependencies; the production runner image is unchanged. The eval command exits nonzero on a threshold miss. CI also runs migration checks, `git diff --check`, P6/P8/P9/P12 tests, and the CP4 fault subset. No rollout flag may skip a contract/eval test.

## Rollout And Backlog Safety

Rollout controls dispatch and mutation, never decision semantics. The same domain service and eval policy run in every stage.

1. Run a read-only shadow command on a deterministic sampled backlog.
2. Persist only a redacted report artifact, not candidate decisions or CP4 transitions.
3. Compare report outcomes against the committed corpus and provider gate.
4. Canary deterministic noise rejection for an explicit project cohort.
5. Canary non-destructive new publication and exact evidence merge.
6. Canary revision after transition rollback evidence is green.
7. Enable supersession only after destructive precision, conflict recall, and conflict resolution rollback tests pass continuously.
8. Switch the human surface to conflicts only when P12 remains healthy.
9. Leave unsampled historical proposals untouched for CP10 repair.

The dispatcher does not claim work whose cohort or outcome class is disabled. If a claimed verdict reaches a disabled outcome class, work remains visible with `rollout_not_enabled`; it is not converted to rejection or conflict. The shadow report includes counts by gate/outcome, retry reason, target ids, evidence tier, conflict candidates, forbidden transitions, and corpus/policy versions. It never applies decisions from the report.

## Observability

Metrics separate operational health from semantic quality. Required operational metrics:

- decision work ready, leased, retry-wait, blocked, and oldest age;
- attempts by stable operational reason;
- embedding and judge latency, failure, and fallback counts;
- precondition invalidation and stale-fence counts;
- CP4 transition rollback and idempotent replay counts.

Required semantic metrics:

- decisions by outcome and deterministic/model route;
- evidence tier by outcome;
- open conflict count and age;
- conflict resolution count and code;
- non-conflict convergence rate;
- similarity-only destructive guard violations, which must remain zero;
- non-conflict rows returned by inbox, which must remain zero.

Logs contain work, candidate, decision, transition, conflict, provider-call, and correlation ids plus reason codes. Logs never contain raw candidate bodies, prompts, provider responses, secrets, or unredacted evidence excerpts. Operational health alerts link to workflow health, not the conflict inbox.

## Serial PR Spine And File Ownership

Central models and migrations have one owner for the whole checkpoint. `apps/backend/engram/memory/curation.py` has one writer in C5.3. CP4 transition files remain owned by the CP4 interface owner; CP5 integrates through the frozen interface and places CP5 assertions in CP5 test files.

### C5.1 - Deterministic Gates

Owner files:

- `apps/backend/engram/core/models.py` and one additive migration;
- new `apps/backend/engram/memory/deterministic_gates.py` and tests;
- `apps/backend/engram/memory/candidate_ttl.py` and tests;
- `apps/backend/engram/memory/escalation.py` removal/deprecation;
- narrow candidate-handoff edits in `services.py` and `distillation.py` after CP3 merges.

This PR freezes candidate state, evidence tiers, scope policy, reason codes, and exact-identity contract.

### C5.2 - Shortlist And Judge

Owner files:

- new `curation_shortlist.py` and tests;
- new `curation_judge.py` and tests;
- model-policy structured response schema, fake provider, gateway, and focused tests;
- no writes to the CP4 transition implementation.

This PR freezes shortlist manifest and `CurationJudgeVerdictV1`.

### C5.3 - Orchestrator

Owner files:

- `curation.py` and focused tests;
- `tasks.py` and focused task tests;
- CP2 work runtime, reconciler, and observability integration;
- CP4 integration tests through the public transition interface.

This PR removes synchronous curation calls and proves fault/idempotency behavior.

### C5.4 - Conflict Surface And Eval

Backend owner files:

- console memory-review view, serializer, service/use case, filters, URLs, and focused tests;
- `apps/backend/engram/context/services.py`, `apps/backend/engram/search/services.py`, and focused unresolved-conflict exclusion tests;
- review-example export changes for conflict resolutions.

Frontend owner files:

- `apps/frontend/src/lib/admin-api.ts`;
- `apps/frontend/src/hooks/use-memory-review.ts`;
- `apps/frontend/src/app/(admin)/memory-review/page.tsx`;
- query keys/navigation, pure helper tests, and the test-only frontend Docker/Compose target.

Eval owner files:

- new `apps/backend/engram/memory/evals/curation_v1/` corpus and scorer;
- new `engram_curator_eval` management command and tests;
- reliability invariant/fault documentation and AI workflow/API/admin docs.

Backend, frontend, and eval work may proceed in parallel only after the C5.2 outcome and conflict contracts are frozen.

## Documentation Supersession

The implementation updates these directional documents in C5.4:

- `docs/ai-workflow-loop.md`;
- `docs/api-reference.md`;
- `docs/admin-ui-requirements.md`;
- `docs/guides/admin-ui.md`;
- `docs/reliability/memory-loop-invariants.md`;
- `docs/reliability/memory-loop-fault-matrix.md`;
- the historical auto-review spec with a superseded status note.

The new docs state that confidence is descriptive, TTL is non-semantic, model failure retries, and only canonical open conflicts are human work.

## Checkpoint Acceptance Gate

C5 is complete only when all of the following are true:

- P6, P8, P9, and P12 are healthy with exact evidence;
- every fresh non-conflict candidate converges automatically when dependencies are healthy;
- every proposed candidate has active decision work;
- provider and parser failures produce zero semantic decisions;
- 0.999 cosine similarity alone cannot select a destructive transition;
- all memory/conflict decisions use typed CP4 transitions and ordinary rejection uses the fenced CP5 disposition exception;
- crash and stale-fence tests converge to one transition;
- TTL, cleanup, retry, and restart preserve open conflict evidence;
- the API and UI return open conflicts only;
- conflict detail includes both claims, versions, provenance, and resolution;
- the hermetic eval meets every frozen threshold;
- the selected rollout policy's captured provider artifact meets the same quality gate;
- the backlog shadow report exists and applied zero mutations;
- no historical backlog was bulk promoted, rejected, or superseded;
- Compose verification commands, counts, exit codes, and unresolved risks are recorded in the checkpoint report.

## Stop Conditions

Stop the checkpoint before implementation proceeds if:

- CP4 cannot atomically include decision, conflict, audit, exact projection, and work completion in one transition;
- CP3 cannot bind candidate and decision work in one commit;
- CP2 cannot preserve retryable work without mapping provider failure to a semantic status;
- evidence provenance cannot distinguish independent groups deterministically;
- authorized shortlist filtering cannot occur before ranking;
- conflict claim snapshots can be deleted by ordinary retention;
- any proposed fallback requires blind publication or similarity-only destruction;
- the eval corpus cannot reach 100% conflict recall and destructive precision;
- active rollout would mutate historical backlog outside CP10 authority.

The recommended response is to fix the failed prerequisite or narrow the rollout stage, not weaken the CP5 invariants.
