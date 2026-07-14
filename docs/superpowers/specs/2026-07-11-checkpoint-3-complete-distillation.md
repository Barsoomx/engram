# Checkpoint 3 Complete And Idempotent Distillation

Date: 2026-07-11

Status: implementation-ready design

Authority:

- `docs/superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md`, Checkpoint 3;
- `docs/reliability/memory-loop-invariants.md`, P3, P5, P6, and P14;
- `docs/reliability/memory-loop-fault-matrix.md`, F10 through F13;
- `docs/superpowers/specs/2026-07-10-checkpoint-0-reliability-contract.md`;
- `docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`;
- the accepted `docs/decisions/2026-07-10-domain-progress-and-transport.md`;
- `docs/superpowers/specs/2026-07-11-checkpoint-2-leases-reconciliation.md` and its `0035_workflow_work_execution.py` schema contract.

## Goal

Make one immutable session-distillation generation complete, resumable, and idempotent in durable effect even when task delivery and provider calls occur more than once.

Every useful observation inside the CP1 sequence watermark must end with an inspectable `signal` or `no_signal` coverage row. A signal must reach at least one durable candidate source. The root `WorkflowWork` may become complete only after all extraction chunks and all required reduction stages are complete and the final coverage, candidate provenance, candidate-decision work, package signals, and root completion commit atomically.

The old `ENGRAM_DISTILL_MAX_CHUNKS` behavior is removed. A cap may limit provider calls in one leased attempt, but the same root work is continued until its entire frozen window is complete. No configuration value may turn an unprocessed tail into success, escalation, or a review item.

## Success Boundary

Checkpoint 3 closes when:

- the CP1 `session_distillation_input/v1` snapshot remains the generation authority;
- deterministic manifests and hashes prove the exact observation prefix and every chunk derived from it;
- a bounded attempt resumes the same root work rather than truncating it or creating child workflow work;
- provider-stage identity is stable across replay and every accepted result has call, policy, response-hash, output-hash, and fallback provenance;
- malformed extraction or reduction output is an operational failure, never a zero-confidence candidate;
- final reduction consumes every valid leaf draft and cannot silently drop a draft or source observation;
- one transaction creates or reuses candidates, appends source relations, writes observation coverage, creates candidate-decision work and its initial package signal, and completes the root work;
- P3 and P5 are exact for the CP3 cohort while older history remains honestly reported as unobservable;
- F10 through F13 have focused executable evidence, with CP5 retaining ownership of autonomous semantic decision outcomes.

## Non-Goals

Checkpoint 3 does not:

- build a general workflow, DAG, saga, or arbitrary-stage engine;
- create one `WorkflowWork` or Celery task per extraction or reduction stage;
- guarantee exactly-once external provider billing;
- change `django-celery-outbox` package, relay, retry, or dead-letter authority;
- replay or repair the historical distillation backlog;
- reopen a terminal candidate decision when later source evidence arrives;
- implement CP4 promotion, version, projection, audit, or lineage atomicity;
- implement CP5 evidence-aware curation outcomes or expose ordinary candidates as a supported human-review workflow;
- mutate or delete legacy provider-call, workflow-run, candidate, or memory history;
- change production, deployment, SSH, Kubernetes, or D2 state.

## Current Contradictions To Remove

The current service is useful characterization evidence, not the target contract:

1. `DistillSession.execute()` reloads the whole mutable session and orders by `prompt_number, created_at` rather than the CP1 frozen sequence prefix.
2. `_distill_max_chunks()` slices the chunk list and permanently omits the tail.
3. Chunk request ids include a random run scope, so replay has no stable stage identity.
4. Provider results live in process memory until all calls finish; a later failure loses earlier valid chunk progress.
5. `parse_synthesized_candidates()` converts malformed structured output into a proposed zero-confidence candidate.
6. Reduction can be skipped or replaced with a union when its prompt is over budget, the provider fails, or the response is malformed.
7. Candidate evidence is JSON only; no exact source relation proves P5 or gives CP4 an authoritative provenance set.
8. Candidate creation and direct inline curation are split, so a crash can leave a proposal without automatic decision work.
9. `ProviderCallRecord.request_id` detects repeated calls but does not prove which one produced an accepted stage result.
10. `distill_session_work_v1` is still a fail-closed placeholder and therefore cannot consume the CP1 root-work snapshot.

Existing tests that require permanent truncation or malformed-output fallback candidates are replaced, not preserved as compatibility requirements.

## Alternatives Considered

### Selected: one root work plus a distillation-specific ledger

The existing session `WorkflowWork` remains the only product work identity. Window, chunk, provider-stage, coverage, and candidate-source rows are domain facts beneath it. A leased attempt executes a bounded number of pending stages, then uses the CP2 continuation primitive on the same work id.

This is selected because it preserves CP1 identity, CP2 recovery, outbox ownership, and simple operator reasoning. It adds only relations needed to prove P3/P5 and stage replay.

### Rejected: child `WorkflowWork` for every chunk and reduction batch

This would require parent/child scheduling, dependency propagation, cancellation, and aggregate completion semantics. It would be a workflow engine in disguise and would multiply package and lease state without improving the invariant.

### Rejected: keep the monolithic attempt and only increase the cap

Any finite cap still loses a larger tail. An unbounded attempt still loses all in-memory progress on worker death and cannot prove coverage or accepted provider provenance.

## Authority And State Boundaries

