# Checkpoint 4 Atomic Memory Transitions And Rebuildable Projections

Date: 2026-07-11

Status: focused implementation specification for C4.1-C4.3

Roadmap gate: Checkpoint 4 -- Atomic Memory Transitions And Rebuildable Projections

Depends on:

- `docs/superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md`;
- `docs/superpowers/specs/2026-07-10-checkpoint-0-reliability-contract.md`;
- `docs/reliability/memory-loop-invariants.md` and `memory-loop-fault-matrix.md`;
- merged Checkpoints 1-3, including fenced logical work and append-only
  `MemoryCandidateSource` provenance.

## Goal And Acceptance Boundary

Every post-cutover publishable-memory change is one short PostgreSQL
transaction. Candidate disposition, memory state, relational current version,
provenance, exact retrieval representation, lineage/conflict evidence, one
semantic audit identity, and required embedding work commit together or all
roll back.

Embedding computation remains outside that transaction. Its absence may reduce
semantic recall, but cannot create, advance, refute, restore, merge, or
supersede memory.

For `transition_contract_version=1` state:

- one promoted candidate resolves to one same-scope transition, memory,
  version, provenance set, exact document, audit, and embedding-work identity;
- one memory has one relational current-transition pointer; legacy integer,
  body, and state fields are compatibility mirrors and agree with it;
- every destructive or restoring decision locks and rechecks all affected
  current versions before commit;
- merge/supersede name exact source and result versions, not only string ids;
- unresolved conflict evidence survives TTL, retries, generic link removal,
  and restart until explicit resolution;
- exact retrieval works at semantic commit; embedding is hash-fenced,
  observable, and automatically retryable;
- console, API, curator, digest, import, feedback, and late-source adapters do
  not write publishable state directly;
- consistency reporting separates repairable projections from impossible
  authoritative combinations.

Contract-version-0 history remains readable and explicitly reported. C4 does
not invent its missing transitions to make global invariants green.

## Non-Goals

- No generic transition/state-machine framework, event-sourcing platform,
  dynamic participant graph, or callback DSL.
- No C5 decision policy/inbox behavior, C8 temporal validity, or C9 retrieval
  optimization.
- No second queue, outbox, lease, retry, or repair-run authority.
- No exactly-once provider billing and no provider call under semantic locks.
- No automatic historical promotion, rejection, merge, supersession,
  refutation, or conflict resolution.
- No deletion/rewrite of source evidence, versions, transitions, audits,
  lineage, or resolved conflicts.
- No alternate vector store, production mutation, deployment, SSH, or D2 work.

## Current Boundaries To Replace

- `PromoteMemoryCandidate` commits candidate/memory/version, then indexes;
  exact document and curator audit can be missing after promotion.
- `IndexMemoryVersion` combines deterministic exact writes with a provider call.
- curator supersession promotes a winner and later stales the loser.
- conflict evidence is split between mutable candidate JSON and a deletable
  string-target `MemoryLink`; TTL calls `clear_candidate_conflict_links()`.
- console edit/supersede/reject/archive/restore and feedback own direct writes.
- digest and import construct independent memory/version/document/audit shapes.
- `Memory.current_version` is an integer, not relational authority.
- P7 lacks uniform provenance/audit identity; P8/P9 have no exact relation.

Each item receives a failing regression before its producer changes.

## Chosen Design And Authority

Use typed services backed by three narrow additive records:

1. `MemoryTransition`: immutable semantic/audit identity and current-pointer target.
2. `MemoryVersionSource`: normalized provenance for one memory version.
3. `MemoryConflict`: durable open/closed candidate-memory conflict.

`MemoryTransition` stores one affected memory and one result memory. Merge and
supersede are binary; multiple sources apply deterministic pairwise
transitions. This covers current and C5 decisions without a generic participant
framework. JSON-only audit/link retrofits are rejected because they cannot
protect relational scope, version, deletion, or replay identity.

