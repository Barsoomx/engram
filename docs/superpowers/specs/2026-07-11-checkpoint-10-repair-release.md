# Checkpoint 10 - Repair Rehearsal And Release Evidence

Date: 2026-07-11
Status: focused parent implementation specification
Roadmap gate: Checkpoint 10, C10.1 through C10.5
Implementation base at drafting: `master` at `79ddb15a`

## Scope

Checkpoint 10 builds development and CI tooling that proves historical repair,
canary expansion, rollback, and release behavior against disposable data. It
does not authorize or perform mutation of production, staging, or any external
database. The implementation produces scoped dry-run artifacts, durable
simulation-only repair runs, replay and fault evidence, and fresh-clone
runbooks.

The authoritative product order is the Checkpoint 10 section of
`docs/superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md`. Temporal
behavior and MCI-7A thresholds come from
`docs/superpowers/specs/2026-07-09-memory-ci-feature-proposal.md` and the
evaluation contract merged by CP7/CP8. P1-P15 and F1-F19 remain defined by
`docs/reliability/memory-loop-invariants.md` and
`docs/reliability/memory-loop-fault-matrix.md`.

This specification freezes the CP10 orchestration and evidence contract. It
does not duplicate the lease, projection, coverage, candidate-decision,
semantic-transition, temporal-revalidation, retrieval, or context services
delivered by CP2 through CP9.

## Binding Execution Boundary

- All planner commands are read-only against domain tables.
- All state-changing repair execution is restricted to a disposable simulation
  database whose name begins with `engram_repair_sim_`.
- Simulation additionally requires `ENGRAM_REPAIR_SIMULATION_ENABLED=true` in
  the process settings; the default is false.
- No HTTP endpoint, Celery task, Beat schedule, deployment manifest, or general
  operator `--apply` command exposes the executor.
- No CP10 command accepts a database URL, host, SSH target, Kubernetes context,
  organization wildcard, project wildcard, or unscoped mode.
- The campaign never runs against production, inspects it through SSH, or edits
  `deploy/**`, private deployment tooling, D2, secrets, or storage migration.
- Future production repair requires a separate reviewed authorization document
  naming the exact manifest hash, database, scopes, backup evidence, operator,
  and abort thresholds.
- The main agent remains the only orchestrator and Git owner. Implementation
  workers own disjoint files and do not commit, merge, push, or publish.

## Goal And Acceptance Boundary

CP10 is accepted when a clean checkout can create the synthetic historical
anomalies, emit and verify one immutable scoped manifest, interrupt and resume
every repair phase, replay the manifest without duplicate durable effects,
rehearse rollback, and finish with the expected invariant and canary report.

The proof must establish:

- an explicit organization/project pair is resolved before any inventory read;
- target and foreign-scope fixtures exist in every scenario, and foreign rows
  remain byte-for-byte unchanged;
- the manifest records inventory, backup/source/schema/policy evidence, ordered
  actions, blocked items, and expected invariant deltas;
- planning does not write domain, workflow, repair-control, audit, or package
  rows;
- runs/batches have durable identity, lease/fence, cursor, counters, audit, and
  one active executor per project;
- batches are transactional, bounded, resumable, idempotent, and follow
  projection -> operational -> coverage -> candidate -> temporal order;
- candidate changes gate deterministic rejection -> non-destructive ->
  destructive, and temporal baseline/revalidation precede context gating;
- canary expansion remains simulation-only while P1-P15 and frozen MCI gates
  are green;
- rollback disables simulated behavior first and never deletes raw evidence,
  workflow history, memory versions, lineage, or audit;
- fresh-clone and fault jobs run Python/backend/E2E checks inside containers;
- the evidence ledger records exact SHA, commands, exit codes, fixture and
  manifest hashes, invariant counts, fault outcomes, and residual risks;
- the final report states explicitly that no production mutation occurred.

## Required Dependency Gate

Implementation starts only after CP9 merges. Before C10.1 RED, record the exact
merged symbols for: scoped P1-P15 evaluation; CP2 claim/fence/retry/missing-work;
CP3 immutable coverage; CP4 projection rebuild and audited semantic
transition/restore; CP5 current-policy shadow decision; CP7/CP8 exact anchors,
revision coverage, revalidation, and context eligibility; and the frozen MCI
threshold contract/digest.

If an upstream name differs from this spec, C10 adds a narrow adapter with the
interface below. It does not rename or fork the upstream service. A missing
capability blocks the consuming slice rather than introducing substitute
domain logic inside `engram.repair`.