- `WorkflowWork.input_snapshot` and `input_fingerprint` remain immutable generation authority.
- `WorkflowWork.disposition` remains product completion authority.
- CP2 operational state, lease owner, fence, retry time, and attempt history remain recovery authority.
- `DistillationWindow`, chunks, and stages are immutable or monotonic domain progress beneath one root work; they never mirror broker state.
- `ProviderCallRecord` remains append-only provider-call audit and cost history.
- `DistillationStage.accepted_provider_call` selects the one call whose strictly parsed output became durable stage output.
- `DistillationObservationCoverage` and `MemoryCandidateSource` are the authoritative CP3 coverage/provenance relations.
- `MemoryCandidate.evidence` remains a compatibility summary and is not used to prove P5 after the relational cutover.
- Candidate semantic status and CP2 operational status remain separate.

### CP2 execution contract consumed

CP3 reads `WorkflowWork.execution_state` (`ready|leased|retry_wait|blocked|terminal_failure|settled`), `fencing_token`, lease/heartbeat/next-retry fields, failure streak, and blocked configuration fingerprint, while `WorkflowRun` remains immutable attempt history. It calls only `claim_work() -> ClaimResult/WorkClaim`, `heartbeat_work()`, `lock_work_fence(claim, now)`, `finish_work_claim()`, `fail_work_claim()`, and `queue_work_attempt(work_id, now, origin)`; it never updates those fields directly.

## Additive Data Contract

All new scoped models live in `engram.core.models`, use UUID primary keys and `TimestampedModel`, and validate organization/project/team consistency in creation services and `clean()`. Cross-app policy/call FKs use string model references to avoid import cycles. Cross-row scope is rechecked in every worker query; a foreign row cannot satisfy a target relation.

### DistillationWindow

One row materializes the immutable CP1 generation for the CP3 cohort.

| Field | Type | Contract |
|---|---|---|
| `organization` | FK `Organization`, CASCADE | Required scope |
| `project` | FK `Project`, CASCADE | Same organization |
| `team` | nullable FK `Team`, PROTECT | Must equal root work/session team |
| `work` | one-to-one FK `WorkflowWork`, PROTECT | Required `session_distillation` root |
| `session` | FK `AgentSession`, PROTECT | Must equal root subject and scope |
| `contract_version` | positive small integer | Exactly `1` in this checkpoint |
| `lower_sequence_exclusive` | non-negative bigint | Copied from work snapshot; currently zero |
| `upper_sequence_inclusive` | positive bigint | Copied from work snapshot; greater than lower |
| `observation_count` | positive integer | Exact useful-row count in the prefix |
| `input_hash` | char(64) | Hash of the complete ordered window manifest |
| `chunk_char_budget` | positive integer | Frozen planner budget |
| `reduction_target` | positive integer | Frozen final candidate target |
| `chunk_contract_version` | positive small integer | Exactly `1` |

Constraints and indexes:

- one window per root work;
- unique `(organization, project, session, input_hash)`;
- `0 <= lower < upper` and `observation_count > 0`;
- lowercase SHA-256 checks on `input_hash`;
- index `(organization, project, session, upper_sequence_inclusive)`;
- all fields are immutable after insert.

Empty/lifecycle-only session work remains CP1 `no_op/no_input` and never gets a window. A useful root work with a window is permanently owned by the CP3 path; it may not fall back to legacy monolithic distillation.

### DistillationChunk

A chunk is an immutable, ordered subrange of one window.

| Field | Type | Contract |
|---|---|---|
| `organization`, `project`, `team` | scoped FKs | Must match window |
| `window` | FK `DistillationWindow`, PROTECT | Parent generation |
| `ordinal` | non-negative integer | Zero-based deterministic order |
| `first_sequence` | positive bigint | First useful sequence in manifest |
| `last_sequence` | positive bigint | Last useful sequence in manifest |
| `observation_count` | positive integer | Manifest length |
| `input_manifest` | JSON | Ordered id/sequence/content-digest entries |
| `input_hash` | char(64) | Canonical chunk-input hash |

Constraints and indexes:

- unique `(window, ordinal)` and `(window, input_hash)`;
- positive count, `first_sequence <= last_sequence`, lowercase hash;
- index `(organization, project, window, ordinal)`;
- manifest, bounds, hash, scope, and parent are immutable.

The manifest schema is `distillation_chunk_manifest.v1` and contains exactly:

```json
{
  "schema": "distillation_chunk_manifest.v1",
  "window_input_hash": "<sha256>",
  "ordinal": 0,
  "observations": [
    {
      "observation_id": "<uuid>",
      "session_sequence": 1,
      "content_digest": "<sha256>"
    }
  ]
}
```

### DistillationStage

A stage is one logical extraction or reduction target under one policy version. It is not product work and carries no lease of its own.

| Field | Type | Contract |
|---|---|---|
| `organization`, `project`, `team` | scoped FKs | Must match window |
| `window` | FK `DistillationWindow`, PROTECT | Root generation |
| `chunk` | nullable FK `DistillationChunk`, PROTECT | Required only for extraction |
| `stage_kind` | enum | `extract` or `reduce` |
| `level` | non-negative small integer | Zero for extract; reduction level otherwise |
| `ordinal` | non-negative integer | Deterministic position within kind/level |
| `target_key` | char(64) | Logical target hash excluding provider policy |
| `stage_key` | char(64) | Target plus exact policy-version identity |
| `input_hash` | char(64) | Exact prompt-domain input hash |
| `input_manifest` | JSON | Chunk ref or ordered prior-draft refs |
| `prompt_contract` | char(80) | `distill_extract.v1` or `distill_reduce.v1` |
| `policy` | FK `ModelPolicy`, PROTECT | Resolved same-scope policy row |
| `policy_version` | positive integer | Version captured in stage identity |
| `policy_role` | enum | `primary` or `fallback` |
| `status` | enum | `required` or `complete` |
| `attempt_count` | non-negative integer | Monotonic provider starts |
| `last_failure_class` | char(80), blank | Typed operational failure only |
| `last_failure_at` | nullable timestamp | Latest failed attempt time |
| `accepted_provider_call` | nullable one-to-one FK `ProviderCallRecord`, PROTECT | Set only on complete |
| `response_hash` | char(64), blank | SHA-256 of returned UTF-8 body |
| `response_size` | positive integer, nullable | Returned UTF-8 byte count |
| `output_snapshot` | nullable JSON | Strict normalized output only |
| `output_hash` | char(64), blank | Canonical normalized-output hash |
| `completed_at` | nullable timestamp | Set only on complete |