PostgreSQL owns semantic state, exact projection, history, provenance,
conflicts, and embedding intent. `django-celery-outbox` remains transport
authority. CP2 remains claim/lease/fence/retry authority. The semantic
transaction performs only scoped reads, row locks, canonical hashing, bounded
database writes, and package-backed enqueue.

The exact document is commit-required state but deterministically rebuildable
from preserved rows. Embeddings are asynchronous derived state and usable only
when fenced to the exact projection hash.

## Additive Data Contract

### Fixed Choices

```text
MemoryTransitionType:
  promote, publish_digest, revise, attach_source, merge, supersede,
  mark_stale, refute, restore, archive, conflict_open, conflict_resolve

MemoryConflictResolution:
  publish_candidate, merge_candidate, supersede_memory, reject_candidate

WorkflowWorkType: memory_embedding
WorkflowSubjectType: retrieval_document
```

Operational ready/running/retry state remains in CP2 models.

### Memory Pointer

Add to `Memory`:

| Field | Type | Contract |
|---|---|---|
| `transition_contract_version` | positive small integer, DB default 0 | 0 pre-cutover; 1 C4-owned |
| `current_transition` | nullable FK `MemoryTransition`, `PROTECT` | required for version 1 |

A check allows version 0 with a nullable pointer or version 1 with a non-null
pointer. New-memory transactions insert version 0 temporarily, build the full
chain, then set version 1 before commit. For version 1, the service and P7
verify transition memory/version, scope, and mirrored
`current_version/body/status/stale/refuted`. Cross-row equality stays out of
triggers.

### MemoryTransition

Add immutable `MemoryTransition`:

| Field | Type and contract |
|---|---|
| `id` | UUID primary key allocated before audit |
| `organization`, `project`, nullable `team` | `PROTECT` scope FKs |
| `transition_type` | fixed choice above |
| `idempotency_key` | char(255), stable producer identity |
| `request_fingerprint` | canonical command SHA-256 |
| `candidate` | nullable `PROTECT` FK, required for candidate decisions |
| `memory` | `PROTECT` FK to created/affected memory |
| `from_version` | nullable `PROTECT` FK to prior current version |
| `to_version` | `PROTECT` FK to affected memory's post-state content |
| `result_memory`, `result_version` | `PROTECT` FKs to surviving/published result |
| `exact_document` | `PROTECT` FK to affected memory's post-state document |
| `result_exact_document` | `PROTECT` FK to result memory's post-state document; may equal `exact_document` |
| `embedding_work` | nullable one-to-one `PROTECT` FK for a newly generated active result projection |
| `semantic_link` | nullable one-to-one `PROTECT` FK to `MemoryLink` |
| `audit_event` | one-to-one `PROTECT` FK to uniform `AuditEvent` |
| `provenance_hash` | SHA-256 of ordered version-source identities/digests |
| `created_at` | insert timestamp; no update timestamp |

Constraints:

- unique `(organization, project, idempotency_key)`;
- request/provenance hashes are lowercase 64-character hex;
- `from_version` is null only when the affected memory is created by this
  transition; otherwise it names its exact prior current version;
- candidate required for `promote`, `attach_source`, `conflict_open`, and
  `conflict_resolve`;
- semantic link required for existing-memory `merge`, `supersede`, and
  `conflict_open`; candidate merge has relational version sources instead, and
  `conflict_resolve` carries a link when its selected outcome creates lineage;
- embedding work is required whenever the command creates a new active result
  projection generation; unchanged or inactive result projections reuse/null it;
- conditional unique candidate across terminal `promote` and
  `conflict_resolve` types;
- indexes on scope/time, scope/type/time, candidate, memory, and result memory;
- persisted rows reject update; no production delete service exists.

The typed service validates same-scope FKs, both document owners, work subject,
result pointers, and type-specific equality. A current pointer may reference a
transition through its `memory/to_version` side or, for atomic replacement,
its `result_memory/result_version` side. P7/P8 repeat these predicates.

