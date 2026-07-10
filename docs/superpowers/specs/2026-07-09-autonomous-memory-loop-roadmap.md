# Autonomous Memory Loop Reliability Roadmap

Date: 2026-07-09

Status: implementation roadmap for supervised multi-agent execution

Scope: Engram memory capture, distillation, curation, promotion, retrieval,
context injection, recovery, and continuous validity

Companion proposal:
[Memory CI: Continuous Validation for Engineering Memory](2026-07-09-memory-ci-feature-proposal.md)

## What This Document Is

This is a high-level implementation plan, not the final schema, API contract,
or per-checkpoint technical specification.

It defines:

- the product and reliability decisions that later specs must preserve;
- the exact order in which checkpoints must be implemented and merged;
- which work may run in parallel inside each checkpoint;
- the acceptance invariants that determine whether a checkpoint is complete;
- how a sol-class or fable-class supervisor should divide, review, and
  integrate the work;
- when and how the existing production data may be repaired.

Every checkpoint that changes architecture, data shape, public behavior, or
deployment still requires its own focused spec before implementation. Those
specs may choose exact model fields and service boundaries, but they must not
weaken the invariants in this roadmap.

## Executive Decision

Engram should be built as a reliable compiler from immutable engineering
evidence into authorized, versioned, temporally valid context.

The implementation order is:

1. prove that accepted evidence always creates durable domain work;
2. make that work observable, leased, retryable, and self-repairing;
3. prove that every input is completely and idempotently processed;
4. make every semantic state transition atomic;
5. make curation autonomous, with only genuine conflicts reaching humans;
6. make context replay immutable and client capture complete;
7. add temporal validity and Memory CI so memory follows the codebase;
8. optimize retrieval and curation only after correctness is measurable;
9. repair historical data and roll out under shadow and canary gates.

This ordering is mandatory. Retry policy is not the first problem: retries
cannot recover work that was never durably represented. A healthy API, an
empty transport queue, and green tests are not proof that the memory loop is
making progress.

## Product Outcome

For every event that Engram accepts:

- the raw evidence remains durable and attributable;
- required downstream work is durably represented in the same commit;
- crashes, worker loss, broker loss, provider outages, and duplicate delivery
  cannot silently discard the work;
- the system can explain the current processing disposition of that evidence;
- useful knowledge is automatically promoted, revised, merged, or rejected;
- only an unresolved contradiction with material support becomes a human
  conflict;
- retrieval uses only authorized and temporally appropriate memory;
- context bundles are bounded, reproducible snapshots;
- changes to code, configuration, dependencies, and behavior automatically
  cause affected memories to be revalidated.

The normal operator experience should not include a daily proposed-memory
queue. Operators may see health and lag, but semantic intervention is reserved
for genuine conflicts.

## Reliability Boundary

The strongest unconditional guarantee begins when the Engram API acknowledges
an event as accepted.

Before server acknowledgement, a thin client cannot guarantee delivery while
also having no durable local queue. The default client remains thin and uses
bounded in-memory retry. A later opt-in encrypted, bounded, expiring client
spool may improve pre-acknowledgement delivery, but it is not an authoritative
memory store and is not required by this roadmap.

After acknowledgement:

- evidence durability is a safety property and must not depend on the broker,
  a worker, or a model provider;
- eventual semantic completion is a liveness property conditional on required
  dependencies eventually recovering;
- an indefinitely unavailable provider may cause indefinitely delayed work,
  but it must not cause silent rejection or evidence loss;
- individual attempts must end and release their lease even when the logical
  work remains scheduled for another attempt.

Engram therefore does not promise that every candidate reaches a semantic
terminal state in finite wall-clock time during an infinite external outage.
It does promise that no accepted evidence disappears, no work becomes
invisible, and processing resumes automatically after recovery.

## Product Decisions To Lock Before Coding

These are roadmap-level decisions. A checkpoint spec may refine their
implementation, but changing one requires an explicit decision record.

### Humans Review Conflicts Only

The human memory inbox contains only genuine semantic conflicts: two materially
supported, mutually incompatible claims that cannot be resolved safely from
current evidence.

The following are not human memory-review reasons:

- low confidence;
- provider outage;
- malformed provider output;
- missing embedding;
- duplicate or low-value content;
- sensitive-looking content;
- organization-wide scope;
- ordinary age or staleness;
- a retry limit being reached;
- a candidate the model calls uncertain.

Those cases are handled automatically:

- retry or use a configured fallback for operational failures;
- reject a derived candidate while retaining its source evidence when it is
  clearly low-value or unsupported;
- narrow or deny unsafe scope expansion by deterministic policy;
- redact or quarantine secret-shaped content automatically;
- mark validity unknown and revalidate rather than asking a person to curate
  routine uncertainty.

### Raw Evidence Is The Durable Truth

Raw event envelopes, normalized observations, source links, and provider-call
provenance are the durable evidence layer.

Candidates, memories, indexes, confidence, summaries, and context bundles are
derived artifacts. They may be rebuilt or re-evaluated without losing the
source record.

Automatic candidate rejection is therefore allowed when the candidate is a
bad projection. Rejection must preserve the evidence and the decision reason,
and new evidence or a new policy version may create a new candidate later.

### Delivery Is Not Domain Progress

`django-celery-outbox` remains the transport authority. Engram must not build a
second broker, relay, transport retry table, or dead-letter implementation.

Engram does need a durable domain-level record of what processing is required
and how far it has progressed. That record is not a transport queue. It lets
the system answer questions such as:

- was work required for this accepted event or ended session;
- which immutable input window is being processed;
- is an attempt currently leased;
- what durable outputs or explicit no-op disposition were produced;
- should a reconciler emit another id-only task through the package outbox.

Transport rows prove delivery intent. Domain progress proves product work.
Both are needed.

The current `goal.md` forbids Engram-owned transport status, attempts, locks,
polling, and relay behavior around the package outbox. Before implementation,
Checkpoint 0 must reconcile that authority in a committed decision record and
matching `goal.md` clarification. The decision must define the minimum
product-domain progress state, prove it does not duplicate package transport,
and prefer reusing existing workflow records before adding a new model.

### Operational And Semantic State Are Separate

Operational work moves through concepts such as ready, leased, retry-wait, and
complete.

Semantic candidates move through concepts such as proposed, promoted, merged,
rejected, and conflict.

Memory validity moves through concepts such as current, suspect, revalidating,
unknown, superseded, refuted, and conflict.

These concepts must not be collapsed into one status enum. A provider outage is
an operational retry, not a rejected memory. A stale code fact is a temporal
validity change, not a failed Celery task. A conflict is a semantic exception,
not a dead letter.

### Similarity Is A Shortlist, Never An Authority

Exact content identity may drive deterministic deduplication.

Semantic similarity may select memories for comparison. It must never, by
itself, authorize merge, supersede, refute, or scope expansion. Destructive
changes require contradiction-aware comparison against evidence and current
memory versions.

