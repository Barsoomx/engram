# Checkpoint 0 Reliability Contract

Date: 2026-07-10

Status: accepted implementation design for C0.2

Roadmap gate: Checkpoint 0 — Campaign Preflight And Invariant Baseline

Branch: `chore/cp0-reliability-contract`

Base: `e4f68eeac2e571e7b1d8442bf61c54f06221070c`

## Goal

Turn the Autonomous Memory Loop Reliability Roadmap into a durable,
executable baseline without changing production behavior. The checkpoint must
make current gaps measurable, settle the boundary between product-domain
progress and package transport, and leave Checkpoint 1 with an exact contract
for lossless work creation.

Checkpoint 0 is successful when a future agent can answer, from committed
artifacts:

- which revision and production snapshot the campaign started from;
- which product invariants are currently measurable, violated, or
  unobservable;
- what must survive at each crash boundary;
- which state Engram may own and which state belongs only to
  `django-celery-outbox`;
- what the first Checkpoint 1 failing test must prove.

## Non-Goals

This checkpoint does not:

- change ingest, worker, curation, retrieval, context, or client behavior;
- add `WorkflowWork`, leases, fencing, retry scheduling, or repair commands;
- run production repair, candidate mutation, or historical backfill;
- expose invariant evaluation through an API, console screen, scheduler, or
  management command;
- copy production content, identifiers, tenant names, repository paths,
  provider errors, or secrets into fixtures or documentation;
- begin Memory CI, temporal gating, or performance work;
- perform a repository-wide security audit.

## Controller-Verified Start State

These facts were verified by the integration owner and remain pending tracked
transcription into
`docs/reliability/checkpoints/2026-07-10-cp0-preflight.md`. They are not yet a
committed CP0 evidence artifact.

- Local `master` and `origin/master` are
  `e4f68eeac2e571e7b1d8442bf61c54f06221070c`.
- The deployed backend and frontend image revision labels match that full SHA.
- `upstream` is `3fe0725a97e18b5edf3e61cde60e181ab2b6c997`.
- The native Codex harness is already merged on `master` through `855f4b3b`.
- The two governing roadmap documents are committed on this branch as
  `1e0e7517a4fea6983f98de524f3965d1a9d51055`.
- A bounded production inventory was captured read-only at
  `2026-07-10T01:30:43Z`; no repair or mutation was performed.
- The isolated baseline applied all migrations, reported no model drift,
  passed the Django system check, and passed 120 focused memory-loop tests.

## Design Alternatives

### Product Progress

1. Reinterpret `WorkflowRun` as stable logical work.
   This is rejected because current rows and console reruns represent attempts:
   a rerun creates another row, provider/result data belongs to one execution,
   and historical rows have no stable input generation.
2. Add one small `WorkflowWork` companion and retain `WorkflowRun` as
   attempt/history.
   This is selected because it is additive, preserves current API meaning, and
   gives later checkpoints one durable logical identity.
3. Derive required work only from raw evidence and package outbox rows.
   This is rejected because package rows describe delivery, not whether product
   work remains required, and raw evidence alone cannot record progress,
   attempts, leases, or an exact completed input generation.

### Invariant Baseline

1. Add a public health API or management command.
   Rejected: CP0 needs characterization, not a new operational surface.
2. Add one small internal, scoped, read-only evaluator module with direct
   pytest coverage.
   Selected: it gives later checkpoints executable evidence without creating a
   framework or public contract.
3. Document raw SQL only.
   Rejected: prose queries drift and are difficult to validate against model
   changes.

## Authority Decision

`django-celery-outbox` remains the sole transport authority. It owns:

- creation and storage of Celery delivery envelopes;
- publication to the broker;
- transport retry and dead-letter state;
- relay selection, locking, and commands.

Engram owns product-domain progress. The Checkpoint 1 design introduces one
additive `WorkflowWork` concept that owns:

- organization/project-scoped logical work identity;
- work type and immutable subject/input fingerprint;
- the input snapshot or generation the work is required to cover;
- required, complete, or explicit no-op product disposition;
- reconciliation eligibility;
- later, only in Checkpoint 2, bounded lease and fencing state.

Team is stored on logical work for authorization and observability, but it is
derived from the typed subject and does not create a second identity for the
same subject/input. A collision with a different derived team is a scope
violation, not another work item.