### MemoryVersionSource

Add immutable `MemoryVersionSource`:

| Field | Type and contract |
|---|---|
| `organization`, `project` | `PROTECT` FKs matching output/source |
| `memory_version` | `PROTECT` FK to published/derived version |
| `candidate_source` | nullable `PROTECT` FK to CP3 `MemoryCandidateSource` |
| `source_memory_version` | nullable `PROTECT` FK for revise/digest/merge |
| `source_content_hash` | SHA-256 of exact preserved source |
| `created_at` | immutable insert time |

Exactly one source FK is non-null. Add conditional unique constraints per
source kind, reject self-reference, and index scope/output version.

Promotion copies every locked `MemoryCandidateSource` for the candidate.
Digest publication uses only frozen source versions from its work snapshot;
revision uses the prior current version. Import must materialize a candidate
source first. Empty provenance fails before semantic writes. Existing
`MemoryVersion.source_observation/source_metadata` are compatibility mirrors.

CP3 locks the candidate before appending a source, preventing a source-set
phantom during promotion. New evidence after terminal work creates a new CP3
decision-work generation without mutating the old one. If the candidate is
already promoted, `AttachPromotedCandidateSource` settles that new generation
by adding one `MemoryVersionSource`, advancing only the provenance
transition/projection generation for the same version, and creating embedding
work; it never repeats promotion.

### MemoryConflict

Add `MemoryConflict` with immutable open fields and a single close operation:

| Field | Type and contract |
|---|---|
| scope FKs | organization/project/nullable team, `PROTECT` |
| `candidate`, `memory`, `memory_version` | exact compared rows, `PROTECT` |
| `semantic_link` | one-to-one `MemoryLink`, `PROTECT` |
| `opened_transition` | one-to-one `conflict_open` transition, `PROTECT` |
| `evidence_hash` | canonical comparison/evidence SHA-256 |
| `resolved_transition` | nullable transition FK, `PROTECT` |
| `resolution`, `resolved_at` | all empty while open; all set on close |
| `created_at` | open time |

Unique `(candidate, memory)`; one candidate may retain multiple independently
supported conflicts. The compatibility link remains
`CONFLICTS_WITH -> candidate:<uuid>`, but current conflict reads require an
unresolved `MemoryConflict`. Protected transition references prevent TTL,
generic delete, or cascade removal. Resolution retains the link as immutable
history.

### Retrieval Projection Fence

Add to `RetrievalDocument`:

| Field | Contract |
|---|---|
| `projection_contract_version` | positive small integer, 0 legacy/1 C4 |
| `exact_projection_hash` | required SHA-256 for version 1 |
| `embedding_projection_hash` | exact hash consumed by current vector, blank if absent |
| `embedding_projected_at` | set only with a current embedding |

Embedding state is fully absent or has reference, vector, matching hash, and
time. With pgvector, JSON and pgvector values agree. Legacy version-0 rows
remain valid during expand.

The exact hash covers transition id, memory/version ids and content hash,
title/body hash, scope/team/visibility, status flags, ordered provenance ids
and hashes, file paths, symbols, exact terms, source observation ids, and
full-text hash. It excludes embedding/provider output.

### Embedding Work

Add one work/subject pair with exact snapshot:

```json
{
  "schema": "memory_embedding/v1",
  "retrieval_document_id": "<uuid>",
  "memory_id": "<uuid>",
  "memory_version_id": "<uuid>",
  "exact_projection_hash": "<sha256>"
}
```

Subject id equals the same-scope document id; creation derives team. Identity
uses exact hash, not policy. Each attempt resolves current embedding policy and
records the actual provider/policy in `ProviderCallRecord`. Missing policy
leaves visible retryable/blocked CP2 work; intent is never suppressed.

## Projection Interfaces