### Confidence Is Not Model Self-Confidence

One opaque model-supplied number is not enough to govern publication or
retrieval.

Later checkpoint specs should distinguish at least:

- evidence strength and provenance;
- corroboration or contradiction;
- temporal validity;
- extraction or judge uncertainty;
- retrieval relevance.

These signals may be summarized for UI purposes, but automatic policy must be
able to inspect the underlying reasons.

### Current Code State Is Part Of Memory Meaning

Semantic similarity cannot determine whether a fact is still true.

Engram must associate durable memories with evidence anchors and repository
state, then revalidate affected memories when those anchors change. Semantic
ranking happens only after authorization and temporal eligibility have been
applied.

The first Memory CI implementation targets the canonical project repository
and default branch. Branch overlays and arbitrary historical DAG reasoning are
later extensions.

## Explicit Non-Goals

This campaign does not:

- turn Engram into a generic agent framework or CI platform;
- make local clients authoritative queues or memory databases;
- require manual approval for normal memory publication;
- use wall-clock TTL as a substitute for evidence-based validity;
- bulk-promote the current candidate backlog;
- introduce a second outbox transport;
- optimize every retrieval query before correctness gates are green;
- implement branch-specific memory universes in the first Memory CI slice;
- block pull requests on model-only judgments in the initial rollout;
- perform unrelated repository-wide security work.

Security review remains focused on the touched trust boundary, especially
tenant and project scoping before work creation, retrieval, ranking, packing,
provider calls, and repair.

## Directional Supersession Of Existing Behavior

This roadmap intentionally changes parts of the current documented direction.
The implementation checkpoints must update the affected docs and tests as the
behavior changes.

| Existing direction | Roadmap direction |
|---|---|
| Low-confidence candidates wait for human review | Automatically decide publish, reject, or retry; only contradiction can enter the human inbox |
| Auto-review may return `skip` and leave a candidate proposed | No indefinite semantic skip state; operational retry is explicit and semantic uncertainty is automatically resolved or re-evaluated |
| Sensitive-term and organization-scope rules hold for a human | Deterministically redact, reject, or narrow scope; explicit authorized promotion is a separate action |
| Confidence decay funnels old memory to human review | Memory CI revalidates evidence and changes temporal eligibility automatically |
| Candidate TTL may reject any old low-confidence proposal | TTL cannot destroy conflicts or substitute for processing; derived candidates settle through the autonomous state machine |
| Missing embeddings allow promotion through an `embedding_unavailable` route | Missing required comparison capability is operationally retried or handled by a safe deterministic fallback; it cannot authorize destructive or blind publication |
| Very high similarity directly supersedes an older memory | Similarity only selects a comparison; evidence-aware merge or contradiction logic decides the transition |
| Session distillation may permanently truncate tail chunks | Large sessions continue in deterministic resumable work units until every input has a disposition |
| A repeated context request id replays whatever was stored first | Replay requires a matching request fingerprint and returns an immutable stored snapshot |

The existing auto-review and memory-intelligence specs remain useful historical
context. Where they conflict with this table, this roadmap is the newer
direction.

## Target Operating Model

    accepted event
        |
        +-- immutable raw evidence and normalized observation
        |
        +-- durable domain work intent
        |       |
        |       +-- package-owned outbox delivery signal
        |       |
        |       +-- leased, idempotent processing attempts
        |               |
        |               +-- explicit no-signal disposition
        |               +-- derived candidate with provenance
        |               +-- automatic retry after operational failure
        |
        +-- autonomous semantic transition
                |
                +-- reject projection, evidence retained
                +-- promote or revise atomically
                +-- deterministic duplicate merge
                +-- genuine contradiction conflict
                        |
                        +-- only human semantic inbox

    repository change
        |
        +-- impacted evidence anchors
                |
                +-- revalidation work
                        |
                        +-- current / revised / superseded / refuted
                        +-- unknown with automatic retry
                        +-- genuine contradiction conflict

    context request
        |
        +-- resolve authorization
        +-- apply temporal eligibility
        +-- exact and semantic ranking
        +-- strict budget packing
        +-- immutable cited snapshot

## Domain Progress Invariants

These invariants are the primary acceptance contract. Health endpoints, queue
depth, and test counts are supporting signals, not substitutes.

| ID | Invariant | Required evidence |
|---|---|---|
| P1 | Every acknowledged event has one durable raw envelope and a normalized disposition | Idempotent ingest tests and an invariant query |
| P2 | Every acknowledged event or lifecycle transition that requires async work has a durable logical work intent committed with it | Transaction-boundary fault test |
| P3 | Every ended session with useful observations has a complete distillation disposition for its latest input watermark | Reconciler query and large-session E2E |
| P4 | No logical work remains leased past its recovery window without being reclaimed | Lease-expiry fault test and metric |
| P5 | Every input observation in a completed distillation window is covered by a candidate, a promoted memory, or an explicit no-signal decision | Coverage ledger assertion |
| P6 | Every proposed candidate has active automatic decision work or is a genuine conflict | Invariant query; no ordinary orphan proposals |
| P7 | Every promoted memory has one coherent current version, retrieval representation, provenance set, and audit transition | Database consistency assertion |
| P8 | Every supersede, merge, refute, or conflict transition preserves both source history and current-state consistency | Concurrency and crash-boundary tests |
| P9 | Conflict evidence and links survive TTL, cleanup, retries, and worker restarts until explicit resolution | Conflict durability test |
| P10 | Every context replay is fingerprint-compatible, byte-stable, authorized, and within its declared budget | Replay and strict-budget tests |
| P11 | No temporally ineligible memory is injected as current knowledge | Memory CI context-gating E2E |
| P12 | The human review inbox contains only unresolved semantic conflicts | API/UI invariant and production query |
| P13 | Every repair operation is scoped, idempotent, resumable, and dry-run explainable | Repair replay tests and audit |
| P14 | All reads, work creation, provider calls, repair, and retrieval begin from resolved organization/project/team scope | Focused negative scope tests |
| P15 | A context request for repository state R cannot present code-sensitive memory as current until impact processing covers R | Revision-coverage lag test and context warning/withholding assertion |

The implementation may add stronger invariants. It must not replace these with
task counts such as “queue is empty.”

## Supervisor And Agent Operating Model

The supervising sol-class or fable-class model is the integration owner. It
owns the active branch, task graph, commits, pushes, PR state, and checkpoint
gate.

### Campaign Gates And Active PR Slices

The numbered checkpoints below are campaign gates. A gate may contain several
serial implementation slices when one PR would be too large to review.

Only one implementation slice may have mutable work at a time. That slice owns
one coherent branch/PR. Parallel agents may edit only non-overlapping zones
inside it.

Agents may research later gates or slices in parallel, but they remain
read-only until the active slice merges. This preserves the local
one-PR-per-slice rule and prevents stacked architecture from silently becoming
the default.