The existing `WorkflowRun` remains append-only attempt/history. It currently
stores one attempt's timestamps, free-form failure reason, provider-call ids,
result memory, and rerun lineage. Checkpoint 2 later adds typed failure
classification; CP0 does not claim it already exists.

Target attempt ownership includes:

- one execution attempt's start and finish;
- failure classification and provider-call provenance;
- result references and manual rerun lineage.

Checkpoint 1 may add a nullable link from `WorkflowRun` to `WorkflowWork`.
Existing rows remain valid and may stay unlinked until the historical repair
checkpoint. No CP1 migration may reinterpret or delete historical run rows.

`WorkflowWork` must never mirror broker message status, relay package rows,
publish messages itself, or treat an empty package queue as completion.
Reconciliation begins from scoped domain state and emits only a stable
`WorkflowWork.id` through the package-backed task boundary. An explicit manual
rerun may additionally carry the stable queued `WorkflowRun.id`; the worker
must reload it and verify that it belongs to the same scoped logical work.

Candidate status and memory validity are separate semantic state machines. A
provider outage changes operational progress; it cannot reject a candidate or
refute a memory.

## Current Contract Violation

Tracked backend contracts already require domain writes and package-backed
Celery enqueue to occur in one database transaction. Live hook ingest instead
registers `.delay()` through `transaction.on_commit()`, and its current
characterization test requires zero package rows inside the transaction. A
process death after database commit and before the callback can therefore keep
accepted evidence while losing its only delivery signal.

CP0 must label this behavior as the current violation in the tracked backend
contract and fault matrix. CP1 changes the test and implementation so evidence,
logical work, and the package row commit or roll back together. Documentation
agreement alone is not evidence that current runtime satisfies the contract.

## Required-Work Policy For Checkpoint 1

The focused Checkpoint 1 spec must preserve these decisions:

- Every newly acknowledged hook event has one raw envelope and one normalized
  observation or an explicit normalized no-op disposition.
- When realtime candidate generation is enabled, each non-lifecycle
  observation requires one observation-processing logical work item. The
  worker, not ingest, records low-content or no-signal completion.
- Lifecycle-only observations do not require per-observation generation work.
- Every transition of a session to ended creates or reuses a session
  distillation work generation. A session with no non-lifecycle observations
  records an explicit terminal no-input disposition and emits no transport
  signal.
- When realtime generation is disabled, non-lifecycle observations remain
  durable inputs for the session-distillation generation; no per-observation
  task is fabricated.
- Current ingest reactivates an ended session when later activity arrives.
  Preserve that lifecycle: the late observation joins the reactivated session,
  and its next explicit or idle end creates a newer input generation. A
  historical success cannot satisfy that later generation.
- Duplicate ingest reuses existing evidence and ensures any originally
  required logical work and delivery signal exist. It does not return early
  while work is missing.
- Idle-session sweep, explicit session end, console rerun, and scheduled digest
  producers eventually use the same logical-work creation primitive, in the
  serial order C1.1, C1.2, then C1.3. Console rerun creates a new explicit
  `WorkflowRun` attempt linked to the same completed `WorkflowWork`; it never
  fabricates a new input generation merely to bypass completion.

The policy inputs that affect whether work was required must be snapshotted
with the accepted evidence or logical work. A later settings change must not
silently reinterpret a duplicate event. For legacy evidence without a policy
snapshot, duplicate repair uses the current scoped organization setting once,
persists `legacy_policy_fallback=true` in the new work snapshot, and never
claims this was the original policy. Session work is derived from current
session state and watermark. Bulk legacy repair remains deferred.

## Logical Identity And Session Generation

Checkpoint 1 uses an organization/project-scoped uniqueness contract. A logical
identity includes:

- work type;
- typed subject identity;
- work contract version;
- immutable input fingerprint.

Observation work fingerprints the observation identity and immutable content
hash. Session work fingerprints a deterministic input watermark that totally
orders accepted observations for that session. The focused CP1 spec must choose
one additive representation that satisfies all of these conditions:

- concurrent appends and session ending are serialized on the session domain
  row;
- the watermark is derived from server-persisted observation identity, never a
  client clock alone;
- the exact covered input can be reconstructed without storing raw content in
  a task payload;
- a late accepted observation produces a distinct newer generation;
- duplicate requests for the same generation converge on one row;
- Checkpoint 3 can extend the same generation into deterministic chunks without
  rewriting its identity.