Constraints and indexes:

- unique scoped `stage_key`;
- unique `(window, stage_kind, level, ordinal, policy, policy_version, policy_role)`;
- partial unique `(window, target_key)` where `status=complete`, allowing many failed policy versions but exactly one accepted target result;
- extraction requires chunk, level zero, and matching chunk/window;
- reduction requires null chunk and positive level;
- `required` has no accepted call/output/completed time;
- `complete` has all accepted-call, response, output, and completion fields;
- hashes use lowercase SHA-256 form;
- stage identity/input/scope fields are immutable; only attempt/failure fields advance while required, and completion is one-way.

The provider request id is exactly `distill-stage:<stage_key>`. Repeated calls therefore remain measurable through existing `ProviderCallRecord` rows. A complete stage is replayed from `output_snapshot` without entering a gateway. An existing provider-call row alone never authorizes replay.

### DistillationObservationCoverage

One row records the complete-window disposition of one useful observation.

| Field | Type | Contract |
|---|---|---|
| `organization`, `project`, `team` | scoped FKs | Must match window/observation |
| `window` | FK `DistillationWindow`, PROTECT | Completed generation |
| `observation` | FK `Observation`, PROTECT | Exact manifest member |
| `session_sequence` | positive bigint | Must match persisted observation |
| `observation_digest` | char(64) | Must match chunk manifest |
| `outcome` | enum | `signal` or `no_signal` |
| `deciding_stage` | FK `DistillationStage`, PROTECT | Accepted stage establishing outcome |

Constraints:

- unique `(window, observation)` and `(window, session_sequence)`;
- lowercase digest and positive sequence;
- immutable after insert.

`signal` is valid only when at least one same-window `MemoryCandidateSource` links the observation. `no_signal` is valid only when none does. These cross-table cardinalities are enforced by the finalization service in one transaction and checked by P5; they are not represented by a mutable status field or inferred from candidate count.

### MemoryCandidateSource

This append-only relation is authoritative candidate provenance for CP3 and the handoff consumed by CP4/CP5.

| Field | Type | Contract |
|---|---|---|
| `organization`, `project`, `team` | scoped FKs | Must match all referenced rows |
| `candidate` | FK `MemoryCandidate`, PROTECT | Durable semantic proposal |
| `window` | FK `DistillationWindow`, PROTECT | Source generation |
| `observation` | FK `Observation`, PROTECT | Supporting source |
| `stage` | FK `DistillationStage`, PROTECT | Final accepted output lineage root |
| `anchors` | JSON | Redacted exact-anchor snapshot |
| `anchors_hash` | char(64) | Canonical anchor hash |

Constraints and indexes:

- unique `(candidate, window, observation)`;
- index `(organization, project, candidate)` and `(window, observation)`;
- scope/reference/hash fields are immutable;
- source rows are never deleted by candidate cleanup, retry, or replay.

Before appending a source to an existing candidate, finalization locks the candidate row. This is the CP4 phantom fence: promotion locks the same row before freezing `MemoryVersionSource`. Evidence appended after a terminal candidate decision never reopens terminal decision work. Until CP4 lands it remains durable here; CP4 adds typed `AttachPromotedCandidateSource` handling to attach it to the resulting memory/version and exact projection generation.

The anchor schema is `candidate_source_anchors.v1` and contains the source observation id, sequence, content digest, sorted unique file paths from `files_read/files_modified`, symbols, commands, error identifiers, and commit identifiers extracted deterministically from persisted redacted fields. The provider cannot add an anchor that is absent from the source. Reduction unions source relations and never regenerates anchor strings from model prose.

### Candidate Decision Work Extension

Extend `WorkflowWorkType` with `candidate_decision` and `WorkflowSubjectType` with `memory_candidate`. The pair uses blank `occurrence_key`, derives organization/project/team from the candidate, and is accepted by the existing full-fingerprint uniqueness constraint.

Add `MemoryCandidate.decision_work_contract_version` as a non-negative small integer with Python/database default `0`, a `0|1` check, and a scoped status/version index. CP3 sets version `1` only in the final transaction after exact sources, decision work, and its package-backed run exist; legacy version `0` is not auto-repaired.

Its exact immutable snapshot is:

```json
{
  "schema": "candidate_decision_input/v1",
  "candidate_id": "<uuid>",
  "candidate_content_hash": "<sha256>",
  "organization_id": "<uuid>",
  "project_id": "<uuid>",
  "team_id": "<uuid-or-null>",
  "evidence_manifest_hash": "<sha256>",
  "policy_version": 1
}
```