Checkpoints 7 and 8 use the sequential MCI slices in the companion proposal.
The same one-active-slice rule applies to them.

### Roles

For each active PR slice, the supervisor assigns:

- one spec owner;
- one schema/migration owner when data shape changes;
- one writer for each mutable module or file group;
- test writers whose files do not overlap implementer-owned files, or a single
  test-and-implementation owner for tightly coupled modules;
- independent read-only reviewers;
- one verification owner for container commands and evidence recording;
- one Karpathy-style simplicity reviewer.

Only the supervisor performs branch operations and commits unless it explicitly
delegates a single git-owner handoff.

### Task Card Required Fields

Every worker prompt must include:

- checkpoint and task ID;
- objective and non-goals;
- branch and base SHA;
- read/write permission;
- exact owned files or conceptual file zone;
- forbidden overlapping files;
- upstream task dependencies;
- required tests or evidence;
- stop conditions;
- required handoff format.

### Worker Handoff Format

Each worker returns:

1. task ID and status;
2. files read and files changed;
3. behavior added or findings made;
4. tests written first and their initial failure;
5. final commands and exit codes;
6. unresolved risks or assumptions;
7. whether public behavior, data shape, or deployment changed;
8. recommended reviewer focus.

### Integration Loop

For every active PR slice:

1. supervisor records live branch, base SHA, dirty state, and current PR;
2. spec owner writes the focused checkpoint spec;
3. independent plan reviewer checks scope and invariants;
4. schema owner lands the smallest additive foundation when required;
5. parallel file owners write narrow failing tests and implementation;
6. supervisor integrates and resolves cross-module contracts;
7. focused adversarial reviewers inspect reliability, data migration,
   tenant scoping, and backward compatibility as relevant;
8. findings are marked fixed, refuted, accepted risk, or deferred with owner;
9. container verification and fault tests run;
10. Karpathy reviewer removes unnecessary abstraction and confirms the change
    is the smallest design that satisfies the checkpoint;
11. supervisor records commands, exit codes, SHA, CI, metrics, and rollback;
12. draft PR is promoted and merged before the next checkpoint begins.

## Exact Checkpoint Order

The merge order is strict:

| Order | Checkpoint | Depends on | Primary outcome |
|---:|---|---|---|
| 0 | Campaign preflight and invariant baseline | None | Clean implementation base, refreshed evidence, approved reliability contract |
| 1 | Lossless work creation | 0 | Accepted evidence and required domain work become one commit |
| 2 | Leases, recovery, and invariant reconciliation | 1 | No invisible, orphaned, or permanently stuck logical work |
| 3 | Complete and idempotent distillation | 2 | Every observation receives a durable disposition; no permanent tail loss |
| 4 | Atomic memory transitions and rebuildable projections | 3 | Promotion, versioning, indexing metadata, links, and audit cannot split |
| 5 | Conflict-only autonomous curation | 4 | No daily proposal queue; similarity is non-destructive |
| 6 | Immutable context and complete client lifecycle | 5 | Bounded reproducible context and faithful capture/recall |
| 7 | Memory CI temporal foundation | 6 | Anchors, repository snapshots, impact selection, and shadow validity |
| 8 | Automatic temporal revalidation and context gating | 7 | Memory evolves with code; only temporal conflicts need humans |
| 9 | Retrieval and curation performance | 8 | Correct paths scale without debug/production divergence |
| 10 | Historical repair, canary expansion, and release gate | 9 | Existing data converges to the invariants and rollout is proven |

Checkpoints 1 through 5 are the core correctness path. Checkpoints 7 and 8 use
the companion Memory CI proposal: Checkpoint 7 contains MCI-0 through MCI-4,
and Checkpoint 8 contains MCI-5 then MCI-6. Each MCI slice is its own
branch/PR and merges in the exact order defined there. MCI-7A joins the
Checkpoint 10 rollout; optional MCI-7B advanced surfaces follow the campaign.
None of this work may be pulled forward before the core work and semantic
transitions are reliable.

## Checkpoint 0 — Campaign Preflight And Invariant Baseline

### Objective

Create a trustworthy start point and turn the product claims in this roadmap
into executable observations before changing behavior.

### Serial PR Spine

1. **C0.1 — branch convergence:** the git owner finishes, merges, or safely
   preserves the current harness slice and records the new master base;
2. **C0.2 — reliability contract:** one docs/test-fixture PR containing the
   domain-progress ADR, `goal.md` clarification, invariant catalog, fault
   matrix, baseline fixture, and Checkpoint 1 focused spec.

### Mandatory Serial Work

1. Merge, close, or explicitly hand off the active Codex harness checkpoint.
2. Start the memory-loop campaign from current `master` on a new coherent
   checkpoint branch.
3. Record live local, origin/master, upstream, and deployment SHAs.
4. Refresh a read-only production inventory.
5. Commit an ADR separating product-domain progress from package transport.
6. Update `goal.md` to authorize the chosen minimum domain-progress contract
   while preserving `django-celery-outbox` as the only transport.
7. Write the focused Checkpoint 1 spec and fault matrix.

Do not implement this campaign directly in the active harness feature branch,
even when that branch is clean. Reverify live state and start the first
implementation slice from then-current `master`.

### Parallel Packages

After the base SHA is fixed:

- **C0-A — invariant catalog:** turn P1-P15 into bounded read-only queries with
  an expected current result/state plus the eventual healthy target;
- **C0-B — fault matrix:** enumerate crash boundaries from API transaction
  through outbox, worker, provider, semantic commit, and context replay;
- **C0-C — baseline fixture:** create sanitized fixtures representing no-run
  sessions, stale running work, duplicate deliveries, orphan candidates,
  partial promotion, conflicts, and oversized sessions;
- **C0-D — observability map:** map current metrics and identify which
  invariants have no signal.

These agents may add separate docs or fixtures only after the spec owner assigns
non-overlapping paths.

### Gate

- Current production counts are refreshed without mutation.
- Each invariant has a query or a named missing-observability item.
- Each crash boundary has an expected recovery outcome.
- The checkpoint plan explicitly distinguishes package transport state from
  domain progress.
- The domain-progress ADR and `goal.md` agree on what may own identity,
  disposition, leases/fencing, reconciliation, and attempts. If the existing
  workflow model is sufficient, no new model is introduced.
- No production repair is run.

### Stop Conditions

Stop if the current WIP cannot be separated safely, if the live deployment SHA
cannot be identified, or if the first schema change cannot be additive.

## Checkpoint 1 — Lossless Work Creation

### Objective

When an ingest or lifecycle transaction commits, every required piece of
logical work and its package-owned delivery signal must already be durable.
When it rolls back, none of them may survive.

### Serial PR Spine

1. **C1.1 — logical work identity and transaction contract:** focused spec,
   minimum additive persistence, and invariant tests;
2. **C1.2 — hook/API atomic creation:** accepted event, logical work, and
   package outbox commit together;