Automatic work sends `WorkflowWork.id` to Celery. An explicit rerun may also
send its queued `WorkflowRun.id`; both are stable domain ids. The worker reloads
subject, tenant, project, derived team, input snapshot, policy state, and rerun
linkage from PostgreSQL after resolving the scoped logical-work row.

## Invariant Evaluator Contract

Create `engram.memory.invariant_queries` as an internal read-only module. It
accepts mandatory `organization_id` and `project_id`; project resolution must
fail closed when the pair does not match. It must not support an implicit
global scope.

The module returns one immutable result per P1–P15 with:

- invariant id;
- state: `healthy`, `violated`, or `missing_observability`;
- a stable machine-readable reason code;
- an exact scoped aggregate violation count when the schema can answer it;
- a proxy count when current rows are diagnostic but cannot prove the
  invariant;
- at most 20 scoped sample ids, ordered deterministically by entity id;
- missing-evidence text and the checkpoint that supplies it.

Each evaluator must begin from the resolved organization/project scope. It may
use aggregate counts and bounded samples, but it may not load memory bodies,
raw payloads, provider prompts, or cross-tenant rows. CP0 must not claim an
invariant healthy when only a proxy can be measured; such a result is
`missing_observability` with the proxy count recorded as diagnostic data.

CP0 freezes these predicates so an implementation cannot report a false
healthy result:

| ID | CP0 predicate or proxy | State and count meaning | Sample entity | Missing evidence and owner |
|---|---|---|---|---|
| P1 | Scoped raw envelopes whose total `ObservationSource.raw_event` link count is not exactly one, or whose same-scope link to a same-scope observation count is not exactly one | `healthy` at zero; otherwise `violated`; violation count is raw envelopes without exactly one valid normalized disposition | raw event | Explicit non-observation disposition is added in CP1 |
| P2 | No logical-work relation exists | Always `missing_observability`; no violation count | none | `WorkflowWork`, CP1 |
| P3 | Proxy: ended sessions with at least one non-lifecycle observation and no historical successful distillation run | Always `missing_observability`; `proxy_count` is diagnostic only | session | Exact input watermark and coverage, CP2/CP3 |
| P4 | Proxy: `RUNNING` workflow attempts where `Coalesce(started_at, created_at) < as_of - 30 minutes` | Always `missing_observability`; proxy count is diagnostic only | workflow run | Lease expiry and fencing, CP2 |
| P5 | No observation coverage ledger exists | Always `missing_observability` | none | Coverage ledger, CP3 |
| P6 | Proxy: all proposed candidates | Always `missing_observability`; proxy count is diagnostic only | candidate | Candidate-to-decision-work relation and conflict classifier, CP2/CP3/CP5 |
| P7 | Promoted candidate without memory; any scoped memory's current version missing; memory/current-version body mismatch; current version without a consistent retrieval document; memory/document scope or stale-refuted mismatch | `violated` when any coherence anomaly exists; otherwise `missing_observability`, because uniform provenance and transition-audit identity do not exist; violation count is the sum of four guarded anomaly relations | `candidate:<id>` or `memory:<id>` | Uniform provenance and transition audit, CP4 |
| P8 | No uniform atomic lineage-transition identity exists | Always `missing_observability` | none | Atomic lineage transition, CP4 |
| P9 | Static conflict links exist, but survival across cleanup/restarts is not provable | Always `missing_observability` | none | Conflict durability tests, CP4/CP5 |
| P10 | No request fingerprint and immutable rendered snapshot contract exists | Always `missing_observability` | none | Immutable replay and strict budget, CP6 |
| P11 | No temporal eligibility state exists | Always `missing_observability` | none | Temporal validation, CP8 |
| P12 | Ordinary proposed candidates without a scoped conflict link, plus reviewable memories that are low-confidence/refuted but not `status=conflict` | `healthy` at zero; otherwise `violated`; count is non-conflict human-review items | `candidate:<id>` or `memory:<id>` | Genuine-conflict policy is completed in CP5 |
| P13 | No resumable repair identity exists | Always `missing_observability` | none | Scoped repair/reconciliation, CP2 and CP10 |
| P14 | Scope tests exist but no single runtime relation proves all source-to-sink paths | Always `missing_observability` | none | Per-boundary negative tests, beginning CP1 |
| P15 | No accepted-versus-impact-processed repository revision exists | Always `missing_observability` | none | Revision coverage, CP8 |