Create `engram/memory/projections.py`:

```python
build_exact_memory_projection(*, memory, version, transition_id, sources) -> ExactMemoryProjection
write_exact_memory_projection(*, memory, version, transition_id, sources) -> RetrievalDocument
create_embedding_work_and_signal(*, document) -> tuple[WorkflowWork, bool]
complete_embedding_projection(*, claim, expected_projection_hash, embedding,
                              provider_call_id, now) -> RetrievalDocument | None
```

`ExactMemoryProjection` contains `document_values` and
`exact_projection_hash`. The writer requires an active transaction and locked
memory/version, performs no provider call, creates/updates the one-to-one
document, clears embedding fields, and marks prior documents stale.

The worker uses CP2 `claim_work()` before the provider call. After it,
completion starts a short transaction with `lock_work_fence(claim, now)`
(work then run), locks document/memory, validates exact hash/current
transition/version/active state, writes both vector forms, then calls
`finish_work_claim()`. A mismatch discards the vector and finishes that work as
`completion='product_no_signal'`, with structured reason
`projection_superseded`; provider failure uses `fail_work_claim()`. A stale
owner cannot write or resolve. Initial dispatch uses
`queue_work_attempt(work.id, now, origin='memory_transition')` inside the
semantic transaction.

Refactor `IndexMemoryVersion` into a compatibility adapter over the exact
builder/work producer. No C4 semantic writer may invoke its current
provider-mixing behavior.

## Typed Transition Interfaces

Create `engram/memory/transitions.py`; expose no generic
`apply_transition(type, payload)`.

```python
TransitionRequest(scope, idempotency_key, actor_type, actor_id, capability,
                  request_id, correlation_id, reason, origin)
CandidateFence(candidate_id, candidate_content_hash, evidence_manifest_hash)
MemoryFence(memory_id, current_transition_id, current_version_id, state_hash)
MemoryTransitionResult(transition, memory, memory_version,
                       retrieval_document, embedding_work, duplicate)

PromoteMemoryCandidate.execute(PromoteMemoryCandidateInput) -> MemoryTransitionResult
PublishDigestMemory.execute(PublishDigestMemoryInput) -> MemoryTransitionResult
ReviseMemory.execute(ReviseMemoryInput) -> MemoryTransitionResult
ReviseMemoryFromCandidate.execute(ReviseMemoryFromCandidateInput) -> MemoryTransitionResult
AttachPromotedCandidateSource.execute(AttachPromotedCandidateSourceInput) -> MemoryTransitionResult
MergeMemoryCandidate.execute(MergeMemoryCandidateInput) -> MemoryTransitionResult
MergeMemories.execute(MergeMemoriesInput) -> MemoryTransitionResult
SupersedeMemoryWithCandidate.execute(SupersedeMemoryWithCandidateInput) -> MemoryTransitionResult
SupersedeMemories.execute(SupersedeMemoriesInput) -> MemoryTransitionResult
MarkMemoryStale.execute(MemoryStateInput) -> MemoryTransitionResult
RefuteMemory.execute(MemoryStateInput) -> MemoryTransitionResult
RestoreMemory.execute(MemoryStateInput) -> MemoryTransitionResult
ArchiveMemory.execute(MemoryStateInput) -> MemoryTransitionResult
OpenMemoryConflict.execute(OpenMemoryConflictInput) -> MemoryConflict
ResolveMemoryConflict.execute(ResolveMemoryConflictInput) -> MemoryTransitionResult
```

Every input composes `TransitionRequest` and relevant fences. Candidate merge/
revision names the target and resulting body; existing-memory lineage names
source and result; candidate supersession names loser and candidate. Digest
names exact source versions and work id. Conflict open names candidate,
compared version, evidence hash, and redacted reason. Conflict resolution names
the complete ordered open-conflict ids/fences, fixed outcome, selected target
where required, and outcome content; set drift is a stale decision.