3. **C1.3 — lifecycle and scheduler atomic creation:** explicit session end,
   stale-session end, digests, and other required producers adopt the same
   primitive.

Only one slice above is writable at a time. Parallel packages below are
allocated within that active slice.

### Required Behavior

- Establish a stable logical identity for each required workflow.
- Create or reuse the domain work intent inside the same transaction as the
  accepted evidence or lifecycle change.
- Invoke the approved outbox-backed task boundary so its transport row belongs
  to that transaction, rather than registering a post-commit callback that can
  be lost.
- Duplicate hook events reuse the same evidence and logical work.
- A duplicate request that encounters historical evidence with missing required
  work repairs or recreates that work idempotently instead of returning early
  forever.
- Task payloads remain stable ids only.
- A response is not considered accepted until evidence and required work are
  both committed.
- Digest work freezes an output-bounded source-visibility policy before
  selection or provider access: project output admits only project-visible
  input; team output admits project-visible input plus exactly its authorized,
  project-linked team. Null team is not all-team privilege.
- Request-driven digest/workflow reads and review mutations narrow by effective
  project/team scope,
  and unproven legacy digest output is quarantined from new digest sources,
  context/search/replay, and ordinary content reads without mutating historical
  rows; flattened product capabilities cannot bypass the quarantine.

The checkpoint spec decides whether the current workflow model can carry this
contract or needs a small additive companion concept. It must not recreate the
transport package.

### Parallel Packages

After the schema owner establishes the logical identity contract:

- **C1-A — ingest path owner:** hook/API event transaction and realtime work;
- **C1-B — lifecycle owner:** session-end and idle-sweep work creation;
- **C1-C — domain identity tests:** duplicates, concurrent session-end, and
  stable input identity;
- **C1-D — transaction fault tests:** rollback, process death immediately
  after commit, broker unavailable, and relay restart;
- **C1-E — focused scope review:** prove tenant/project scope is resolved
  before the work intent and task are created, and prove digest source
  visibility is authorized before selection, provider access, and output.

C1-A owns hook service files. C1-B owns session lifecycle/sweep files. One
schema owner owns models and migrations. No other agent edits those files.

### Gate

- Typed post-cutover P1/P2 cohorts pass. Focused P14 source-to-sink negative
  tests pass; global P14 remains explicitly missing until its later
  observability owner lands.
- A test that previously expected zero outbox rows inside the ingest
  transaction is replaced by the correct atomicity contract.
- Killing the request process after database commit still leaves recoverable
  work.
- Rolling the transaction back leaves no evidence, logical work, or transport
  row.
- Duplicate delivery creates no duplicate logical work.
- Existing transport relay behavior remains package-owned.
- Same-organization/project unauthorized-team digest sources never reach the
  frozen snapshot, provider, or a broader output; project/team scheduler and
  request-path negative tests pass.
- Same-organization unauthorized project/team request paths fail before product
  reads/writes, and legacy digest output without exact visibility/work linkage
  is absent from new digest inputs, fresh/replayed retrieval, weekly history,
  and ordinary content reads.

### Rollout

Run the scoped internal evaluator and record a post-cutover cohort report; CP1
does not expose or schedule invariant metrics, which belong to CP2 operations.
Do not repair historical no-run sessions yet. Observe only newly accepted
traffic through at least one full scheduler cycle.

## Checkpoint 2 — Leases, Recovery, And Invariant Reconciliation

### Objective

Make logical work recoverable independently of transient Celery task state.

### Serial PR Spine

1. **C2.1 — execution claim and fencing:** implement only the product-domain
   lease/attempt behavior authorized by the Checkpoint 0 ADR;
2. **C2.2 — session-work reconciliation:** no-run, stale-running, newer
   watermark, and later-failure recovery;
3. **C2.3 — candidate/projection/transport reconciliation:** orphan decision
   work, missing projections, unsatisfied dead letters, dry-run reporting, and
   metrics.

### Required Behavior

- A worker claims a bounded lease for a logical work item.
- An attempt records start, heartbeat or lease expiry, outcome, and failure
  class without erasing prior attempts.
- Worker loss makes the lease reclaimable.
- Infrastructure and provider failures schedule capped-backoff logical retry
  without converting the memory into a semantic rejection.
- Configuration faults remain visible operational blocks and automatically
  resume when configuration changes; they are not semantic terminal outcomes.
- Deterministic invalid inputs receive an explicit terminal operational
  disposition and remain visible in invariant reports.
- A reconciler derives required work from domain invariants, not from whether
  the transport queue is non-empty.
- The reconciler covers at least:
  - ended sessions with observations and no run;
  - queued work with no delivery signal;
  - expired running leases;
  - failed latest work even when an older run succeeded;
  - proposed candidates with no active decision work;
- dead-lettered delivery whose logical work is still required.
- A historical success satisfies only the exact input generation or watermark
  it processed; it cannot hide newer incomplete work.

Retry loops may use finite Celery retries per delivery. The domain reconciler
is responsible for future attempts, so an infinite external outage does not
create one immortal Celery message or silently abandon the domain work.

### Parallel Packages

After the schema/lease contract is merged within the checkpoint branch:

- **C2-A — claim/lease owner:** worker claim, heartbeat, release, and fencing;
- **C2-B — session reconciler owner:** missing, stale, and failed session work;
- **C2-C — candidate reconciler owner:** orphaned proposal/curation work;
- **C2-D — transport reconciliation owner:** package dead-letter and
  missing-signal comparison without owning transport mutations directly;
- **C2-E — operations owner:** invariant metrics, lag/age, and dry-run repair
  output;
- **C2-F — concurrency verifier:** two reconcilers, duplicate task delivery,
  worker kill, and lease expiry.

The core work/attempt files have one writer. Reconcilers should be separate
modules so their owners do not overlap.

### Gate

- P3, P4, P6, and P13 are observable.
- No old `RUNNING` row can remain authoritative solely because a worker died.
- An earlier successful run does not hide a later incomplete input watermark
  or failed latest run.
- Reconciler replay is idempotent under concurrent schedulers.
- Provider outage produces visible retry-wait work with capped backoff and
  health degradation, not a candidate decision.
- A read-only audit command explains every proposed repair before mutation.

### Rollout

Run the reconciler in report-only mode first. Compare its findings with the
refreshed production inventory. Enable automatic repair only for newly created
work; historical repair waits for Checkpoints 3 through 5.

## Checkpoint 3 — Complete And Idempotent Distillation

### Objective

Ensure that every observation in the immutable input window is processed
exactly in durable effect, even though provider calls and task deliveries are
at-least-once.

### Serial PR Spine

1. **C3.1 — deterministic input windows and coverage:** resumable chunks,
   watermarks, and continuation;
2. **C3.2 — provider-stage identity and strict output:** replay identity,
   malformed-response failure, fallback, and retry;