P1 uses `ObservationSource`, not `Observation.raw_event`: one normalized
observation may be reused while every accepted raw envelope still requires
exactly one source/disposition link. Both the total source-link cardinality and
same-scope valid-link cardinality must equal one, so duplicate or corrupt links
cannot produce a false healthy result. P12 mirrors the complete current review population:
proposed candidates plus `reviewable_memory_filter()`, while treating only
scoped links from a same-scope memory whose target is `candidate:<candidate_uuid>` and
`Memory.status=conflict` as genuine conflicts.

P7 evaluates every scoped `Memory`, not only approved or candidate-linked rows.
Its four relations are: promoted candidate without a same-scope memory, missing
declared current version, current-version body mismatch, and missing or
inconsistent current retrieval document. The last two are evaluated only when
the declared current version exists. Violation count sums relation counts; one
memory may therefore contribute to more than one relation, while sample ids are
deduplicated. Document consistency compares organization, project, null-safe
team, visibility scope, stale, and refuted state without loading body text.

Samples are capped at 20 after deterministic ordering by UUID then prefix. P1
uses `raw_event`, P3 uses `session`, P4 uses `workflow_run`, P6 uses
`candidate`, and P7/P12 use `candidate` or `memory`. Caller-supplied `as_of`
must be timezone-aware; a naive value raises `ValueError`.

Reason codes, zero/nonzero state rules, proxy meanings, sample entity prefixes,
missing evidence, and owner checkpoints are part of the evaluator contract and
must be asserted in tests. `missing_observability` never becomes `healthy`
because its proxy count happens to be zero.

| ID/state | Exact reason code | Target checkpoint |
|---|---|---|
| P1 healthy | `scoped_raw_events_normalized` | CP1 |
| P1 violated | `raw_event_normalization_cardinality_invalid` | CP1 |
| P2 missing | `logical_work_intent_relation_missing` | CP1 |
| P3 missing | `latest_input_watermark_missing` | CP2/CP3 |
| P4 missing | `work_lease_and_reclaim_evidence_missing` | CP2 |
| P5 missing | `observation_coverage_relation_missing` | CP3 |
| P6 missing | `candidate_decision_work_relation_missing` | CP2/CP3/CP5 |
| P7 violated | `promotion_chain_inconsistent` | CP4 |
| P7 missing | `promotion_provenance_audit_relation_missing` | CP4 |
| P8 missing | `memory_transition_history_relation_missing` | CP4 |
| P9 missing | `durable_conflict_evidence_relation_missing` | CP4/CP5 |
| P10 missing | `replay_evidence_fields_missing` | CP6 |
| P11 missing | `temporal_eligibility_evidence_missing` | CP8 |
| P12 healthy | `human_inbox_conflicts_only` | CP5 |
| P12 violated | `non_conflict_item_in_human_inbox` | CP5 |
| P13 missing | `repair_run_relation_missing` | CP2/CP10 |
| P14 missing | `operation_scope_resolution_evidence_missing` | CP1+ |
| P15 missing | `repository_impact_coverage_relation_missing` | CP8 |

## Sanitized Fixture Contract

Create one synthetic JSON scenario manifest under
`engram/memory/fixtures/`. It contains no production-derived strings or ids and
covers exactly:

- `no_run_session`;
- `stale_running_work`;
- `latest_failure_after_prior_success`;
- `duplicate_delivery`;
- `orphan_candidate`;
- `partial_promotion`;
- `conflict`;
- `oversized_session`.

Every scenario declares its invariant ids, current expected characterizations,
and synthetic target plus foreign-tenant control. Each characterization carries
its own missing evidence and target checkpoint so multi-invariant cases cannot
collapse different owners. The JSON
manifest drives one parametrized database test: a small test-only builder for
each scenario materializes both scopes, runs the evaluator for the target
scope, and asserts every declared state, count/diagnostic, reason, and owner
checkpoint. The same test proves foreign-scope anomalies do not change the
target result. For an unobservable scenario, it asserts the exact
`missing_observability` reason and owner rather than merely validating JSON
shape.

The fixture is a characterization manifest, not Django production data and not
a repair instruction. CP0 tests prove that known bad states are detected; they
must not encode the future repair as current behavior.

## Fault Matrix Contract

Create a tracked fault matrix with the columns:

`ID | fault boundary | durable state before fault | current outcome | target outcome | invariant | owner checkpoint | executable evidence`