Stable idempotency keys:

- `candidate:<candidate_id>:settle:v1`;
- `candidate-source:<source_id>:attach:v1`;
- `decision-work:<work_id>:settle:v1`;
- `digest-work:<work_id>:publish:v1`;
- `request:<request_id>:<action>:<subject_id>:v1`;
- `request:<request_id>:feedback:<action>:<memory_id>:v1`.

The service hashes the canonical command as `request_fingerprint`. Same key and
fingerprint returns the stored result. Same key with different input raises
`idempotency_collision` without writes.

C5 outcome mapping is fixed:

- `publish_new` -> `PromoteMemoryCandidate`;
- `merge_evidence` -> `MergeMemoryCandidate`, creating a target version whose
  sources are prior target version plus all candidate sources;
- `revise_memory` -> `ReviseMemoryFromCandidate`, also creating a version but
  retaining the distinct revise audit decision;
- `supersede_memory` -> `SupersedeMemoryWithCandidate`;
- ordinary `reject_candidate` remains C5 candidate-decision disposition;
  unresolved-conflict rejection must use `ResolveMemoryConflict`.

Conflict resolution fences and closes the complete locked open set atomically:

- `publish_candidate` creates a second active memory and leaves compared
  memories active;
- `merge_candidate` creates a new version on one selected compared memory and
  points `candidate.promoted_memory` to it; no second memory is created;
- `supersede_memory` creates the candidate result and stales/links one selected
  compared memory; the explicit resolution leaves other compared memories active;
- `reject_candidate` rejects the candidate and leaves compared memories unchanged.

## Locking, Fencing, And Atomic Shape

Embedding/judge calls use immutable snapshots outside the semantic transaction.
Commit uses one global lock order:

1. optional owning CP2 claim through `lock_work_fence()` (work then run);
2. candidate rows in UUID order;
3. affected memory rows in UUID order;
4. declared current versions in UUID order;
5. conflicts, then exact documents, in UUID order.

Every candidate-source appender also locks its candidate first. The transition
reloads by organization/project, validates team, and recomputes candidate
content hash, ordered CP3 `evidence_manifest_hash`, and memory fences after
locking. Mismatch raises
`MemoryTransitionError(code='stale_decision', retryable=True)` before writes;
the decision work reruns comparison. Batch repair alone may use `skip_locked`.

Candidate hashes cover scope/team, status, promoted memory, content/title/body,
evidence, and ordered source rows. Memory state hashes cover current
transition/version, title/body, status/stale/refuted, visibility, and team.

One publishable transaction performs:

1. scope resolution, locks, recheck, idempotent-result/collision check;
2. create/reuse memory/version and complete ordered version-source set;
3. allocate transition/audit ids and write affected and result exact projection
   generations (the same row when affected and result memory are identical);
4. create embedding work and package signal when result is active;
5. create protected semantic link/conflict when required;
6. create one `AuditEvent(event_type='MemoryTransitionCommitted')`;
7. create `MemoryTransition` referencing every authoritative row;
8. update memory pointer/version marker/mirrors and candidate/conflict outcome;
9. resolve owning decision/digest work inside the same outer transaction.

Audit metadata schema `memory_transition/v1` contains transition type/id,
origin, scoped entity/version/link/work ids, hashes, redacted reason, and scope
filters. It excludes body, vectors, raw provider output, credentials, and raw
idempotency keys. Denied attempts create only normal denied audits.

Fault before commit means full rollback. Fault after commit means one complete
chain plus retryable embedding work.

## Lineage And Conflict Rules

- candidate `supersede` creates the candidate result and exact projection while
  staling only the loser and its exact document under the same transition id;
  existing-memory supersede reuses the already coherent result projection.
- `merge` uses `NARROWED_BY`, preserves both histories, and includes source
  versions in relational provenance; candidate merge creates a new version on
  its target and needs no fabricated memory-to-itself link.