3. **C3.3 — complete reduction and provenance:** reduce only fully covered
   input and atomically create candidate decision work.

### Required Behavior

- Freeze a deterministic input watermark or snapshot for each distillation
  unit.
- Split large sessions into resumable deterministic chunks.
- Continue scheduling chunks until the entire input window has a disposition;
  a maximum per-attempt batch may limit work, but must not drop the tail.
- Persist partial chunk results as resumable intermediate work and run final
  candidate reduction only after complete input coverage.
- Give provider stages stable logical identities derived from workflow,
  snapshot, stage, chunk, input hash, and policy version.
- Record provider response provenance and content hashes.
- Do not convert malformed structured output into a zero-confidence memory
  candidate. Treat it as a failed extraction, try safe fallback policy, and
  retry automatically.
- Record coverage from every source observation to a candidate, memory, or
  explicit no-signal outcome.
- Persist a new candidate and its required autonomous decision work atomically,
  so a crash cannot create an orphan proposal.
- Preserve source file, symbol, command, error, and commit anchors through
  reduction.
- A crash after provider success but before candidate write must replay without
  duplicate durable candidates.

Exactly-once provider billing cannot be guaranteed across every network
failure. Durable effects and provider-call identity must still be idempotent,
and duplicate external calls must be measurable.

### Parallel Packages

- **C3-A — input snapshot/chunk owner:** deterministic paging and continuation;
- **C3-B — provider identity owner:** stable stage identity, replay, and
  malformed-output behavior;
- **C3-C — provenance owner:** complete observation coverage and evidence
  anchors;
- **C3-D — large-session tests:** more chunks than one attempt, crash after
  each boundary, and late-arriving observation behavior;
- **C3-E — provider fault tests:** timeout, 429/5xx, malformed response,
  fallback provider, and repeated delivery.

One owner edits the central distillation service. Other packages should use
new helper modules, provider modules, or tests assigned by the supervisor.

### Gate

- P3 and P5 pass for empty, ordinary, and oversized sessions.
- No test encodes permanent max-chunk truncation.
- Re-running the same input snapshot does not create duplicate candidates or
  lose provenance.
- New observations after a completed watermark create new logical work rather
  than being hidden by historical success.
- Malformed model output never appears in the semantic review queue.
- Provider recovery automatically resumes the same logical work.

### Rollout

Shadow the new coverage ledger on fresh sessions. Do not replay the historical
backlog until atomic memory transitions are complete.

## Checkpoint 4 — Atomic Memory Transitions And Rebuildable Projections

### Objective

Make the authoritative semantic transition coherent even if indexing,
embedding, workers, or processes fail.

### Serial PR Spine

1. **C4.1 — atomic promotion:** candidate, memory, current version, exact
   projection, provenance, audit, and embedding intent;
2. **C4.2 — atomic lineage transitions:** merge, supersede, refute, restore,
   conflict, and conflict-link durability;
3. **C4.3 — writer convergence and repair:** console, digest, import, feedback,
   projection rebuild, and consistency reporting use the same primitives.

### Required Behavior

- Promotion locks and rechecks the candidate.
- Candidate outcome, memory identity, current version, source provenance,
  exact retrieval representation, conflict-link cleanup, and transition audit
  commit atomically where they are authoritative database state.
- Embeddings remain rebuildable asynchronous projections. Their absence may
  degrade semantic recall, but cannot leave a memory half-promoted.
- Supersede, merge, refute, restore, and conflict resolution lock all affected
  current versions and revalidate the comparison before commit.
- Historical versions and source evidence remain immutable.
- Conflict links cannot be removed by ordinary TTL or cleanup.
- A consistency reconciler can rebuild missing derived indexes and report
  impossible authoritative combinations.
- Every write surface, including console/import/digest paths, goes through the
  same transition services.

The checkpoint spec should minimize the authoritative transaction. Provider
calls and expensive embedding computation happen before or after it with
fencing; they do not hold database locks.

### Parallel Packages

After one schema owner establishes additive constraints:

- **C4-A — promotion owner:** candidate-to-memory/version transaction;
- **C4-B — transition owner:** merge, supersede, refute, restore, and conflict
  resolution;
- **C4-C — projection owner:** exact document write and async embedding rebuild;
- **C4-D — alternate-writer owner:** console, digest, import, and feedback path
  convergence;
- **C4-E — consistency repair owner:** report and rebuild commands;
- **C4-F — fault/concurrency verifier:** crash between every old split boundary
  and simultaneous decisions on one candidate.

Central models and migrations have one owner. Promotion and transition service
files must not have multiple writers.

### Gate

- P7, P8, and P9 pass. The CP4 consistency/projection-repair subset of P13
  passes; global P13 remains open until the historical repair work in CP10.
- It is impossible to observe a promoted candidate without a coherent memory
  and current version.
- Exact retrieval is available when the authoritative transition commits.
- Missing embeddings are visible and automatically rebuildable.
- A failed supersede cannot leave both a winner and a partially stale loser.
- Every path that changes publishable memory creates the same audit and
  versioning shape.

### Rollout

Run consistency reporting against production. Repair only non-semantic derived
projections at this stage. Defer candidate promotion, rejection, and
supersession repair until Checkpoint 5 is canaried.

## Checkpoint 5 — Conflict-Only Autonomous Curation

### Objective

Replace the ordinary review backlog with an autonomous evidence-aware state
machine. Only genuine contradictions remain for humans.

### Serial PR Spine

1. **C5.1 — deterministic gates:** noise, exact identity, redaction, safe scope,
   and conflict-preserving TTL;
2. **C5.2 — scoped shortlist and failure-safe judge:** pgvector shortlist,
   structured comparison, fallback, and no fail-open outcome;
3. **C5.3 — autonomous decision orchestrator:** stable work drives automatic
   publish, revise, merge, supersede, reject, retry, or conflict;
4. **C5.4 — conflict-only product surface and rollout:** backend query,
   frontend inbox, eval, backlog shadow, and canary.

### Required Decision Flow

1. Validate scope, provenance, redaction, and minimum evidence.
2. Reject deterministic noise while retaining source evidence.
3. Resolve exact content identity deterministically.
4. Build a bounded near-match shortlist using scoped PostgreSQL retrieval.
5. Treat similarity as comparison input, never as a destructive verdict.
6. Compare candidate, current memory version, evidence, and temporal anchors.
7. Apply one automatic outcome:
   - publish a new memory;
   - revise or merge into a new version;
   - supersede an obsolete version;
   - reject the derived candidate;
   - retry because required capability is unavailable;
   - create a conflict only when supported claims remain mutually
     incompatible.
8. Commit the selected transition through Checkpoint 4 primitives.

There is no persistent `skip` outcome. Uncertainty either means “not enough
support to publish,” which automatically rejects the projection, or “required
evidence/capability is temporarily unavailable,” which remains operational
retry work.

### Policy Changes