## Existing Seams To Replace Or Reuse

- `engram_backfill_retrieval_terms` remains a legacy row loop, not the CP10
  executor; the projection adapter calls the CP4 rebuild service.
- `RetryFailedDistillations` remains a proxy, not a repair ledger; the
  operational adapter consumes merged CP2/CP3 exact-watermark reconcilers.
- The CP0 baseline fixture remains characterization evidence and is never
  rewritten to make repair green.
- The existing runtime-durability controller retains Rabbit/Beat/PID1 fault
  ownership; CP10 adds only container-side repair preparation/assertion.
- `docs/release-runbook.md` remains release authority; C10.5 links a disposable
  rehearsal and performs no tagging, publishing, deployment, or post-release
  production check.

## Design Choice

Use one repair-run spine with phase adapters. Independent backfill commands
cannot prove global ordering or cross-phase resume. A filesystem-only executor
cannot prove P13 after process loss. A second workflow engine would duplicate
CP2 and create conflicting retry authority.

The selected design has one read-only planner/verifier, two repair-control
models, one lease/fence executor, phase adapters into upstream services, one
pure MCI canary evaluator, and container-only fixture/replay/fault/restore
evidence. It does not add a second workflow or semantic engine.

## Manifest Artifact Contract

`engram_plan_repair` writes a new empty directory. It refuses to overwrite or
append to an existing directory. Successful completion uses an atomic rename
from a sibling temporary directory; an interrupted plan leaves no valid
`manifest.json` at the requested path.

The directory layout is fixed:

```text
manifest.json
inventory.json
phases/{01_projection,02_lease_reclaim,03_missing_work,04_coverage}.ndjson
phases/{05_candidate_shadow,06_deterministic_reject}.ndjson
phases/{07_non_destructive_transition,08_destructive_transition}.ndjson
phases/{09_temporal_baseline,10_temporal_revalidation}.ndjson
phases/{11_context_gate,12_canary_expansion}.ndjson
batches.json
```

`manifest.json` has exactly these top-level fields:

| Field | Contract |
|---|---|
| `schema_version` | integer `1` |
| `mode` | literal `dry_run` |
| `organization_id` | canonical UUID string |
| `project_id` | canonical UUID string belonging to the organization |
| `snapshot_as_of` | UTC RFC3339 timestamp supplied once to the planner |
| `source_git_sha` | 40-character lowercase commit SHA |
| `migration_leaf_fingerprint` | SHA-256 of sorted Django migration leaves |
| `policy_fingerprints` | sorted map of behavior-relevant upstream contract hashes |
| `invariant_catalog_sha256` | digest of the P1-P15 catalog version |
| `evaluation_contract_sha256` | digest of the frozen CP7/CP8 MCI thresholds |
| `backup_evidence` | disposable artifact name, size, SHA-256, and restore-check id |
| `inventory_sha256` | digest of canonical `inventory.json` |
| `phase_files` | ordered path/count/SHA-256 records for all twelve phase files |
| `batches_sha256` | digest of canonical `batches.json` |
| `plan_sha256` | digest of all semantic fields and referenced file digests |
| `generated_at` | UTC display timestamp excluded from `plan_sha256` |

`inventory.json` contains target-scoped counts, bounded refs, watermarks, and
P1-P15 for evidence, generations, work, coverage, transitions, projections,
anchors/revisions, revalidation/context, and conflicts. Foreign control digests
live only in E2E evidence and are never read by the planner.

Each NDJSON line has exactly:

```json
{"action":"rebuild_exact_projection","blocked_by":[],"entity_ref":"memory_version:00000000-0000-0000-0000-000000000000","input_sha256":"64 lowercase hex characters","reason_code":"current_projection_missing","reversible":true}
```

`engram.repair.contracts` enumerates action/reason values. `entity_ref` is a
type plus stable UUID/canonical revision id. Sort by `entity_ref`, `action`,
then `input_sha256`; duplicate identity is invalid.

`batches.json` groups consecutive action identities by phase. Each descriptor
contains phase, zero-based ordinal, first/last identity, item count, canonical
item SHA-256, batch SHA-256, and declared reversibility. It never embeds the
action payload a second time.

Canonical JSON uses UTF-8/NFC, sorted keys, compact separators, LF, lowercase
UUIDs, UTC `Z` timestamps, and no floats. The plan digest excludes only
`generated_at` and itself; fixed input yields byte-identical semantic files.