The evidence manifest is an ordered canonical list of `(window.input_hash, observation.session_sequence, observation_id, observation_digest, stage.stage_key, anchors_hash)`, sorted by those semantic fields. `policy_version` is the resolved candidate-decision policy contract version; `kind` is excluded because dispatch is not kind-specific. The builder locks the candidate, rechecks scope/content, hashes all current source rows, and creates one immutable work generation. New evidence produces a new manifest hash and a new work generation; it never mutates or reopens an older terminal generation. CP4/CP5 lock and revalidate candidate hash plus the exact manifest before semantic commit.

Register `engram.memory.process_candidate_decision_work_v1` with the id-only signature `(work_id, workflow_run_id=None)` and batch-queue routing. Before CP5, the live handler validates/claims the work and passes `ClassifiedWorkFailure(failure_class='configuration', code='candidate_decision_capability_unavailable', redacted_detail='candidate decision capability unavailable', configuration_fingerprint=execution_configuration_fingerprint(work))` to `fail_work_claim()` without semantic mutation. A changed fingerprint clears the block and `queue_work_attempt()` resumes that generation. CP5 replaces the handler with `DecideMemoryCandidate`; it must not delegate to legacy `CurateMemoryCandidate`, whose fail-open semantics are superseded.

## Canonical Hashes

All hashes use CP1 `canonical_json_bytes()` and lowercase SHA-256.

The window manifest is:

```json
{
  "schema": "distillation_window_manifest.v1",
  "work_id": "<uuid>",
  "work_input_fingerprint": "<sha256>",
  "lower_sequence_exclusive": 0,
  "upper_sequence_inclusive": 37,
  "observations": [
    {"observation_id": "<uuid>", "session_sequence": 1, "content_digest": "<sha256>"}
  ]
}
```

The stage target projection includes `work_id`, work fingerprint, window input hash, stage kind, level, ordinal, chunk ordinal when present, input hash, and prompt contract. `target_key` hashes that projection. `stage_key` hashes the target projection plus policy id, policy version, and policy role. Changing any snapshot, coordinate, prompt contract, input content, or policy version changes the stage key; retrying unchanged input does not.

Valid provider output is normalized to exact-key JSON before hashing. Raw prompts and raw response bodies are not persisted in stages, provider-call metadata, audit events, logs, or task payloads.

## Public Domain Interfaces

The implementation exposes focused dataclasses/functions rather than one large service surface:

```python
CandidateDecisionWorkInput(candidate_id: UUID, candidate_content_hash: str, organization_id: UUID, project_id: UUID, team_id: UUID | None, evidence_manifest_hash: str, policy_version: int)
CandidateDecisionWorkBuilder.expected_input(candidate_id: UUID) -> CandidateDecisionWorkInput
CandidateDecisionWorkBuilder.exact_work(value: CandidateDecisionWorkInput) -> WorkflowWork | None
materialize_distillation_window(work: WorkflowWork) -> DistillationWindow
next_distillation_stage(window: DistillationWindow) -> DistillationStage | None
execute_distillation_stage(stage: DistillationStage, claim: WorkClaim) -> StageExecutionResult
finalize_distillation_window(window: DistillationWindow, claim: WorkClaim) -> FinalizeResult
process_candidate_decision_work_v1(work_id: str, workflow_run_id: str | None = None) -> str
```

`StageExecutionResult` is one of `completed`, `retry`, `blocked`, or `continuation`; it is operational and never a candidate status.

## C3.1 Deterministic Window, Chunks, And Continuation

### Materialization

The first fenced execution:

1. loads root work by id and verifies scope, type, contract version, subject, snapshot schema, and recomputed CP1 fingerprint;
2. loads the session through work organization/project/team and verifies the snapshot session id;
3. reads only trusted non-lifecycle observations satisfying `lower < session_sequence <= upper`, ordered by `session_sequence`;
4. recomputes every CP1 observation content digest;
5. requires a non-empty exact prefix and builds the window manifest/hash;
6. freezes chunk budget in `8,000..120,000` characters and reduction target in `1..64` (default `12`) after validating configuration;
7. applies `chunk_contract_version=1` in a pure function;
8. inserts the window and every chunk in one short transaction under the root work fence, converging through unique constraints on a concurrent insert;
9. reloads and byte-compares a winning existing plan rather than accepting a hash or scope collision.

The v1 chunker renders each redacted observation block once, scans ascending sequence, greedily appends while `current_chars + (2 if nonempty else 0) + block_chars <= chunk_char_budget`, and otherwise starts the next ordinal; an oversized first block occupies one chunk after the existing deterministic truncation marker. Thus every manifest entry appears in exactly one chunk and no row is omitted. Full content digest and exact local anchors remain in provenance; byte-complete semantic extraction of an arbitrarily large single normalized observation is outside this checkpoint.

Late observations with sequence greater than the frozen upper bound are never read, hashed, prompted, or covered by generation N. Ending the session again creates CP1 generation N+1 with a different work fingerprint. Historical success for N cannot satisfy N+1.

### Bounded continuation

Replace `ENGRAM_DISTILL_MAX_CHUNKS` with `ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT`, default `8`, valid range `1..64`. It counts started extraction and reduction provider calls, not rows or total generations.

When the budget or lease-safe time margin is reached:

1. already accepted stages remain committed;
2. root work remains `required`;
3. under `lock_work_fence(claim, now)`, `finish_work_claim(completion='continue_required')` records a successful attempt only when its batch has a durable stage disposition;
4. the same transaction sets root execution state back to `ready` and calls `queue_work_attempt(work.id, now, origin='reconciliation')` for one new run/signal; the finished run/token is never reused;
5. if the process dies before that signal, CP2 reconciliation sees required, eligible work and restores the signal from domain state.