- Remove confidence-threshold items from the human queue.
- Replace sensitive-term holds with redaction, automatic rejection, or
  project-scope narrowing.
- Do not auto-create organization-wide memory from ordinary agent evidence.
- Remove age/decay items from the human queue.
- Exclude conflict candidates from TTL and ordinary cleanup.
- On judge or embedding failure, retry or use an approved safe fallback; do not
  silently keep both, promote blindly, merge, or supersede.
- Require stronger evidence for destructive changes than for creating a
  non-destructive new version.

### Parallel Packages

- **C5-A — deterministic gate owner:** noise, exact identity, scope narrowing,
  and redaction policy;
- **C5-B — shortlist owner:** direct pgvector distance and bounded lexical/exact
  candidates within already-authorized scope;
- **C5-C — semantic judge owner:** structured outcomes, evidence policy,
  fallback, and failure classification;
- **C5-D — decision orchestrator owner:** operational work through atomic
  transition;
- **C5-E — conflict inbox backend owner:** conflict-only queries and resolution;
- **C5-F — frontend owner:** conflict inbox, evidence comparison, and health
  separation;
- **C5-G — eval owner:** golden duplicate, revision, contradiction, noise, and
  uncertainty corpus.

The decision orchestrator and current curation module have one writer. Backend
conflict API and frontend may proceed in parallel after the outcome contract is
frozen.

### Gate

- P6, P8, P9, and P12 pass.
- Every non-conflict proposal automatically converges when dependencies are
  healthy.
- High vector similarity alone cannot supersede or merge.
- Provider failure produces no semantic decision.
- Human queue API and UI return conflicts only.
- A conflict includes both claims, provenance, current versions, and a clear
  resolution operation.
- An offline eval meets checkpoint thresholds agreed in its focused spec.
- Existing candidate backlog has a dry-run decision report; it is not bulk
  promoted.

### Rollout

1. Shadow decisions without mutation on a sampled backlog.
2. Compare deterministic and model outcomes against the eval corpus.
3. Canary automatic rejection of obvious noise.
4. Canary non-destructive publication.
5. Canary revision/merge.
6. Enable destructive supersede only after conflict recall and rollback are
   proven.
7. Keep the old manual queue read-only during the canary, then remove ordinary
   items once P12 is continuously true.

## Checkpoint 6 — Immutable Context And Complete Client Lifecycle

### Objective

Make capture complete enough to support the loop and make every injected
context bundle a strict immutable artifact.

### Serial PR Spine

1. **C6.1 — bundle identity and snapshot:** request fingerprint, immutable
   selected versions/bodies, and replay conflict;
2. **C6.2 — strict packing and degraded retrieval:** hard budget, exact
   fallback, and warnings;
3. **C6.3 — shared hook lifecycle contract:** stable ids and supported
   stop/failure events;
4. **C6.4 — runtime clients and E2E:** canonical CLI first, then Claude/Codex
   adapters and generated bundle synchronization.

### Backend Context Behavior

- Compute a canonical request fingerprint from all behavior-relevant request
  inputs, resolved scope, retrieval policy version, and repository state.
- Reusing a request id with a different fingerprint returns an explicit
  idempotency conflict instead of stale context.
- Persist the selected memory version, rendered body snapshot, validity state,
  inclusion reason, citations, and scope evidence needed for byte-stable replay.
- Enforce token budget strictly. An oversized first item is truncated through
  an explicit bounded representation or omitted; it cannot exceed the budget
  merely because it ranks first.
- Authorize before retrieval, apply semantic eligibility, then rank and pack.
- Preserve degraded-mode warnings without letting provider failure suppress
  exact authorized retrieval.

### Client Behavior

- Preserve stable tool-use and event identities across all supported hooks.
- Capture normal completion, stop, and tool-failure lifecycle signals supported
  by each runtime.
- Keep capture and recall independent: a failed observation submission must not
  prevent an otherwise authorized context request.
- Preserve server warnings, correlation ids, selected context metadata, and
  actionable errors through CLI/plugin layers.
- Keep clients thin and runtime-neutral.

The active Codex harness work must be merged or explicitly handed off before
this checkpoint begins. One client owner reconciles Claude and Codex behavior
without editing the same files concurrently.

### Parallel Packages

- **C6-A — bundle identity owner:** fingerprint, immutable snapshot, replay;
- **C6-B — packing owner:** strict budget and deterministic rendering;
- **C6-C — retrieval degradation owner:** exact fallback and warnings;
- **C6-D — Claude client owner:** lifecycle and contract fixtures;
- **C6-E — Codex client owner:** lifecycle and contract fixtures;
- **C6-F — cross-runtime E2E owner:** capture failure, recall success, duplicate
  hooks, and replay mismatch.

### Gate

- P10 and P14 pass.
- Bundle replay is byte-stable after the underlying memory later changes.
- Same id plus different semantic request is rejected explicitly.
- No response exceeds its declared token budget.
- Missing semantic provider still returns authorized exact matches with a
  warning.
- Runtime hook matrices are fixture-backed and all supported terminal/failure
  events have a documented disposition.
- Capture failure and recall failure are reported independently.

## Checkpoint 7 — Memory CI Temporal Foundation

Parent gate: MCI-0 through MCI-4 in the companion proposal; no single branch
should contain this whole gate.

### Objective

Add the evidence and repository-state foundation needed to know whether a
memory is current, without yet changing production retrieval decisions.

This checkpoint is governed by the companion Memory CI proposal.

### Required Behavior

- Normalize existing exact path and symbol anchors for current memories and new
  candidates.
- Record canonical repository snapshots and coalesced change sets.
- Establish a trusted scoped SCM/CI source for bounded old/new blobs or
  fingerprints; missing evidence stays explicit.
- Map changed, renamed, and deleted files and existing symbols to potentially
  affected memories.
- Keep semantic expansion, broad anchor types, and custom product surfaces out
  of this first causal loop.
- Create idempotent revalidation work through the Checkpoint 1-2 primitives.
- Compute deterministic unchanged/rename shadow validation and an explainable
  temporal reason without hiding or mutating current production memory.
- Backfill anchors conservatively; unknown provenance remains explicit.

### Parallel Packages

- **C7-A — repository snapshot owner;**
- **C7-B — evidence-anchor owner;**
- **C7-C — deterministic impact graph owner;**
- **C7-D — trusted evidence adapter owner;**
- **C7-E — deterministic shadow validator owner;**
- **C7-F — revalidation scheduler and temporal evaluation owner.**

The schema owner serializes repository-state and anchor migrations. Signal
adapters may proceed in parallel after their common event contract is frozen.

### Gate

- Every new memory has explainable evidence anchors or an explicit
  unanchored classification.
- Replaying the same repository change creates no duplicate revalidation work.
- Unrelated file changes do not trigger global memory revalidation.
- Missing required evidence produces unknown/retry, not confirmation or
  invalidation.
- Shadow results include reasons and current repository snapshot.
- No production retrieval behavior changes in this checkpoint.