The artifact must not contain source bodies, raw events, prompts, provider
responses, embeddings, credentials, API keys, authorization headers, access
tokens, environment dumps, exception traces, or arbitrary model metadata.
Tests recursively reject sensitive key names and known synthetic secrets.

The planner runs its inventory and action readers in one PostgreSQL
`REPEATABLE READ`, read-only transaction. Every reader receives the already
resolved scope and `snapshot_as_of`; no reader may discover its own scope or
read rows newer than the snapshot boundary.

## Repair Control Models

C10.1 creates the `engram.repair` Django app and additive initial migration.
No data migration runs.

`RepairRun` fields are:

- UUID; organization/project; literal mode `simulation`; manifest version and
  plan hash; snapshot, source, and evaluation-contract hashes;
- status (`registered`, `running`, `paused`, `succeeded`, `failed`,
  `rolling_back`, `rolled_back`), next phase/ordinal, lease owner/expiry,
  monotonically increasing fence, timestamps, sanitized counters/failure code.

`RepairBatch` fields are:

- UUID/run; phase/ordinal/batch hash; item count/input hash;
- status (`planned`, `running`, `applied`, `blocked`, `failed`, `rolled_back`),
  attempt count, committing fence, before/after digests, sanitized counters,
  inverse refs, stable failure code, and timestamps.

Database constraints enforce project/organization agreement, lowercase hash
shape, positive item counts, one `(run, phase, ordinal)`, one batch hash per
run, terminal timestamps, and legal status fields. A partial unique constraint
allows only one `registered`, `running`, `paused`, or `rolling_back` run per
organization/project.

Repair rows are control/audit state. They never replace `WorkflowWork`, package
outbox rows, semantic transition audit, or repository-revision work.

## Scope, Lease, Lock, And Fence Contract

`RepairScope` contains `organization_id`, `project_id`, and the tuple of project
team ids resolved from current scope. Construction fails closed when the
project is absent, archived when the fixture disallows archived projects, or
belongs to another organization.

The read-only planner may run concurrently. Registering or resuming simulation:

1. verifies manifest/guard, resolves scope again, and rejects
   source/schema/policy drift;
2. `select_for_update` locks the run and obtains a project-derived PostgreSQL
   transaction advisory lock;
3. claims/reclaims a bounded lease and increments the fence;
4. executes at most one manifest batch in one transaction and rechecks the
   fence before commit;
5. atomically stores batch result, cursor, and audit, then releases the lock.

The caller repeats `execute_next_batch`; the executor never holds a database
transaction across batches or provider calls. Lease loss causes a stable
`repair_fence_stale` result. A stale owner cannot mark a batch applied, advance
the cursor, or commit an upstream transition.

Blocked upstream capability, provider outage, or configuration failure pauses
at the current action identity. It never converts operational failure into a
candidate rejection or skips ahead to a later phase.

## Adapter Interface

`engram.repair.adapters.base` defines one `RepairAdapter` protocol. Each phase
adapter has these exact operations:

| Operation | Input | Output | Rule |
|---|---|---|---|
| `plan` | resolved scope, snapshot, upstream readers | ordered immutable actions | read-only |
| `precheck` | verified action, current scope | `ready`, `already_converged`, `blocked`, or `drifted` | no write/provider call |
| `apply` | ready action, run id, fencing token | effect digest, counters, inverse refs | calls upstream service |
| `verify` | action and effect digest | `converged` or stable failure code | read-only |
| `rollback` | applied effect and inverse refs | rollback effect digest | only where declared reversible |

`already_converged` marks the batch action as idempotently satisfied. `drifted`
pauses the run and requires a new plan; it never silently selects a replacement
row. `blocked` preserves the cursor and stable reason.

Adapters may query through upstream read services. Apply and rollback methods
must not call `.save()`, `.update()`, `.delete()`, `bulk_create()`, or
`bulk_update()` on session, workflow, candidate, memory, version, projection,
anchor, revision, revalidation, or context models. A source census test enforces
that state changes enter through the merged CP2-CP8 domain services.

No adapter sends raw action data through Celery. When an upstream operation
requires async work, it creates or reuses `WorkflowWork` and its package-backed
signal through the existing atomic producer using stable ids only.

## Strict Phase Order

The verifier accepts only this complete phase sequence:

| Order | Phase | Permitted effect |
|---|---|---|
| 1 | `01_projection` | rebuild missing/stale derived exact projection and schedule embedding work |
| 2 | `02_lease_reclaim` | reclaim expired operational work through CP2 fencing |
| 3 | `03_missing_work` | create missing exact-generation/session/revision work identities |
| 4 | `04_coverage` | resume uncovered deterministic chunks and explicit dispositions |
| 5 | `05_candidate_shadow` | recompute current-policy decision without semantic mutation |
| 6 | `06_deterministic_reject` | reject only deterministic noise/invalid projection through CP5 |
| 7 | `07_non_destructive_transition` | publish or revise in synthetic canary scope through CP4/CP5 |
| 8 | `08_destructive_transition` | merge/supersede only after rollback and conflict-recall gates |
| 9 | `09_temporal_baseline` | exact anchor backfill or explicit unanchored baseline |
| 10 | `10_temporal_revalidation` | create/reuse shadow revalidation work for baseline revision |
| 11 | `11_context_gate` | simulate temporal eligibility before ranking |
| 12 | `12_canary_expansion` | evaluate project-to-organization default-on and immediate reversal |

Inventory, backup verification, and baseline invariants precede phase 1 and are
not mutating phases. A manifest may contain zero actions for a phase, but may
not omit, duplicate, rename, or reorder the phase file.

Default batch sizes are 100 for projection and operational phases, 50 for
coverage and temporal phases, and 1 for every semantic candidate phase. Hard
caps are 500, 200, and 1 respectively. Batch sizes are frozen in the manifest;
resume cannot change them.

## C10.1 - Inventory, Backup Evidence, And Dry-Run Manifests

### Behavior

C10.1 adds the repair app, immutable artifact codec, scoped inventory readers,
manifest planner/verifier, repair-control schema, and simulation guard. It does
not execute a repair batch.

The command is:

```text
python manage.py engram_plan_repair --dry-run \
  --organization <uuid> --project <uuid> \
  --snapshot-as-of <RFC3339-Z> --source-git-sha <40-hex> \
  --backup-evidence <absolute-json-path> --output <new-directory>
```

All arguments shown are mandatory. The backup evidence file must describe a
disposable database artifact and a successful restore verification; C10.1 does
not invoke `pg_dump`. Relative paths, symlinks escaping the workspace, unknown
fields, invalid hashes, absent threshold contracts, and non-empty output paths
fail before any artifact is published.

`engram_verify_repair_manifest --manifest <absolute-directory>` verifies the
artifact read-only and prints only scope ids, plan hash, phase counts, blocked
reason counts, and invariant summaries.

### Files

Own `engram/repair/{apps,models,contracts,manifest,inventory,planner,guards}.py`,
`repair/migrations/0001_initial.py`, adjacent tests, the
`engram_plan_repair`/`engram_verify_repair_manifest` command modules/tests, and only repair-app
registration plus the false simulation default in `settings/settings.py`.

### RED Tests

1. Missing/mismatched scope fails before a domain query, artifact, or write.
2. Mirrored foreign anomalies yield target refs only and unchanged foreign digests.
3. Planning changes no domain, workflow, package, repair, or audit row count.
4. Fixed input yields identical hashes; newer rows obey the snapshot boundary.
5. Verification rejects phase reorder, duplicate/unknown actions, tampering, wrong hashes, and unsafe paths.
6. Sensitive-content scanning rejects forbidden keys and synthetic secrets.
7. Active-run constraints isolate projects; the guard rejects the default database even if enabled.
8. Forward/reverse migration is data-neutral, and pre-rename crash publishes no valid manifest.

### Gate

C10.1 merges only when a verifier can independently reconstruct every file
digest, the dry run is proven read-only, P13 has durable simulation schema, and
no state-changing entry point exists.

## C10.2 - Projection, Operational Work, And Coverage Repair

### Behavior

C10.2 adds the executor and the first four adapters. Simulation registration
loads a verified manifest into one `RepairRun` plus all planned `RepairBatch`
rows without touching domain state.

Projection actions call the CP4 consistency/rebuild primitive. Exact retrieval
documents are rebuilt before embeddings. Embedding absence creates or reuses
projection work; it never re-promotes a memory.

Lease reclaim calls CP2 claim/reclaim with the manifest subject and current
fence. Missing-work actions create exact session-generation, revision-impact,
or other upstream identities already authorized by CP2/CP7. They do not infer
completion from package rows or queue depth.

Coverage actions call CP3 deterministic continuation. They may create/reuse
chunk work or an explicit no-signal/no-input disposition. They never call a
provider inside the repair batch transaction and never fabricate coverage for
an observation.

### Files

Own `repair/{locks,executor}.py`, `repair/adapters/{base,projection,
operational,coverage}.py`, adjacent tests, `adapter_census_tests.py`, and the
simulation command/tests.