- refute changes status/flag/document together while retaining content history.
- restore clears inactive flags, creates a new projection generation and new
  embedding work even when content version is unchanged.
- archive explicitly makes the exact document non-retrievable.
- conflict open does not advance memory current pointer because publishable
  state is unchanged; open transition/link/conflict still commit together.
- resolution locks every unresolved row for that candidate, closes them with
  one outcome, retains links, and applies one terminal result atomically.

Generic link POST/DELETE accepts only file, symbol, commit, and issue links.
Lineage/conflict types raise `semantic_link_requires_transition`; protected
rows map database `PROTECT` to a stable conflict response.

TTL excludes candidates with unresolved `MemoryConflict`. Ordinary reject
cannot clear conflict evidence; only `ResolveMemoryConflict` settles it.

## Serial Delivery Spine

### C4.1 -- Atomic Promotion

Land additive schema, exact/embedding split, embedding work, promotion, and
late-source attachment. No other semantic writer changes yet.

Gate: F14/F15 rollback at every boundary; concurrent duplicate promotion has
one chain/signal; exact retrieval works at commit; no provider call holds a
semantic lock; replay/fence/collision/scope controls pass; late source creates
one provenance-only transition without reopening decision work; version-1 P7
is healthy.

### C4.2 -- Atomic Lineage And Conflict Durability

Land remaining typed services, sorted multi-memory locking, protected links,
`MemoryConflict`, conflict-aware TTL/link API, then move curator paths.

Gate: F17 leaves old or complete lineage; reverse-order concurrency avoids
deadlock and only compatible fences commit; refute/restore and
supersede/restore serialize; conflict survives retry/restart/TTL and resolves
once; source history is undeletable; version-1 P8/P9 are healthy.

### C4.3 -- Writer Convergence And Repair

Cut remaining writers, add census, consistency report, exact rebuild,
embedding reconciliation, invariant evolution, and scoped commands.

| Current surface | C4 owner |
|---|---|
| public/console edit | `ReviseMemory` |
| console narrow/supersede | `MergeMemories` / `SupersedeMemories` |
| reject/archive/restore | `RefuteMemory` / `ArchiveMemory` / `RestoreMemory` |
| feedback stale/refuted | `MarkMemoryStale` / `RefuteMemory` |
| curator outcomes | typed transition/conflict services |
| import promotion | `PromoteMemoryCandidate` |
| CP3 late candidate source | `AttachPromotedCandidateSource` |
| daily/weekly publication | `PublishDigestMemory` inside work completion |
| index/reembed | exact builder and fenced embedding work only |

`MemoryReviewExample` remains supplemental and shares the outer transaction.
Adapters preserve response keys and may add `transition_id`; they create no
second semantic audit.

Gate: writer census has no bypass; all writers produce the uniform relation;
projection rebuild is scoped/idempotent; missing embedding cannot repromote;
CP4's P13 projection subset passes while global historical P13 stays open.

## Consistency And Projection Repair

Create `engram/memory/consistency.py`:

```python
MemoryConsistencyReporter.execute(ConsistencyReportInput) -> ConsistencyReport
RebuildMemoryProjections.execute(RebuildProjectionInput) -> RebuildProjectionResult
```

Input requires resolved scope, one project, aware `as_of`, deterministic
`after_id`, sample limit at most 20, and batch at most 200. Rebuild defaults to
dry-run and accepts only `exact` or `embedding`.

Stable issue codes:

- `candidate_transition_missing_or_mismatched`;
- `current_transition_missing_or_mismatched`;
- `current_version_pointer_mismatched`;
- `version_provenance_missing_or_mismatched`;
- `transition_audit_missing_or_mismatched`;
- `lineage_link_missing_or_mismatched`;
- `conflict_relation_missing_or_mismatched`;
- `conflict_resolution_incomplete`;
- `exact_projection_missing_or_mismatched`;
- `embedding_projection_missing` / `embedding_projection_stale`;
- `legacy_transition_observability_missing`.