## Checkpoint 8 — Automatic Temporal Revalidation And Context Gating

Parent gate: MCI-5 then MCI-6 in the companion proposal; no single branch
should contain this whole gate.

### Objective

Turn shadow temporal analysis into an autonomous state machine that evolves
memory with the canonical codebase.

### Required Behavior

- Change impact moves affected memory into a visible revalidation lifecycle.
- Deterministic validators and current evidence run before model comparison.
- Bounded semantic impact expansion may improve recall only after exact scope
  and deterministic causal reasons are established; it cannot apply a state
  change.
- Revalidation automatically confirms, revises, supersedes, refutes, splits,
  or withholds memory.
- Insufficient operational capability becomes automatic retry.
- A conflict is created only when current evidence supports incompatible
  claims and precedence rules cannot resolve them.
- Context eligibility applies temporal state before similarity ranking.
- A project-level revision coverage boundary prevents a newly accepted but not
  yet impact-planned revision from inheriting an older “current” certification.
- When requested state R is newer than fully processed coverage, the default
  policy withholds code-sensitive memory as unknown and emits a coverage-lag
  warning. An explicit historical request may instead pin to the last processed
  revision and must label that revision.
- High-risk unknown memory is withheld; lower-risk suspect memory may be
  included only under an explicit policy with a visible warning.
- Every revision creates a versioned transition and preserves the historical
  code/evidence snapshot.
- Rollback or reversion is handled as a new code state; prior versions may be
  revalidated rather than copied or silently resurrected.
- Agents can receive a compact memory delta since their previous repository
  snapshot.

### Parallel Packages

- **C8-A — deterministic validator owner;**
- **C8-B — evidence-aware revalidation judge owner;**
- **C8-C — temporal transition owner;**
- **C8-D — context eligibility owner;**
- **C8-E — memory-delta owner;**
- **C8-F — timeline/conflict frontend owner;**
- **C8-G — change/revert/branch E2E owner.**

The temporal transition and context eligibility contracts are frozen before
frontend and client work begins.

### Gate

- P11 and P12 pass.
- P15 passes for the window between revision acceptance and impact-plan
  completion.
- Directly anchored code changes trigger revalidation within the configured
  project SLO.
- Unchanged anchored evidence confirms validity without model churn.
- Obsolete operational facts stop being injected as current.
- Revalidation failure cannot silently refute or revise memory.
- Only genuine temporal contradictions reach the conflict inbox.
- A repository revert produces a coherent, audited result.
- Shadow-versus-active evaluation meets the thresholds frozen in the MCI-0
  focused evaluation contract after baseline measurement.

## Checkpoint 9 — Retrieval And Curation Performance

### Objective

Scale the now-correct production primitives without creating separate debug or
curation algorithms.

### Serial PR Spine

1. **C9.1 — one scoped pgvector primitive:** direct database distance for
   retrieval and curation with behavior-parity tests;
2. **C9.2 — measured lexical/debug convergence:** query-plan evidence,
   justified indexes, production-service reuse, and load verification.

### Ordered Work

1. Use pgvector distance directly as the semantic score and shortlist source.
2. Ensure curation near-duplicate selection uses the same scoped PostgreSQL
   primitive.
3. Measure lexical query plans under production-shaped data.
4. Add trigram/full-text indexes only where `EXPLAIN ANALYZE` proves the need.
5. Make search-debug call the production retrieval service or explicitly defer
   it; do not maintain a second ranking implementation.
6. Tune batch sizes, HNSW parameters, and worker concurrency only after query
   and queue SLOs are measured.

The curation shortlist needed for safe backlog processing may be introduced in
Checkpoint 5. This checkpoint consolidates and optimizes the shared primitive.

### Parallel Packages

- **C9-A — pgvector retrieval owner;**
- **C9-B — lexical plan/index owner;**
- **C9-C — curation primitive convergence owner;**
- **C9-D — debug-service convergence owner;**
- **C9-E — load and regression benchmark owner.**

Database indexes have one migration owner and require before/after plans.

### Gate

- Results and explanations remain behaviorally equivalent to the accepted eval
  corpus.
- No unscoped global vector query is introduced.
- Production and debug scores come from the same primitive.
- Query, context, and curation SLOs pass on production-shaped data.
- Index write cost and migration rollback are recorded.

## Checkpoint 10 — Historical Repair, Canary Expansion, And Release Gate

### Objective

Bring existing data under the new invariants without blind bulk mutation, then
prove the autonomous loop in fresh and failure-path environments.

### Serial PR Spine

1. **C10.1 — inventory, backup, and dry-run manifests;**
2. **C10.2 — derived projection and operational-work repair;**
3. **C10.3 — candidate shadow decisions and bounded semantic canary;**
4. **C10.4 — temporal baseline, context-gating canary, and MCI-7A
   continuous-operation rollout;**
5. **C10.5 — rollback drill, fault E2E, fresh-clone verification, and release
   runbooks.**

### Repair Order

The repair order is strict:

1. snapshot/backup and refresh invariant report;
2. repair missing derived retrieval projections;
3. reclaim expired operational work;
4. create missing logical work for ended sessions and uncovered input
   watermarks;
5. complete distillation coverage;
6. re-run autonomous candidate decisions in shadow;
7. apply low-risk deterministic rejections;
8. apply non-destructive promotions/revisions in canary projects;
9. enable merge/supersede after conflict-recall review;
10. backfill Memory CI anchors and run shadow revalidation;
11. enable temporal context gating by canary;
12. expand only while all invariants remain green.

Never bulk-promote the existing proposed backlog merely because confidence is
high. Reprocess it through the same current evidence, policy, curation, and
transition path as new work.

### Parallel Packages

- **C10-A — repair planner:** dry-run manifests and resumable batches;
- **C10-B — backup/rollback verifier;**
- **C10-C — canary observer:** invariant, latency, provider cost, and conflict
  metrics;
- **C10-D — Compose fault E2E owner;**
- **C10-E — fresh-clone release verifier;**
- **C10-F — docs/runbook owner.**

Only one repair executor may mutate a given organization/project at a time.

### Gate

- P1-P15 are continuously true for canary projects.
- The human inbox contains conflicts only.
- Historical no-run, stale-running, orphan-candidate, partial-promotion, and
  missing-index counts converge to zero or explicit accepted exceptions.
- Every repair batch is audited, resumable, and reversible where the
  transition permits.
- Fresh-clone Compose E2E proves capture to future-session context.
- Fault E2E kills API, relay, worker, broker, provider, and scheduler at the
  named boundaries and demonstrates recovery.
- Required CI is green and commands/exit codes are recorded.

## Parallel Ownership Map

The supervisor should use these stable conceptual zones to minimize conflicts.
Exact files are assigned in each checkpoint task card.