No continuation cursor is trusted. The next target is derived by deterministic query: first missing extraction chunk ordinal, then missing reduction `(level, ordinal)`, then finalization. This makes concurrent schedulers and duplicate delivery converge without a mutable tail pointer.

## C3.2 Provider Stage Identity, Strict Output, And Fallback

### Extraction contract

`distill_extract.v1` accepts one chunk manifest and returns an object with exactly `memories` and `no_signal_observation_ids`.

Each memory has exactly `title`, `body`, `confidence`, `supporting_observation_ids`, and optional `kind`. Validation requires:

- non-empty title of at most 255 characters, body of at most 3,000 characters, and at most 12 memories per extraction;
- numeric confidence in `[0, 1]` without clamping or fallback coercion;
- known non-digest kind or an omitted kind;
- non-empty, duplicate-free supporting ids, all from the chunk;
- duplicate-free no-signal ids, all from the chunk;
- no observation in both a supporting set and the no-signal set;
- the union of all supporting ids and no-signal ids equals the chunk manifest;
- unknown keys, unknown ids, missing coverage, non-object items, fences around non-JSON, and invalid types fail the stage.

One observation may support more than one draft. An empty `memories` array is valid only when every chunk observation is explicitly no-signal. The legacy `parse_synthesized_candidates()` remains available to legacy observation processing but is never called by the CP3 session path.

### Call and commit order

For one required stage:

1. verify root lease/fence and stage scope;
2. resolve the current same-scope policy, capture id/version, and create/reuse its stable stage row;
3. atomically increment `attempt_count`, then leave the transaction;
4. call the provider with `distill-stage:<stage_key>` outside all write locks;
5. hash the returned body before strict parsing;
6. on valid output, enter a short transaction, lock the target stage and root work, recheck the CP2 fence, and complete exactly one target result;
7. if another accepted result won, retain the new provider-call history but return the existing normalized stage output;
8. on malformed output, store only typed failure, response hash/size, and call id diagnostics; keep the target incomplete and create no semantic rows.

A crash after provider response but before stage commit leaves no accepted output. Replay makes a fresh call with the same request id, as required by the provider replay-echo security fix. Exactly one normalized stage can win, while duplicate external calls and cost remain visible.

### Safe fallback

Primary selection preserves the existing `curation`, then explicit same-scope structured-`generation`, compatibility order; whichever resolves is recorded as primary, while absence of both is configuration failure. At most one secondary fallback is attempted per stage execution, only when the captured primary enables fallback, the fallback is a distinct same-scope active policy, and the primary outcome is:

- timeout or connection failure;
- HTTP 429 or 5xx;
- a strictly classified malformed structured response.

Scope, authorization, disabled-secret, missing-policy, and deterministic 4xx configuration errors do not fall through to another provider. Each fallback policy/version has its own `stage_key` and provider request id but shares the same `target_key`. An accepted fallback result records `policy_role=fallback`. If both attempts fail, CP2 records retry-wait or capability-block state; the root work and stage remain semantically incomplete.

Policy mutation before call causes the old required stage to be abandoned as operational history and a new stage identity to be created from the new policy version. A completed target never changes when policy changes later.

## C3.3 Full Reduction, Coverage, And Atomic Candidate Work

### Reduction planner

Reduction begins only after every chunk has one accepted extraction target. No candidate or coverage row is written from a partial window.

Leaf drafts receive stable ids derived from accepted extraction target key, normalized output hash, and output index. No-signal observations leave the reduction graph immediately but remain in the final coverage plan.

When signal drafts span more than one chunk, a pure planner groups ordered draft refs into the largest deterministic batches that fit the frozen prompt budget. `distill_reduce.v1` output must:

- contain only strict memory objects with `source_ids`;
- reference only input draft ids;
- cover every input draft at least once;
- preserve the union of supporting observation ids and local anchors;
- contain no empty source list;
- produce no more than `max(reduction_target, ceil(input_count / 2))` outputs when the input exceeds the target.

The shrink bound guarantees convergence. Odd singleton carry rows advance to the next level without a provider call. Levels repeat until the ordered output set is at or below `reduction_target`; a one-chunk set already at target is the final set. Every reduction stage has the same stable identity, strict parsing, fallback, replay, and continuation behavior as extraction.

Provider failure, over-budget input, a non-shrinking result, missing source id, or malformed response never falls back to an untracked union and never marks the window complete. Output size limits ensure at least two normalized drafts fit a reduction batch; violating a limit is malformed output.

### Final transaction

After all stages are complete, finalization constructs a complete plan in memory, then enters one database transaction:

1. lock the root work and verify its CP2 fence and required disposition;
2. lock the window and reverify all chunk/stage hashes and complete target cardinalities;
3. prove every manifest observation is exactly signal or no-signal and no observation outside the window appears;
4. derive candidate content hashes from normalized title/body under the existing session candidate identity contract;
5. lock existing candidates in sorted UUID order; create missing candidates under the project content-hash constraint;
6. append `MemoryCandidateSource` rows and compatibility evidence summaries;
7. create every `DistillationObservationCoverage` row;
8. after all source appends, build the candidate's ordered evidence manifest and create/reuse its exact `candidate_decision_input/v1` work generation, including for new evidence on a terminal candidate;
9. only for newly created required decision work, call `queue_work_attempt(work_id=decision_work.id, now=now, origin=WorkflowRunOrigin.AUTOMATIC)`, which emits `process_candidate_decision_work_v1` through the package-backed boundary;
10. call `finish_work_claim(completion='product_succeeded')` when at least one signal exists or `finish_work_claim(completion='product_no_signal')` otherwise;
11. set/verify candidate `decision_work_contract_version=1` and prove the root disposition/execution state and run outcome agree;
12. commit all final semantic/domain writes and package rows together.

