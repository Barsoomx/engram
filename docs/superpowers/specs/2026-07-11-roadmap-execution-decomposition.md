# Autonomous Memory Loop Execution Decomposition

Date: 2026-07-11

Status: execution authority for roadmap specification and implementation work

## Objective

Turn the autonomous-memory-loop roadmap into small reviewable product-code
slices, specify later checkpoints in parallel, and execute the active checkpoint
with disjoint worktrees and TDD. The campaign optimizes for working code and
measurable invariant progress, not infrastructure investigation or review
ceremony.

## Binding Scope

- Product code in Checkpoints 1 through 10 is in scope.
- Production hosts, SSH, deployment, runtime storage migration, D2 bootstrap,
  production queue state, and production repair are out of scope.
- `django-celery-outbox` remains the only transport authority.
- Checkpoint merge order remains strict. Later checkpoints may be specified and
  reviewed early, but their code does not merge before their dependencies.
- The main agent is the only orchestrator and Git owner. Subagents never spawn
  subagents or perform branch operations.
- Parallel implementation is allowed only in isolated worktrees with disjoint
  owned files and a frozen shared interface. Two tasks that need the same
  central file are serial.
- Python, CLI, plugin, backend, and E2E tests run in containers. If the normal
  local container gate is unavailable for environmental reasons, the branch is
  pushed as a draft and GitHub Actions is the verification environment; local
  environment debugging does not become a project.
- Each coherent slice gets one independent correctness review and one
  simplicity review. Another loop occurs only for a reproduced Critical or
  Important defect.

## Current Baseline

| Node | State | Evidence |
|---|---|---|
| CP0 | complete | reliability contract, invariant catalog, and fault matrix merged |
| C1.1 | complete | `WorkflowWork`, versioned id-only tasks, terminal convergence, and rolling-writer compatibility merged in `a5e58879`, `81beccb5`, and `5366def5` |
| C1.2 | active | hook/import writers still use post-commit or lack versioned normalization/work creation |
| C1.3a-d | not started | sequence backfill, lifecycle producers, digest work, and schedulers remain legacy |
| CP2-CP10 | not started | focused specifications are being written in parallel |

The D2 branch `feat/cp1-1-d2-storage-bootstrap` is parked. Its commits and
untracked rejected design are preserved but do not block or participate in the
product-code campaign. The locked `feat/realtime-candidate-gating` worktree is
superseded by equivalent code already merged on `master` and is not an
integration source.

## Dependency DAG

```text
CP0 complete
  -> C1.1 complete
  -> C1.2 atomic observation writers
  -> C1.3a deterministic session-sequence backfill
  -> C1.3b atomic explicit and idle session lifecycle
  -> C1.3c immutable digest work and explicit attempts
  -> C1.3d scheduled producer cutover and final census
  -> C2.1 execution claim, lease, fencing, attempts
  -> C2.2 session-work reconciliation
  -> C2.3 candidate/projection/transport reconciliation
  -> C3.1 deterministic windows, chunks, coverage
  -> C3.2 provider-stage identity and strict output
  -> C3.3 complete reduction and atomic decision work
  -> C4.1 atomic promotion
  -> C4.2 atomic lineage and durable conflict transitions
  -> C4.3 writer convergence and rebuildable projections
  -> C5.1 deterministic curation gates
  -> C5.2 authorized shortlist and failure-safe judge
  -> C5.3 autonomous decision orchestrator
  -> C5.4 conflict-only product surface and eval
  -> C6.1 immutable context identity and snapshot
  -> C6.2 strict packing and degraded retrieval
  -> C6.3 shared client lifecycle contract
  -> C6.4 Claude/Codex adapters and E2E
  -> MCI-0 -> MCI-1 -> MCI-2 -> MCI-3 -> MCI-4
  -> MCI-5 -> MCI-6
  -> C9.1 shared scoped pgvector primitive
  -> C9.2 measured lexical/debug convergence
  -> C10.1 -> C10.2 -> C10.3 -> C10.4 -> C10.5
```

The arrows are merge dependencies, not a ban on early spec work. All focused
specifications may be drafted concurrently against the roadmap and current
code, then refreshed at implementation time for changed interfaces.

## Focused Specification Pack

| Slice | Focused specification | Immediate use |
|---|---|---|
| C1.2 | `2026-07-11-c1-2-atomic-observation-work.md` | active implementation authority |
| C1.3a | `2026-07-11-c1-3a-session-sequence-backfill.md` | next serial slice |
| C1.3b | `2026-07-11-c1-3b-atomic-session-lifecycle.md` | next serial slice |
| C1.3c | `2026-07-11-c1-3c-atomic-digest-work.md` | later CP1 slice |
| C1.3d | `2026-07-11-c1-3d-scheduler-cutover.md` | CP1 closure |
| CP2 | `2026-07-11-checkpoint-2-leases-reconciliation.md` | design now, code after CP1 |
| CP3 | `2026-07-11-checkpoint-3-complete-distillation.md` | design now, code after CP2 |
| CP4 | `2026-07-11-checkpoint-4-atomic-memory-transitions.md` | design now, code after CP3 |
| CP5 | `2026-07-11-checkpoint-5-conflict-only-curation.md` | design now, code after CP4 |
| CP6 | `2026-07-11-checkpoint-6-immutable-context-clients.md` | design now, code after CP5 |
| CP7 | `2026-07-11-checkpoint-7-memory-ci-foundation.md` | second spec wave |
| CP8 | `2026-07-11-checkpoint-8-temporal-revalidation.md` | second spec wave |
| CP9 | `2026-07-11-checkpoint-9-retrieval-convergence.md` | second spec wave |
| CP10 | `2026-07-11-checkpoint-10-repair-release.md` | second spec wave |