`engram_simulate_repair` requires an already created `engram_repair_sim_*`
database, enabled guard, verified manifest, explicit run id, and bounded
`--max-batches`. It has no database-selection option and cannot plan new work.

### RED, Replay, And Concurrency Tests

1. Duplicate manifest registration reuses run/batches; a competing active plan fails closed.
2. Racing executors produce one effect; lease reclaim increments the fence and rejects the stale owner.
3. Mid-batch failure rolls back effect/state/cursor/audit; post-commit death resumes at the next ordinal.
4. Full replay changes no domain/package count; drift pauses without substituting an entity.
5. Projection replay leaves one exact document and embedding-work identity.
6. Operational repair reclaims only expired leases, preserves attempts, and creates one exact-generation work/package identity without hiding newer input.
7. Oversized coverage resumes to non-overlapping complete dispositions; provider outage pauses without semantic rejection.
8. Every adapter preserves foreign counts/digests, and the census rejects direct semantic ORM mutation.

### Gate

After phase 4, synthetic missing projection, expired lease, missing work,
latest-failure, and uncovered-input counts are zero. P1-P5, P7, P13, and P14
must not regress; all other invariant results match the expected pre-candidate
state.

## C10.3 - Candidate Shadow And Bounded Semantic Canary

### Behavior

C10.3 adds phases 5 through 8 and the first canary evaluator. Every historical
proposal is reprocessed through the current CP5 evidence, policy, shortlist,
judge, and CP4 transition path. Existing confidence is an input to explanation,
never authority to promote.

Phase 5 is shadow-only and persists only repair batch/output digests plus
upstream shadow-decision evidence. Phase 6 may apply deterministic rejection in
the disposable target fixture while preserving source evidence. Phase 7 may
publish a new memory or create a non-destructive revision in the synthetic
canary. Phase 8 remains blocked until the fixture's frozen evaluation contract
passes conflict recall and the same run has a successful rollback proof.

The adapter creates/reuses CP5 decision work, then blocks until its persisted
shadow result exists; it never calls a provider inside a repair transaction.

Provider outage, malformed output, missing evidence, or threshold-contract
mismatch blocks the current action. None becomes `reject`, `keep_both`,
`merge`, `supersede`, or a human conflict.

### Canary Fixture Contract

Create
`apps/backend/engram/repair/fixtures/repair_release_canary_v1.json`. It contains
target and foreign projects with:

- deterministic noise and malformed projections;
- exact duplicates;
- supported non-destructive new claims;
- revision evidence that preserves prior history;
- near matches where vector score alone must not merge;
- one genuine contradiction with durable evidence on both sides;
- provider outage and malformed provider response;
- a destructive supersede candidate with preserved inverse transition;
- expected shadow reason, allowed phase, invariant delta, and semantic outcome
  for every item;
- the exact MCI evaluation-contract hash, never copied threshold values.

### Files

Own candidate adapter/tests, `repair/canary.py` and tests, the candidate fixture
and fixture tests, plus only phase 5-8 additions to executor/contracts.

### RED And Replay Tests

1. Whole-backlog shadow changes no candidate/memory/version/link/projection row.
2. High similarity alone cannot merge/supersede; deterministic reject preserves evidence and creates one audit.
3. Publish/revise uses CP4 atomically and leaves one coherent current projection.
4. Destructive execution requires rollback proof and accepted conflict recall.
5. Every CP4 boundary fault leaves old state or one complete transition; restart converges on one identity.
6. Provider outage/malformed output remains retryable with no semantic effect, and ordinary uncertainty never reaches humans.
7. Genuine contradiction creates one evidence-complete conflict while foreign digests stay unchanged.
8. Rollback restores the prior current version and preserves forward audit.

### Gate

The phase-8 gate requires P6-P9 and P12-P14 green in the synthetic canary, the
frozen candidate evaluation accepted, no ordinary inbox item, and successful
semantic rollback/replay evidence. No actual organization or project canary is
enabled.

## C10.4 - Temporal Baseline, Context Canary, And MCI-7A Simulation

### Behavior

C10.4 adds phases 9 through 12. It proves MCI-7A continuous operation against
accepted synthetic changes plus periodic coverage sweeps, but does not change
production defaults or add a live schedule.

Temporal baseline uses exact existing provenance to create/reuse normalized
path and symbol anchors. Every other code-sensitive memory receives an
explicit unanchored/unknown classification. Historical memory is never bulk
marked current merely because baseline planning completed.