| Zone | Primary responsibility | Serialization rule |
|---|---|---|
| Domain schema | workflow identity, attempts, anchors, validity, constraints | One owner for all models and migrations in a checkpoint |
| Ingest | hook/API acceptance transaction and idempotency | One writer; clients remain read-only until contract freezes |
| Workflow runtime | claim, lease, retry, reconciler | One core writer; separate reconcilers may own new modules |
| Distillation | input snapshots, chunking, provider stages, provenance | One central service writer |
| Semantic transitions | promotion, merge, supersede, refute, conflict | One transition owner |
| Retrieval | authorized document selection, exact/vector ranking | One production primitive owner |
| Context | fingerprint, packing, snapshot, audit | One bundle owner |
| Client integrations | CLI, Claude, Codex hook mappings | One owner per runtime after shared contract freezes |
| Frontend | conflict, health, timeline, debug views | May run parallel once backend response contract is frozen |
| Operations | invariant queries, metrics, repair commands | Separate files; cannot mutate production without supervisor gate |
| Evaluation | fixtures, golden corpora, fault harness | Read-only against production; separate test paths |

When two tasks need the same central file, they are not parallel tasks. The
supervisor serializes them or gives the whole file to one owner.

## Verification Strategy

### Test Layers

Each checkpoint runs only the broadest relevant set, but the campaign must
eventually cover:

- pure state-transition and policy tests;
- database constraint and migration tests;
- ingest transaction/outbox tests;
- worker lease, duplicate, retry, and reconciliation tests;
- provider timeout, malformed response, fallback, and replay tests;
- semantic decision evals;
- tenant/project/team negative-scope tests;
- immutable context and budget tests;
- CLI/plugin hook contract fixtures;
- Memory CI change-impact and reversion tests;
- Docker Compose golden and fault-injection E2E;
- fresh-clone release verification.

Python, CLI, plugin, backend, and E2E commands run in containers once Compose
is available. Frontend uses the repository's pnpm workflow and retains the
global package-age gate.

### Fault Boundaries

At minimum, fault tests cover process death:

- before domain transaction commit;
- immediately after commit;
- before relay publication;
- after broker delivery but before claim;
- after claim but before provider call;
- after provider response but before durable semantic output;
- during candidate promotion;
- after memory/version commit but before embedding;
- during merge/supersede;
- during context snapshot creation;
- during revalidation transition.

For each boundary, the expected result is one of:

- complete rollback;
- durable work remains ready/retryable;
- idempotent replay converges to one durable effect;
- derived projection is automatically rebuildable.

“Manual database fix” is not an accepted expected result.

### Evidence Ledger

Every PR-slice report records its parent campaign gate and:

- branch and start/end SHA;
- focused spec path;
- PR/MR link and state;
- migrations and rollback notes;
- commands and exit codes;
- CI job names and results;
- invariant query before/after counts;
- fault tests executed;
- production or canary scope;
- review findings and dispositions;
- deferred risks with owner and target checkpoint.

## Operational Signals

Queue depth remains useful, but the primary dashboard should expose domain
progress:

- accepted evidence without required work;
- ended sessions without complete current-watermark distillation;
- ready, leased, retry-wait, and expired logical work;
- oldest age per work class;
- observations without disposition;
- proposed candidates without automatic work;
- conflicts awaiting resolution;
- promoted memories missing coherent current projection;
- missing embeddings and projection lag;
- context fingerprint conflicts and budget drops;
- memories by temporal state;
- repository changes awaiting revalidation;
- stale or unknown memories withheld from context;
- repair backlog and last successful invariant sweep.

Provider outage, queue lag, and invariant violations may page operations. They
do not enter the semantic conflict inbox.

## Rollback Principles

- Prefer additive migrations and backward-compatible reads before activation.
- Put new decision behavior behind organization/project canary controls.
- Keep old data readable until the new invariant report is clean.
- Roll back behavior flags before rolling back additive schema.
- Never roll back by deleting evidence, workflow history, memory versions, or
  audit.
- A failed repair stops at a batch boundary and can be resumed from stable
  identities.
- Destructive semantic transitions require an inverse operation or preserved
  prior version that can be restored through the same audited service.

## Assumptions

This roadmap assumes:

- PostgreSQL is the authoritative store and supports the required constraints,
  locking, full-text search, pgvector, and trigram extensions;
- `django-celery-outbox` remains the approved package transport;
- the canonical project repository and default branch can be identified for
  Memory CI;
- raw evidence retention is long enough to rebuild derived memory;
- provider policies can define fallback and distinguish retryable operational
  failure from semantic output;
- organization/project/team scope is resolved before every domain operation;
- the active Codex harness checkpoint will be merged or separated before client
  checkpoint work;
- operators may respond to infrastructure alerts, but they do not curate
  ordinary memory daily;
- genuine conflicts may remain unresolved until a human acts and are safely
  excluded or explicitly marked in context meanwhile.

## Known Tradeoffs

- Infinite retry under an infinite provider outage preserves evidence but
  cannot provide finite semantic completion.
- Strict fail-safe curation may delay publication during provider degradation;
  exact retrieval of already approved memory remains available.
- Temporal gating may temporarily reduce recall while a high-impact memory is
  unknown; this is preferable to injecting a confidently stale instruction.
- Complete provenance and immutable context snapshots consume more PostgreSQL
  storage. Retention and compaction must preserve reconstructability.
- Canonical-default-branch validity does not initially model every feature
  branch. The first version should be correct for one declared code state
  before adding overlays.
- Model judgments cannot prove code truth alone. Deterministic anchors, tests,
  source precedence, and preserved conflict handling remain necessary.

## Campaign Definition Of Done

The campaign is complete when:

- every accepted event and required work signal is one atomic durable commit;
- every logical workflow is observable, leased, retryable, and reconcilable;
- every session input reaches a durable disposition without permanent
  truncation;
- no provider or worker failure silently creates a semantic outcome;
- all memory transitions are versioned, atomic, auditable, and reconstructable;
- the autonomous curator settles every healthy non-conflict candidate;
- the human inbox contains only genuine conflicts;
- context replay is immutable, fingerprint-safe, authorized, and strictly
  bounded;
- Claude and Codex clients faithfully capture supported lifecycle events while
  remaining thin;
- Memory CI revalidates impacted memories as the canonical codebase changes;
- temporally invalid knowledge is not injected as current;
- production historical data converges under dry-run, canary, and repair gates;
- fault-injection and fresh-clone E2E prove the whole loop;
- docs, runbooks, metrics, and CI describe the behavior that actually exists.

## Recommended First Move

Do not begin with auto-review tuning, retry-count changes, pgvector
optimization, or backlog mutation.

Begin with Checkpoint 0, then implement Checkpoint 1:

`fix: make accepted memory work creation lossless`

The immediate regression should prove that raw evidence, the logical workflow
intent, and the package-owned outbox signal either all commit or all roll back.
Only after that is true does it make sense to invest in leases, indefinite
retry, autonomous curation, and Memory CI.