It covers scope denial, transaction rollback, post-commit callback loss,
broker/relay unavailability, duplicate ingest, idle sweep, worker death before
and after claim, provider outage, historical-success masking, provider success
before durable output, chunk truncation, orphan candidate decision work,
promotion/index split, embedding failure, split supersession, context replay,
and temporal revalidation.

CP0 adds only characterization/fixture evidence. Dynamic crash tests land with
the checkpoint that owns the recovery mechanism:

- atomic commit and delivery signal: CP1;
- claim, lease, retry, and reconciliation: CP2;
- provider stage, chunk coverage, and candidate decision work: CP3;
- promotion and lineage transitions: CP4;
- context snapshot/replay: CP6;
- temporal revalidation: CP8.

Negative foreign-tenant controls are required once per distinct evaluator or
source-to-sink trust boundary. They are not repeated mechanically in unrelated
worker-kill or provider-fault tests.

## Preflight Evidence Contract

Create a tracked CP0 preflight report containing:

- local, origin, deployment, branch, and upstream SHAs;
- exact commands and exit codes for repository and baseline verification;
- aggregate production inventory already captured by the integration owner;
- evidence quality per count: exact, proxy, or unobservable;
- the canonical snapshot hash
  `35267a66917d463a16efbcbf68cb18362c6f5b0857557855dde23ba0d2e9602b`;
- a statement that no production mutation or repair occurred.

The report includes aggregate integers only. It excludes tenant grouping,
identifiers, slugs, repository URLs, memory text, payloads, timestamps for
individual rows, failure strings, provider/model names, hostnames, DSNs,
credentials, and task arguments. An unobservable fact is written as
`UNOBSERVABLE`, never inferred as zero.

## Files And Ownership

The implementation plan divides C0.2 into non-overlapping tasks:

- contract owner: decision record and tracked backend-contract clarification;
- invariant owner: evaluator module, tests, and synthetic fixture manifest;
- reliability-doc owner: invariant catalog/observability map, fault matrix, and
  preflight report; `docs/reliability/memory-loop-invariants.md` maps current
  metrics/proxies and every named signal gap;
- CP1 spec owner: focused lossless-work-creation spec and executable plan;
- reviewers: independent spec/code quality, Karpathy simplicity, bounded scope
  review, and Claude adversarial review.

The integration owner alone performs branch operations and commits. No two
workers edit the same file. Production remains read-only.

The private ignored `goal.md` is updated in the control checkout to match this
authority decision. It is not force-added. The same durable contract is
committed through the decision record and `docs/backend-contracts.md`.

## Verification

Before closing C0.2:

- run the new invariant/fixture tests in the backend container;
- run focused Ruff check and format check on new Python files;
- run migration freshness and Django system checks in the container;
- run `git diff --check` and documentation link/placeholder checks;
- have an independent Codex reviewer verify spec compliance and code quality;
- have a separate Karpathy reviewer reject unnecessary abstraction;
- run `cc@sendbird` adversarial review against the committed branch diff with
  Opus/xhigh and focus restricted to CP0 reliability/scoping behavior;
- verify every Critical/Important finding as fixed or refuted before the gate;
- record commands, exit codes, commit SHAs, residual risks, and rollback.

## Acceptance Gate

C0.2 is complete only when:

- the roadmap, Memory CI proposal, this design, and the domain-progress
  decision are committed;
- local `goal.md`, the decision record, and tracked backend contract agree;
- the tracked backend contract and fault matrix explicitly record the current
  post-commit callback loss window rather than claiming runtime atomicity;
- P1–P15 each have a scoped evaluator or named missing-observability result;
- all eight synthetic baseline scenarios are materialized, evaluated, and
  matched against their declared target and foreign-scope expectations;
- every fault boundary has current/target behavior and an owner checkpoint;
- the production baseline is recorded without sensitive content or mutation;
- the focused CP1 spec freezes logical identity, required-work policy, session
  generation, migration/backfill, producer order, and first red tests;
- focused container verification and all three review gates are clean;
- no CP1 behavior or production repair has leaked into CP0.

## Stop Conditions

Stop before Checkpoint 1 if:

- the domain-progress authority remains ambiguous;
- required-work policy or session generation remains underspecified;
- a query requires implicit global scope, mutation, secrets, or unbounded
  content reads;
- synthetic fixtures require production content;
- the first CP1 migration cannot remain additive;
- a plan attempts to reinterpret existing `WorkflowRun` rows or duplicate
  package transport behavior;
- review discovers a material architecture, data, security, migration, public
  API, deployment, or release change not authorized by this design.