Classify each as `report_only`, `rebuild_exact`, or `enqueue_embedding`. Exact
repair requires a coherent authoritative chain and changes only document
fields. Embedding repair creates/reuses work/signal. Report-only never mutates
semantic rows.

Commands:

```text
engram_memory_consistency --organization <uuid> --project <uuid> --limit 20
engram_rebuild_memory_projections --organization <uuid> --project <uuid>
  --kind exact|embedding --dry-run|--apply --after-id <uuid> --batch-size <1..200>
```

No cross-organization mode. Output gives deterministic counts/capped ids/next
cursor/changed/skipped. Each row is locked, rechecked, and committed alone.
CP10 owns persistent multi-batch repair-run history.

## Invariant Evolution

P7 version-1 health requires exactly one matching terminal candidate
transition; coherent transition/current pointer/version/provenance/document/
audit/work; mirrored state and exact hash agreement. Embedding absence is not a
P7 violation but must have visible work or a stable operational reason.

P8 requires exact from/to/result versions, protected typed link where needed,
one audit, preserved source rows, and current-pointer update for every state
change. P9 requires every conflict link to have one open transition/conflict,
unresolved evidence to remain protected, and resolved rows to name one terminal
resolution transition.

Structural checks still cover all memories. Version-0 rows report
`legacy_transition_observability_missing`, never healthy. Merge gate requires
P7/P8/P9 healthy for version 1 and zero new version-0 writes after writer drain;
global historical closure belongs to CP10.

## Required RED, Crash, And Concurrency Tests

### C4.1

1. Fault after memory, version, source, exact document, audit, work/package,
   transition, and candidate/pointer writes leaves proposed input and no chain.
2. Suppressed post-commit activity still leaves exact recall and one work/signal.
3. Embedding failure/recovery writes one vector without a second promotion.
4. Two PostgreSQL threads promote one candidate into exactly one chain.
5. Idempotent replay succeeds; fingerprint collision and stale fence write nothing.
6. Candidate source appended before lock joins promotion; appended after
   settlement attaches once without reopening terminal work.
7. Foreign scope fails before semantic or package writes.
8. Exact hash is deterministic and changes for every named input.

### C4.2

1. Fault at each promotion/supersession boundary leaves old or complete state.
2. Concurrent A-to-B/B-to-A avoids deadlock and only one compatible fence commits.
3. A comparison against version N cannot mutate N+1.
4. Refute/restore and supersede/restore yield one coherent serialized final state.
5. Merge preserves both versions and relational provenance.
6. Conflict-open fault leaves none or all link/conflict/audit/transition rows.
7. Duplicate open/restart is idempotent; TTL/reject/link delete preserve it.
8. Resolution closes all candidate conflicts and applies one outcome atomically.
9. Foreign-scope link/conflict cannot satisfy or mutate target P8/P9.

### C4.3

1. Every adapter yields the same transition/audit/projection shape and outer rollback.
2. AST/callsite census rejects direct production writes outside
   `transitions.py`, `projections.py`, and projection-only `consistency.py`.
3. Exact dry-run is inert; apply repairs only projection; rerun finds no drift.
4. Authoritative mismatch is report-only and never semantically repaired.
5. Concurrent embedding reconciliation creates one work/signal.
6. Late vector for an old hash is discarded and only old work is superseded.
7. JSON/pgvector mismatch is reported and repaired by one fenced result.
8. Migration forward/reverse preserves legacy; constraints reject invalid v1.
9. Compose E2E kills after semantic commit/before embedding, proves exact recall,
   restarts, then observes one current vector and no duplicate transition.

## Migrations, Cutover, And Rollback

Use expand, writer cutover, then projection upgrade:

1. Add models, nullable pointers, version defaults, projection fields, indexes,
   and legacy-compatible checks; existing rows stay version 0.