Shadow revalidation creates/reuses CP7/CP8 work for the exact baseline revision.
It processes edit, rename, delete, unrelated change, duplicate event,
out-of-order event, provider outage, newer-head fencing, and revert fixtures.

Context-gate simulation authorizes first, applies temporal eligibility, then
ranks and packs. A request for accepted revision R while impact coverage is
behind R withholds code-sensitive memory and emits the stable coverage-lag
warning. Replay remains byte-stable for a compatible fingerprint.

Canary expansion is a pure state-machine simulation over fixture assignments:
`off -> shadow -> deterministic -> non_destructive -> temporal_gate ->
default_on`. Every step is organization/project scoped, evaluates current
P1-P15 plus the frozen MCI contract, and has `previous_state`. Any failed gate
returns immediately to `previous_state`. The simulator writes only repair
control/audit rows and fixture-domain effects already permitted by earlier
phases.

The periodic sweep calls the upstream MCI coverage reader and creates/reuses
normal revalidation work. CP10 adds no Beat key, queue, retry loop, or alternate
work identity.

### Files

Own temporal adapter/tests, `continuous_operation.py` and tests, the phase 9-12
canary extension/contracts, and `repair_release_temporal_v1.json`.

### RED, Sweep, And Canary Tests

1. Exact provenance yields one anchor; unsupported inference is unknown without a model call, and baseline never certifies history current.
2. Duplicate revisions/sweeps reuse work; unrelated changes make no provider/semantic call; rename updates without refutation.
3. Missing evidence remains retryable, newer head fences old work, and revert creates a new audited world state using preserved versions.
4. Coverage lag at revision R withholds before ranking; compatible replay stays byte-stable.
5. Canary transitions are deterministic from scoped metrics and threshold hash.
6. Any failed reliability/quality/autonomy/latency/cost gate blocks expansion and restores `previous_state`.
7. Simulated default-on is organization-scoped/reversible and foreign settings/digests remain unchanged.
8. Provider failure has no semantic/eligibility effect, and only genuine temporal conflict reaches humans.

### Gate

P1-P15 are healthy for the target fixture, accepted exceptions are empty, MCI
reliability/quality/autonomy/latency/cost evaluation passes, stale-memory
injection is within its frozen threshold, and simulated default-on rolls back
cleanly. The gate is evidence about disposable data, not rollout authority.

## C10.5 - Rollback Drill, Fault E2E, Fresh Clone, And Runbooks

### Disposable Backup And Restore Drill

The rehearsal creates two randomly suffixed databases matching
`engram_repair_sim_[0-9a-f]{16}` and
`engram_repair_restore_[0-9a-f]{16}`. It rejects every other database name.

Inside containers it migrates/seeds the source, records migration/invariant/
table inventory, creates a custom-format `pg_dump` outside the volume, records
size/hash without content/environment, restores into the empty restore DB,
reruns checks/inventory, compares semantic counts and row digests, passes the
verified evidence to planning, and cleans only validated disposable names.

The drill never presents its dump command as a production backup procedure.

### Repair Rollback Drill

Rollback proceeds in reverse applied-phase order and stops at batch boundaries.

- canary/context returns to `previous_state`;
- anchors stay as evidence unless CP7 removes only a derived duplicate;
- semantic/rejection inverses use CP4/CP5 services and preserved versions;
- coverage/work history stays append-only; projections rebuild from current
  versions; run/batch/audit rows remain `rolled_back`;
- a second rollback changes no domain row.

Snapshot restore is proven into a separate database. It is not used to erase
forward evidence in the source simulation database.

### Fault E2E Matrix

The CP10 harness maps existing F1-F19 to these repair-specific boundaries:

| Boundary | Injection | Required recovery |
|---|---|---|
| plan publication | kill before/after artifact rename | no valid partial artifact or one valid manifest |
| run claim | kill after lease/fence claim | expired lease is reclaimed once |
| batch transaction | raise after first domain call | complete rollback, cursor unchanged |
| batch commit | kill immediately after commit | resume starts at next batch |
| relay/broker | stop during newly created upstream work | logical work/package remain recoverable |
| worker claim | kill before and after CP2 claim | ready/reclaimed work, stale owner fenced |
| provider stage | outage and malformed response | retryable work, no semantic result |
| projection | fail embedding after exact document | exact document survives, one retry work |
| candidate transition | fault at every CP4 boundary | old state or one complete transition |
| scheduler sweep | stop between coverage pages | next sweep reuses identities |
| repository head | advance while revalidation runs | older worker cannot apply |
| context snapshot | kill during render/persist | retry is byte-stable or explicit conflict |
| rollback | kill after inverse batch commit | resume at next inverse batch, one inverse effect |