Any exception, package creation error, stale fence, scope mismatch, or injected fault rolls back steps 5 through 11. Accepted provider stages remain durable, so replay repeats only finalization. Package tables are never read to decide whether work is complete.

For a reused promoted/rejected candidate, finalization appends source provenance and may create a new manifest-bound work generation, but never mutates or reopens the prior terminal generation. CP5 recognizes the terminal semantic state and hands the new provenance obligation to CP4 attachment processing; before CP4, the source row preserves it without pretending the memory version already contains it.

## Idempotency And Concurrency Matrix

| Race or replay | Required convergence |
|---|---|
| Two materializers | One byte-identical window/chunk plan |
| Duplicate task before provider call | CP2 claim grants one active fence |
| Worker dies during provider call | Root remains required; lease expires |
| Two external calls for one stage | Multiple call records, one accepted target |
| Stale worker returns after reclaim | Fence rejects stage/final commit |
| Policy changes before success | New stage key; old failure remains history |
| Crash after stage commit | Replay skips accepted stage |
| Crash between reduction levels | Replay derives first missing level/ordinal |
| Two finalizers | Root/candidate locks and uniqueness yield one final effect |
| Candidate content-hash race | One candidate; loser reloads and verifies scope/content |
| Decision-work race | One work per exact manifest generation and one initial package signal |
| Generation N and N+1 overlap | Separate windows/coverage; N cannot complete N+1 |
| Late evidence for terminal candidate | Append source; create new generation; never reopen prior work |

Lock order is root `WorkflowWork`, window, existing candidates sorted by UUID, then candidate source/coverage/decision-work rows. CP4 promotion also locks the candidate before reading sources. No provider or embedding call occurs while a database transaction or row lock is held.

## Operational Failure Classes And Metrics

CP3 uses CP2 typed failures, not string matching:

- `provider_transient` with CP2 codes for timeout, connection, 429, and 5xx, plus CP3 code `provider_output_malformed` after strict extraction/reduction failure;
- `configuration` for missing/disabled policy or secret and pre-CP5 code `candidate_decision_capability_unavailable` with a non-secret configuration fingerprint;
- `invalid_input` for deterministic fingerprint, scope, manifest, digest, or provider-request mismatch;
- `continue_required` as a successful `WorkClaimCompletion`, never a failure class;
- `unexpected` only for an untranslated exception, never for known malformed output.

Required aggregate metrics, scoped by organization/project without content:

- incomplete-window count and oldest age;
- pending/complete extraction and reduction stages;
- covered/expected observation count;
- continuation count and attempts per window;
- provider calls per `stage_key`, duplicate-call count, fallback count, and cost;
- malformed-output count by provider/model/prompt contract;
- candidate-decision work created/reused/blocked;
- finalization fence conflicts and rollback count.

Logs and audit metadata contain ids, hashes, counts, policy/provider/model names, typed reasons, and timing only. They never contain prompts, model response bodies, observation bodies, candidate bodies, commands, errors, secrets, or anchor values.

## Invariant Evolution

### P3 latest generation

For the CP3 cohort, derive the latest useful upper sequence for each ended session and locate the exact matching session work. A violation is any latest work that is absent, required/blocked, lacks its window, or has incomplete stages/finalization. An older completed upper never masks it.

Stable reasons are `latest_distillation_window_complete`, `latest_distillation_window_incomplete`, and `legacy_distillation_window_unobservable`. Legacy cohorts keep the project result `missing_observability` when there is no exact violation; they do not become healthy because their proxy count is zero.

### P5 observation coverage

For every completed CP3 window, compare its manifest to coverage and source relations. Violations include missing/extra/duplicate coverage, digest/sequence mismatch, signal without candidate source, no-signal with a candidate source, foreign-scope references, or a root completed before all stages.

Stable reasons are `completed_window_observations_disposed`, `completed_window_coverage_invalid`, and `legacy_observation_coverage_unobservable`.

### P6 decision work

CP3 makes the candidate-to-work relation exact: each current source-manifest generation has same-scope `candidate_decision` work with matching candidate/scope/content/policy/manifest fields and a CP2 active, blocked, or terminal state. Canonical conflict-only classification and autonomous terminal convergence remain CP5, so global P6 is not claimed healthy in CP3.

P14 has one focused negative control at the worker/provider boundary and one at final candidate/source creation; it remains globally missing observability.

## Serial Implementation Spine And File Ownership

### C3.2 prerequisite: provider schema and stage-role coordinate

One focused prerequisite owner edits:

- `apps/backend/engram/model_policy/services.py` and focused gateway tests to
  add the strict `distill_extract.v1` response kind without provider replay;
- `apps/backend/engram/core/models.py`, new forward migration
  `0037_distillation_stage_policy_role_coord.py`, and focused model/migration
  tests to bind coordinate uniqueness to `policy_role`;
- the focused prerequisite specification; its local implementation plan stays
  under the repository-ignored `docs/superpowers/plans` path.

This prerequisite is reviewed and merged before the C3.2 provider-stage owner
resumes. It does not edit stage execution, parsing, reduction, or finalization.

### C3.1 schema, deterministic planner, and continuation

One schema/planner owner edits:

- `apps/backend/engram/core/models.py`;
- `apps/backend/engram/core/migrations/0036_distillation_coverage.py`, depending on `0035_workflow_work_execution.py`;
- `apps/backend/engram/core/migrations_tests.py` and focused model tests;
- new `apps/backend/engram/memory/distillation_window.py`;
- new `apps/backend/engram/memory/distillation_window_tests.py`;
- CP2 session-work reconciler integration and its focused tests.

It does not edit provider parsing, reduction, candidates, or central `distillation.py` execution.

### C3.2 provider-stage identity and strict output

One provider-stage owner edits:

- new `apps/backend/engram/memory/distillation_provider_stage.py`;
- new `apps/backend/engram/memory/distillation_provider_stage_tests.py`;
- `apps/backend/engram/memory/candidate_parsing.py` only to expose reusable non-fallback primitives; legacy parser behavior remains isolated;
- `apps/backend/engram/memory/candidate_parsing_tests.py`;
- provider fault fixtures, without changing gateway replay semantics.

After the prerequisite slice, `apps/backend/engram/model_policy/services.py` is
changed only if a typed error field required by the accepted CP2 failure
contract is absent. No provider-body retention or gateway-level response replay
is added.

### C3.3 reduction, provenance, candidate work, and integration

One central integration owner edits:

- `apps/backend/engram/memory/distillation.py`;
- new `apps/backend/engram/memory/distillation_reduction.py`;
- new `apps/backend/engram/memory/distillation_provenance.py`;
- new `apps/backend/engram/memory/distillation_reduction_tests.py`;
- `apps/backend/engram/memory/distillation_tests.py`, deleting obsolete permanent-truncation/malformed-candidate expectations;
- `apps/backend/engram/memory/workflow_work.py` and its tests for candidate work;
- new `apps/backend/engram/memory/candidate_decision_work.py` implementing the CP2 builder protocol;
- `apps/backend/engram/memory/candidate_work_reconciler.py` and focused builder-generation tests;
- `apps/backend/engram/memory/tasks.py` and `tasks_tests.py`;
- `apps/backend/engram/celeryconfig.py` and routing tests;
- `apps/backend/engram/memory/invariant_queries.py` and tests;
- `docs/reliability/memory-loop-invariants.md` and fault-matrix evidence status;
- new `scripts/e2e_distillation_coverage.py` and its isolated tests.

The central integration owner is the only writer to `distillation.py`. Schema, provider-stage, and reduction owners hand off reviewed interfaces before that file changes. No two packages edit the same shared file concurrently.

## Required RED And Fault Tests

### C3.1 RED

1. `test_window_materialization_uses_exact_scoped_sequence_prefix` proves gaps, lifecycle exclusion, scope, and late-observation exclusion.
2. `test_window_and_chunk_hashes_are_stable_across_query_order_and_replay` compares byte-identical manifests.
3. `test_concurrent_window_materialization_converges_on_one_plan` exercises the uniqueness winner/reload path.
4. `test_success_for_generation_n_does_not_cover_failed_generation_n_plus_1` implements F10.
5. `test_max_calls_per_attempt_continues_same_work_without_tail_loss` uses more chunks than one attempt and asserts repeated same-work delivery.
6. `test_invalid_scope_or_content_digest_fails_before_provider_call` proves the worker trust boundary.

### C3.2 RED

1. `test_stage_key_binds_work_snapshot_kind_chunk_input_and_policy_version`.
2. `test_completed_stage_replay_uses_normalized_output_without_provider_call`.
3. `test_crash_after_provider_response_replays_to_one_durable_decision` implements F11: two call records may exist, one stage target wins, and no duplicate final candidate is possible.
4. `test_malformed_extraction_never_creates_candidate_or_coverage` covers invalid JSON, missing keys, invalid confidence, unknown ids, and incomplete observation coverage.
5. `test_malformed_primary_uses_one_safe_fallback_with_distinct_stage_key`.
6. `test_timeout_429_and_5xx_retry_but_scope_and_configuration_fail_closed`.
7. `test_stale_fence_cannot_commit_returned_provider_output`.
8. `test_worker_rejects_cross_scope_subject_before_provider_call` provides the allocated P14 negative control.
9. `test_stage_audit_retains_hashes_not_prompt_or_response_content`.

### C3.3 RED

1. `test_reduction_waits_for_complete_extraction_coverage`.
2. `test_multilevel_reduction_covers_every_leaf_and_preserves_anchor_union`.
3. `test_nonshrinking_or_incomplete_reduction_is_retryable_not_union_fallback`.
4. `test_partial_oversized_session_resumes_uncovered_chunks` implements F12 with 101 observations, a fault after the first chunk, multiple continuations, and complete non-overlapping final coverage.
5. `test_candidate_source_append_locks_candidate_before_terminal_handoff`.
6. `test_candidate_decision_snapshot_freezes_scope_policy_and_ordered_evidence_manifest`.
7. `test_candidate_and_decision_work_signal_commit_or_roll_back_together` injects faults after candidate, source, coverage, work, package, and root completion writes.
8. `test_orphan_candidate_gets_decision_work_and_terminal_disposition` implements the F13 structural half here: CP2 reconciliation restores one blocked decision work/signal with no ordinary semantic mutation; CP5 extends the same test through automatic semantic terminal disposition.
9. `test_replaying_finalization_creates_one_candidate_work_and_signal`.
10. `test_terminal_candidate_late_source_creates_new_generation_without_reopening_prior_work`.
11. `test_completed_window_p5_query_rejects_each_coverage_anomaly`.

### Container large-session E2E