Existing large checkpoint documents remain roadmap authority. A focused spec
extracts executable transactions, interfaces, ownership, and tests; it does not
repeat product background.

## Active C1.2 Execution Shape

C1.2 begins from a dedicated integration branch and worktree based on current
`master`. The focused spec freezes the shared writer interface before parallel
workers begin.

### Serial foundation

One owner adds the shared row-locked observation/session allocation and atomic
work-creation primitive in a new focused module with unit and transaction tests.
It reuses `WorkflowWork` and versioned tasks; it does not import package
transport into the identity service or create another outbox abstraction.

### Parallel cutovers after the interface commit

| Task | Owned zone | Forbidden overlap |
|---|---|---|
| Hook writer | `engram/hooks/services.py` and hook ingest tests | imports, context, schema, shared primitive |
| Import writer | `engram/imports/services.py` and import tests | hooks, context, schema, shared primitive |
| Context session creation | `engram/context/services.py` and focused context tests | hooks, imports, schema, shared primitive |
| Fault and scope verification | new dedicated CP1 atomicity test module | production modules owned above |

Each worker uses a child branch/worktree from the same frozen foundation commit.
The main agent cherry-picks or merges reviewed commits into the C1.2 integration
branch and runs the combined container/CI gate. Workers do not share a mutable
worktree.

### C1.2 acceptance

- Evidence, required `WorkflowWork`, and the initial id-only package signal all
  commit or all roll back.
- Duplicate new-format evidence reuses evidence and work without another
  signal; legacy duplicate evidence repairs missing required work atomically.
- Hook and import observations receive server-authored session sequence and
  version-1 normalization/work-policy fields.
- Lifecycle-only evidence creates no observation work.
- Scope is resolved before evidence, work, provider-facing policy snapshot, or
  task creation.
- No migrated producer uses `transaction.on_commit()` for required work.
- Tests include rollback, broker unavailable at call time, duplicate/concurrent
  ingest, process boundary after commit, unauthorized scope, and id-only payload.

## Subsequent CP1 Execution

- C1.3a is a migration/backfill-only branch. It does not change producers.
- C1.3b serializes the final sequence/normalization contract and shared session
  end primitive, then parallelizes explicit-end and idle-sweep callsite tests
  only where files do not overlap.
- C1.3c owns immutable digest work, manual invocation, and new-format reruns.
- C1.3d owns Beat/scheduler/task/management-command producer cutover and the
  final repository callsite census.

No CP1 code waits for production rollout evidence. Deployment and historical
repair gates remain documented future concerns; development completion is
proved by migrations, fault tests, container tests, and GitHub CI.

## Later Checkpoint Parallelism

- CP2 serializes core lease/fence schema and claim logic, then separates
  session, candidate, transport-comparison, operations, and concurrency modules.
- CP3 gives one owner the central distillation service while chunk coverage,
  provider identity, provenance, and fault tests live in separate modules/files.
- CP4 serializes models/migrations and transition services; projection repair,
  alternate-writer adapters, and fault tests remain disjoint.
- CP5 serializes the decision orchestrator/current curation module; conflict API,
  frontend, and eval proceed after the outcome contract freezes.
- CP6 serializes bundle schema/context service; Claude, Codex, CLI fixtures, and
  cross-runtime E2E can proceed in parallel after the lifecycle contract.
- CP7-CP8 follow the MCI slice order but allow adapters, fixtures, validators,
  and UI work in parallel after each shared contract commit.
- CP9 uses one production retrieval primitive owner and a separate migration/
  benchmark owner; debug and curation adapters follow the frozen interface.
- CP10 keeps repair planning, fault E2E, fresh-clone verification, and runbooks
  disjoint. No production execution is part of this campaign run.

## Review And Verification Policy

For every coherent implementation slice:

1. The implementer records the failing RED command and expected failure.
2. The implementer records focused GREEN tests and scoped lint/format results.
3. One fresh correctness reviewer checks only the focused spec and diff.
4. One fresh simplicity reviewer checks scope, duplication, and unnecessary
   abstraction.
5. Reproduced Critical/Important findings return once to the same task owner.
6. The main agent runs the authoritative combined gate or pushes a draft branch
   and uses GitHub Actions when local container infrastructure is unavailable.
7. The main agent alone commits/integrates/pushes and updates the progress
   ledger.

Repeated reviews without a concrete reproduced blocker are forbidden. Optional
cleanup is recorded for later and does not stall the next slice.

## Immediate Queue

1. Complete and self-review the C1.2-C1.3d and CP2-CP6 focused specs in parallel.
2. Run Fable adversarial review on this decomposition and incorporate only
   concrete dependency/ownership defects.
3. Start the C1.2 integration worktree and serial shared primitive immediately
   after the focused C1.2 spec freezes.
4. Dispatch hook, import, context, and fault-test worktrees in parallel from the
   shared-interface commit.
5. While C1.2 code runs, draft CP7-CP10 focused specs in a second parallel wave.
6. Integrate C1.2, run container/CI gates, perform the two bounded reviews, and
   merge before starting C1.3a code.

## Stop Conditions

Stop only when a task requires an unapproved public/data contract change, two
parallel tasks unexpectedly need the same mutable file, a failing test exposes
an ambiguity in the focused spec, or GitHub CI proves the base itself is broken.
Local Docker/setup inconvenience and production state are not stop conditions.