2. Deploy dual-version readers/reporters and prove migrations in Compose/Postgres.
3. Deploy typed services/adapters disabled; drain every old API/worker/beat/
   import/console revision and open transaction.
4. Enable C4 writers for a bounded organization/project canary. Any new
   version-0 publishable write is a stop condition.
5. Dry-run exact repair; apply only coherent `rebuild_exact`; enqueue embeddings
   only for coherent active documents.
6. Expand after clean P7/P8/P9 through restart and broker/provider outages.

No migration invents semantic outcomes or historical audits. A legacy conflict
may be protected only by a separately reviewed deterministic migration when
same-scope candidate, memory, exact target, and matching conflict evidence all
agree; ambiguity stays report-only.

Rollback rules:

- before version-1 activation, revert writers/additive schema normally;
- after activation, disable new decision behavior and embedding workers first;
  exact retrieval remains available;
- never roll back to code that directly mutates version-1 memory; use a forward
  compatibility fix retaining transition services/schema;
- leave embedding work retryable and never convert dependency failure to
  semantic rejection;
- reverse schema only when no version-1 transition exists;
- never delete versions, sources, transitions, audits, links, conflicts,
  provider calls, or evidence.

## Files And Ownership

C4.1 owner: `core/models.py`, one additive migration/model/migration tests,
`memory/projections.py`, promotion/attachment slice of
`memory/transitions.py`, promotion adapters/task/work tests, and P7 tests.

C4.2 owner: remaining `memory/transitions.py`, `memory/curation.py`,
`memory/conflict_links.py`, `memory/candidate_ttl.py`, link guards, and P8/P9
tests.

C4.3 owner: `memory/consistency.py`, two commands, console/feedback/version/
digest/import adapters, context indexing adapter/reembed, writer census,
metrics, invariant docs, and fault evidence.

`core/models.py`, migrations, `transitions.py`, `projections.py`, and
`invariant_queries.py` each have one active writer. Adapter work begins only
after its typed interface freezes.

## Verification And CI

All Python, Django, worker, migration, and E2E verification runs inside the
repository Compose containers. Each slice records exact commands/exit codes/
counts, branch/start/end SHA, CI links/conclusions, and risks.

Required per slice:

- focused RED then GREEN on PostgreSQL for named crash/concurrency cases;
- affected memory/context/search/console/import/digest/task/invariant tests;
- `makemigrations --check --dry-run`, migrate, allowed reverse, fresh apply,
  and Django system check;
- Ruff check/format, `git diff --check`, and repository quality hook;
- writer census, independent correctness/security review, Karpathy simplicity
  review, and adversarial committed-range review;
- C4.3 Compose fault E2E.

CI fails on provider calls inside atomic blocks, semantic writer bypass,
incomplete version-1 relations, stale-fence commit, or cross-scope repair.

## Checkpoint Gate And Stop Conditions

C4 closes only after C4.1-C4.3 merge serially; F14-F17 and the CP4 scope
control pass; all new publishable writes are version 1; cohort P7/P8/P9 and the
projection subset of P13 pass; exact recall is commit-synchronous; embeddings
are fenced/retryable; every writer emits one uniform transition audit;
conflicts survive until explicit resolution; only projection drift is
auto-repaired; container checks/reviews/CI are green.

Stop before implementation or the next slice if:

- CP2 lacks stable claim/fence/completion or CP3 lacks immutable candidate sources;
- an exact operation needs a generic participant framework;
- provider/unbounded computation would run under locks;
- a writer cannot atomically include transition, exact document, audit,
  embedding intent, and owning work completion;
- candidate/memory/source comparison cannot be fenced;
- rollback permits old code to mutate version-1 state;
- repair must choose a semantic winner or invent history;
- durability requires deleting evidence;
- work expands into C5/C8 policy, production mutation, deployment, secrets,
  SSH, or release scope.