The workflow uses native `docker compose` steps to stop/restart disposable
services. Scenario preparation and assertions run through Python commands
inside the API/backend container. It reuses the current runtime durability job
for Rabbit/Beat/PID1 baseline rather than duplicating those checks.

After every fault, the harness replays the same manifest, evaluates P1-P15,
checks target effect identities, compares foreign row digests, and emits one
sanitized evidence record. Manual database edits are not an accepted recovery.

### Fresh-Clone Gate

The job starts from a clean Actions checkout with no reused virtualenv,
`node_modules`, database volume, generated config, simulation manifest, or
backup artifact. It records checkout SHA, Docker/Compose versions, and image
digests.

In order: start the existing Compose stack under a unique disposable project;
run migrations/checks; create source/restore DBs and fixtures; verify backup and
restore; plan twice at a fixed snapshot; verify/register; interrupt/resume every
phase; replay with zero extra effects; roll back twice; rerun invariant/canary/
scope/secret/digest checks; run the existing golden path; and clean only the
validated project, databases, volumes, and artifacts.

### Files

Own `scripts/e2e_repair_release{,_tests}.py`,
`.github/workflows/repair-release-e2e.yml`, `docs/repair-rehearsal-runbook.md`,
the CP10 link in `docs/release-runbook.md`, CP10 evidence names in the fault
matrix, and `repair-release-evidence-template.md`.

No C10.5 file lives under `deploy/**`.

## Audit And Evidence Ledger

Each lifecycle transition creates an `AuditEvent` atomically with control state.
Exact types are `RepairSimulationRegistered`, `RepairSimulationLeaseClaimed`, `RepairSimulationBatchApplied`, `RepairSimulationBatchBlocked`, `RepairSimulationPaused`, `RepairSimulationResumed`, `RepairSimulationSucceeded`, `RepairSimulationRollbackStarted`, `RepairSimulationBatchRolledBack`, `RepairSimulationRolledBack`, and `RepairCanaryEvaluated`.

Metadata is allowlisted: plan/batch/effect hashes, phase/ordinal, item and result
counters, stable reason, fence, threshold-contract hash, and previous/next
canary state. It excludes action bodies, entity content, provider output,
credentials, environment, paths outside the artifact basename, and raw
exceptions.

C10.5 evidence JSON records branch/start/end SHA; manifest/fixture/evaluation/
backup hashes; opaque source/restore ids; command, cwd, image digest, exit and
first failure; before/after P1-P15; phase/replay/rollback counters; fault
injection/expected/observed recovery; target/foreign digests; CI/review status;
and `production_mutation_performed: false`.

## File Ownership And Serial PR Spine

The five slices merge strictly C10.1 -> C10.2 -> C10.3 -> C10.4 -> C10.5.

| Slice | Exclusive mutable ownership | Consumed read-only |
|---|---|---|
| C10.1 | repair app/schema, contracts, manifest, inventory, planner, guards, planner/verifier commands, settings registration | invariant and upstream read services |
| C10.2 | locks, executor, base/projection/operational/coverage adapters, simulation command | CP2-CP4 services |
| C10.3 | candidate adapter, canary evaluator, candidate fixture, phase 5-8 contract additions | CP4/CP5 services/eval |
| C10.4 | temporal adapter, continuous-operation simulator, temporal fixture, phase 9-12 contract additions | CP7/CP8/MCI services |
| C10.5 | repair E2E script/workflow/tests, repair runbook, release-runbook link, fault evidence docs | existing Compose/golden/durability harness |

`apps/backend/engram/core/models.py`, transport-package internals, current
retrieval/curation/transition implementations, frontend, and deployment files
have no CP10 owner. If a consuming adapter exposes an upstream defect, fix it
in that checkpoint's module under a separately assigned owner before resuming
CP10.

## Required RED-To-GREEN Discipline

Each slice records a failing RED command/expected failure, implements only its
contract, runs focused GREEN plus scoped Ruff/format, then receives correctness
and simplicity review. Replay proves exact first-run effects and zero second-run
effects. Fault tests prove injection before recovery. Mocks may isolate adapter
unit tests, but cannot turn a missing upstream relation green; acceptance uses
PostgreSQL and real merged services in the backend container.

## CI Commands

Run focused tests sequentially inside the Compose backend container:

```text
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec \
  "poetry install --no-interaction --no-root --with dev && python -m pytest engram/repair/models_tests.py engram/repair/manifest_tests.py engram/repair/inventory_tests.py engram/repair/planner_tests.py engram/repair/guards_tests.py -q"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \
  "python -m pytest engram/repair/executor_tests.py engram/repair/adapters/projection_tests.py engram/repair/adapters/operational_tests.py engram/repair/adapters/coverage_tests.py engram/repair/adapter_census_tests.py -q"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \
  "python -m pytest engram/repair/adapters/candidate_tests.py engram/repair/canary_tests.py engram/repair/adapters/temporal_tests.py engram/repair/continuous_operation_tests.py engram/repair/fixtures_tests.py -q"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \
  "python -m pytest /workspace/scripts/e2e_repair_release_tests.py -q"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec \
  "python manage.py check && python manage.py makemigrations --check --dry-run && ruff check engram/repair && ruff format --check engram/repair"
```

The workflow then runs the disposable backup/restore, replay, fault, rollback,
and fresh-clone scenarios. Existing required `Backend`, `Compose E2E`, plugin
E2E, frontend, and CodeQL jobs remain required according to repository rules.

Host-side checks are limited to Git/read-only/native tooling:

```text
git status --short
git diff --check
git diff --name-only -- deploy
docker version
docker compose version
```

The expected `git diff --name-only -- deploy` output is empty. If Docker or
Compose is unavailable, record the exact command, exit code, and first decisive
environment failure; do not replace container tests with host Python tests.

## CI Admission Gates

- C10.1: manifest/tamper/read-only/scope/schema/disabled-guard gates pass.
- C10.2: phase 1-4 replay/concurrency/fence and targeted invariants pass.
- C10.3: shadow/semantic fault/conflict/inverse/evaluation gates pass.
- C10.4: temporal/revert/context/canary and full P1-P15/MCI gates pass.
- C10.5: clean checkout, restore, faults, replay, rollback, golden path, redaction, and evidence pass.
- Manifest, fixtures, evidence, runbook, and fault matrix use identical phase/reason names.
- Required CI is current-head green; no job is accepted from an older SHA.

## Rollback Of The Implementation

The feature is inert by default with no public executor. Code revert removes
Python/CI entry points; empty additive tables may remain, while schema reversal
is tested only on disposable DBs. For an existing run, stop at a boundary,
expire the lease, perform audited rollback, verify P1-P15/digests, and retain
run/batch/audit plus all evidence/history/versions/lineage/conflicts. A bad
canary evaluator is disabled and reverted; no production assignment exists.

## Non-Goals

- No production/staging/shared/external repair, inventory, backup, restore,
  canary/default-on, deployment, SSH, Kubernetes write, or D2.
- No generic repair HTTP API, admin UI, Celery executor, Beat schedule, or
  always-on repair daemon.
- No outbox replacement/queue-depth progress inference, provider prompt,
  curation/retrieval/semantic/temporal/context/conflict algorithm.
- No blind promotion of historical proposals and no confidence-only merge or
  supersede.
- No inferred anchors beyond exact existing path/symbol provenance.
- No evidence/version/lineage mutation to make invariants pass, and no MCI-7B
  preview/time-travel/contracts/anchor expansion.
- No performance tuning beyond measuring the CP9 primitives against the frozen
  evaluation contract.
- No edits to `deploy/**`, private deployment scripts, release tags, image
  publication, plugin publication, or changelog versioning.

## Stop Conditions

Stop the current slice and report exact SHA, diff, command, exit code, and first
decisive failure when:

- an upstream capability/MCI contract is absent, or planning lacks one scope and
  repeatable-read snapshot;
- an artifact leaks content/secret, an adapter writes semantic ORM directly or
  creates a second workflow identity, or a provider call holds a batch lock;
- active-run uniqueness, advisory lock, lease, or fence cannot be proven;
- a phase would run out of order, skip blocked work, or change batch size on
  resume;
- a destructive action lacks accepted evaluation and an exercised inverse;
- target invariants regress, foreign digests change, injection is unproven, recovery needs SQL, or cleanup scope is ambiguous;
- work requires `deploy/**`, SSH, production state, or D2;
- the same verification gate fails twice for different reasons.

## Checkpoint Acceptance

Evidence completes only after ordered C10.1-C10.5 merges, container RED/replay/
fault and current-HEAD fresh-clone gates, P1-P15/MCI-7A, idempotent rollback,
runbooks/ledger, and `production_mutation_performed: false`. This proves only
readiness for a separately authorized production plan; it grants no rollout.