The E2E creates one ended session with 101 useful observations, forces one observation per chunk and two provider calls per attempt, kills a worker after an accepted stage, restores provider availability after a retryable outage, and runs CP2 reconciliation until quiescent. It asserts:

- one root work and one window;
- 101 manifest memberships with no duplicate or gap;
- more than one attempt and continuation package history;
- all extraction/reduction targets complete;
- exactly 101 valid coverage rows;
- one durable source relation per signaled observation or explicit no-signal;
- no `SessionDistillationTruncated` audit;
- one candidate-decision work per candidate content identity;
- P3/P5 healthy for the isolated CP3 cohort.

## Rollout

1. Apply the additive schema with no writer enabled.
2. Materialize and compare deterministic plans in shadow mode for newly created session work only; make no provider calls or semantic writes in shadow.
3. Enable strict stages for a bounded fresh-session canary. A created window is a durable ownership marker and cannot run through legacy distillation.
4. Compare expected/covered counts, duplicate calls, malformed rate, provider cost, continuation count, and P3/P5 before widening.
5. Enable finalization only after candidate decision task registration and CP2 capability-block behavior are live.
6. Keep historical work/reporting untouched until CP4 transitions and CP5 decisions are canaried; historical repair remains CP10.
7. Remove the legacy truncation test and environment variable only when no legacy package can invoke the old monolithic task path.

## Rollback

- Before a window is created, disable the new writer and reverse the additive migration normally.
- After any window exists, do not deploy code that is unaware of the ownership marker. Stop new window creation, let compatible workers finish, or ship a forward fix while retaining schema and durable progress.
- Required/incomplete root work remains recoverable; do not mark it complete to make rollback quiet.
- Accepted stages, provider-call history, coverage, candidate sources, candidates, decision work, and package history are durable facts and are not deleted on behavior rollback.
- Never fall back from a CP3-owned window to legacy monolithic distillation.
- Never delete raw observations or reinterpret a terminal candidate/memory.

## Verification And CI Gates

All Python, backend, CLI, and E2E commands run inside Docker/Compose once the Compose runtime is available. Required evidence per serial slice:

```text
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && poetry run python manage.py migrate --noinput --settings=settings.test_settings && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings && poetry run python manage.py check --settings=settings.test_settings"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run pytest -q engram/memory/distillation_window_tests.py engram/memory/distillation_provider_stage_tests.py engram/memory/distillation_reduction_tests.py engram/memory/distillation_tests.py engram/memory/tasks_tests.py engram/memory/invariant_queries_tests.py"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run pytest -q"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run ruff check engram && poetry run ruff format --check engram"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run python /workspace/scripts/e2e_distillation_coverage.py"
```

Also require:

- migration apply, exact reverse, reapply, and fresh-database tests;
- `git diff --check` and repository-quality checks;
- focused security review for scope, redaction, prompt/response retention, and duplicate external billing visibility;
- Karpathy simplicity review proving no child-workflow engine leaked in;
- adversarial review against the exact committed slice range;
- CI names, URLs, conclusions, exact pass counts, rollback, and residual risks recorded before the next checkpoint gate.

## Dependencies

- CP1 sequence, immutable work snapshot, id-only task, and atomic initial signal contracts must be deployed and legacy package signatures drained.
- CP2 must provide `0035_workflow_work_execution.py`, the named claim/fence/failure/queue APIs, typed configuration fingerprints, and domain reconcilers.
- PostgreSQL partial uniqueness and transactional row locking are required.
- Existing provider gateways, strict JSON modes/tools, redaction, and `ProviderCallRecord` remain in force.
- CP4 consumes `MemoryCandidateSource` and owns post-terminal source attachment to `MemoryVersionSource`/projection work.
- CP5 consumes `candidate_decision_input/v1`, rejects stale manifest generations before mutation, and replaces the capability-block handler with evidence-aware automatic decisions.

## Acceptance Gate

Checkpoint 3 is accepted only when:

- no code path or test treats max-chunk truncation as terminal or supported;
- every CP3 window manifest entry is in exactly one chunk and receives final coverage before root completion;
- bounded attempts automatically continue the same work until complete;
- complete stages replay without a provider call and incomplete responses replay without duplicate durable effects;
- malformed output creates no candidate, memory, review item, or no-signal disposition;
- safe fallback is scoped, bounded, separately identified, and failure-safe;
- reduction covers every leaf and converges without silent union fallback;
- final candidate/source/coverage/decision-work/package/root writes are atomic;
- replay and concurrency leave one window plan, one accepted stage target, one candidate identity, one decision work identity, and one initial signal;
- new observations create a distinct newer root work/window;
- P3/P5 focused cohort tests and F10-F13 allocated tests pass;
- all container, migration, lint, full-suite, E2E, review, and CI gates are recorded green.

## Stop Conditions

Stop before implementation or the next serial slice if:

- the CP2 fence/continuation API is not committed or cannot atomically preserve required work plus a recoverable signal;
- CP1 snapshot reconstruction does not produce one exact scoped sequence prefix;
- deterministic chunking would omit an observation or depend on mutable order;
- a design requires provider calls under database locks;
- malformed output can reach candidate or review state;
- a reduction batch cannot prove full input-source coverage and convergence;
- finalization cannot atomically include candidate decision work and its package signal;
- a late candidate source can race promotion without the shared candidate lock;
- rollback would require deleting evidence, semantic history, or package rows;
- the implementation adds generic workflow dependency/cancellation semantics;
- any scope, hash, schema, public API, migration, security, or rollout decision differs materially from this contract.
