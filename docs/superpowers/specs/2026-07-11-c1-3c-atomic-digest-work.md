# C1.3c — Atomic Digest Work And Explicit Attempts

Date: 2026-07-11
Status: focused implementation specification
Roadmap gate: Checkpoint 1, C1.3c

## Scope

C1.3c makes daily and weekly digest execution consume one immutable
`WorkflowWork.input_snapshot`, proves output visibility before source selection,
provider access, and publication, and moves manual creation and new-format
reruns to versioned work/run task payloads. C1.3d owns scheduled producers and
the management command; this slice supplies their shared producer primitive.

The authoritative contracts are the C1.1–C1.3 sections of
`docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`.
This document names the implementation seams, tests, and cutover gate for
digest work only.

## Goal And Success Boundary

For every post-cutover daily or weekly occurrence:

- the first scoped transaction freezes the exact authorized source/version
  list, policy, UTC window, cap, and input digest;
- duplicate occurrence creation returns the winner without mutating its
  snapshot;
- an empty authorized set creates terminal `no_op/no_input` work and no
  package row;
- required work and its initial outbox-backed id-only task signal commit or
  roll back together;
- the worker reloads only the frozen versions, recomputes their body digests,
  and fails closed on scope, version, policy, or fingerprint drift;
- source authorization is checked before selection, before the provider call,
  and before publication; an invalidated result is never published;
- digest output, `MemoryVersion`, `RetrievalDocument`, linkage metadata, and
  `complete/succeeded` commit in one post-provider transaction;
- each accepted manual request can create one linked queued attempt against the
  same work, while the work identity never contains a request UUID;
- a linked rerun uses only `work_id` and `workflow_run_id`; an unlinked legacy
  run returns `409 legacy_work_unlinked` without mutation;
- malformed or unproven historical `kind=digest` rows remain untouched and are
  excluded from every new source, retrieval, replay, review, and body-bearing
  read path.

## Current Seams To Replace

The implementation starts from these existing call sites:

- `apps/backend/engram/memory/tasks.py`: legacy
  `generate_daily_digest(organization_id, project_id, memory_ids, ...)`,
  `generate_weekly_digest(organization_id, project_id, ...)`, and the already
  registered but unfinished `*_work_v1` adapters;
- `apps/backend/engram/memory/services.py`:
  `run_daily_digest_with_tracking`, `run_weekly_digest_with_tracking`,
  `GenerateDigest`, and `BuildWeeklyStructuredDigest` currently reload mutable
  state;
- `apps/backend/engram/console/views/project_digest.py` creates a run and calls
  the legacy daily task after a separate commit;
- `apps/backend/engram/console/views/digests.py` synchronously executes
  `BuildWeeklyStructuredDigest` and reads unscoped historical digests;
- `apps/backend/engram/console/views/workflow_runs.py` reconstructs mutable
  `memory_ids`/window inputs and dispatches legacy task signatures;
- `apps/backend/engram/memory/workflow_work.py` already owns canonical JSON,
  occurrence uniqueness, daily/weekly snapshot validation, and `create_work`.

No producer may reselect current memories after a work row is frozen.

## Interfaces And Data Contract

### Digest input builders

Add `apps/backend/engram/memory/digest_work.py` with these typed interfaces:

```python
@dataclass(frozen=True, slots=True)
class DigestSourceRef:
    render_position: int
    memory_id: UUID
    memory_version_id: UUID
    version: int
    server_body_digest: str
    visibility_scope: str
    team_id: UUID | None
    source_title: str

def freeze_daily_digest_input(
    *, organization_id: UUID, project_id: UUID, window_start: datetime,
    window_end: datetime, schedule_key: str, max_sources: int,
) -> dict[str, object]: ...

def freeze_weekly_digest_input(
    *, organization_id: UUID, project_id: UUID, team_id: UUID | None,
    window_start: datetime, window_end: datetime, schedule_key: str,
) -> dict[str, object]: ...
```

Both functions normalize boundaries to UTC and apply the output policy before
querying eligible rows. They exclude `kind='digest'`, session/organization
visibility, foreign project rows, and team rows not explicitly admitted.
Daily sorts authorized rows by `(-updated_at, id)`, caps at `max_sources`, then
assigns `render_position` by `(source_title, memory_id)`. Weekly classifies only
authorized closed-window transitions and sorts by bucket, occurrence time,
memory UUID, and transition reference.

Each source/change records the exact `MemoryVersion` and a server SHA-256 over
canonical `(memory_version_id, version, body)` bytes. `input_digest` covers the
schema, project/team, window, visibility policy, cap/truncation fields, and
ordered refs; it is not a memory-id-only or window-only hash.

### Work creation and signal

Use the existing `CreateWorkflowWorkInput` and `create_work` from
`engram.memory.workflow_work` inside the producer's outer `transaction.atomic()`.
Add one helper with an explicit result:

```python
def create_digest_work_and_signal(
    *, data: CreateWorkflowWorkInput, signal_task: object,
    workflow_run: WorkflowRun | None = None,
) -> tuple[WorkflowWork, bool]: ...
```

The helper must not import Celery task functions or read package tables. The
caller creates the queued `WorkflowRun` (when applicable), passes the matching
task object as `signal_task`, and the helper calls `create_work` followed by
`dispatch_work_task(signal_task, work.id, workflow_run.id if present)` only when
`created` and disposition is `required`. The
deterministic task id is `workflow-work:<work UUID>` for the initial signal and
`workflow-work:<work UUID>:run:<run UUID>` for an explicit attempt.

`no_op/no_input` is terminal in the same transaction and emits no package row.
Any work, run, audit, or outbox failure rolls the producer transaction back.

### Worker boundary

`engram.memory.tasks.generate_daily_digest_work_v1(work_id, workflow_run_id=None)`
and `generate_weekly_digest_work_v1(work_id, workflow_run_id=None)` parse UUIDs,
load the expected work type, validate the optional run scope, and call:

```python
def execute_frozen_digest_work(
    work: WorkflowWork, workflow_run: WorkflowRun | None,
) -> UUID | None: ...
```

The executor reloads the exact scoped subject and versions, recomputes every
server body digest and the work fingerprint, and validates the frozen policy.
Mismatch raises `MemoryWorkerError` before provider access. Automatic terminal
work returns idempotently; a prior exact output is reusable only when its
metadata matches work id, input digest, output identity, policy, allowed teams,
scope, and team.

### Visibility authority and publication

Project output freezes `allowed_team_ids=[]`, admits only project-visible rows,
and publishes project visibility with null team. Team output requires the
selected team in `request.effective_scope`, a same-organization `ProjectTeam`
link, freezes exactly `[team_id]`, admits project plus that team, and publishes
team visibility bound to that team. A project request never means all teams.

Immediately before provider access, a short transaction locks selected
`Memory`, exact `MemoryVersion`, and selected `ProjectTeam` rows in UUID order,
revalidates policy, and commits. No lock spans provider execution. After the
provider returns, a second transaction takes the same locks, revalidates, then
creates/reuses the exact output, `MemoryVersion(version=1)`, and
`RetrievalDocument` with `defer_embedding=True` (or equivalent row-only path),
links metadata, and resolves work succeeded. A mutation committed after the
pre-call point but before output locking discards the provider result. No
embedding provider runs under publication locks.

### Quarantine predicate

Add `engram.memory.digest_visibility.py`:

```python
def proven_digest_memory(memory: Memory) -> bool: ...
def digest_visibility_failure(memory: Memory) -> str | None: ...
```

Proof requires server-authored `digest_visibility/v1`, `workflow_work_id`,
`input_digest`, `output_identity`, allowed teams, output scope/team, a
completed same-scope work, matching immutable snapshot, recomputed output
identity, and matching `Memory`/`RetrievalDocument` visibility. Missing,
malformed, legacy, or inconsistent linkage returns
`digest_visibility_unproven`; it never mutates the row.

Apply the predicate before digest source selection, search candidates/ranking,
context packing and stored replay, curation, search debug, weekly history,
inspection counts/details, version/diff, approved export, review queue, audit
title resolution, and workflow result titles. A replay containing any unproven
digest returns `context_bundle_digest_visibility_unproven` with no rendered text
or items. No capability, including `memories:admin`, bypasses body quarantine.

## Manual And Rerun Cutover

`ProjectDigestRunView.post` must resolve the project through
`request.effective_scope` before any source read, freeze the project-output policy and input,
then in one transaction enforce the active-run guard, create/reuse work, create
the linked queued run, create the composite-id package when required, and write
the audit. Empty authorized input returns the existing non-enqueued shape with
terminal no-input work and no audit/package. A concurrent active-run conflict
creates nothing.

`WeeklyDigestView.get` becomes enqueue/read-through. For `weeks_back=0`, scope
and team membership are checked first, then current occurrence work is created
and its initial signal emitted transactionally. It returns `built=true` only for
exact proven output; pending/new/no-input uses the existing `built=false` shape.
For `weeks_back>0`, it remains read-only and applies scope plus quarantine.

`DigestReviewView.post` must perform its first memory query with effective
project/team scope and the proof predicate; unproven or unauthorized rows are
not found and create no mutation or audit.

`WorkflowRunViewSet.rerun` must require a terminal linked run/work pair. It
locks the work, creates one linked queued run with immutable work snapshot and
`rerun_of`, writes `WorkflowRunReran`, and dispatches the matching versioned
task. An authorized unlinked historical run returns `409 legacy_work_unlinked`
with no write or signal. Reruns never create a new digest generation.

## Files And Ownership

The C1.3c owner may edit only these application/test files in this slice:

- `apps/backend/engram/memory/digest_work.py` and
  `digest_work_tests.py` (freeze, publication, quarantine contracts);
- `apps/backend/engram/memory/digest_visibility.py` and focused tests (the
  single proof predicate and failure code);
- `apps/backend/engram/memory/workflow_work.py` and adjacent tests (only
  digest-specific validation/helper additions);
- `apps/backend/engram/memory/tasks.py` and `tasks_tests.py` (worker adapters);
- `apps/backend/engram/console/views/project_digest.py` and tests;
- `apps/backend/engram/console/views/digests.py` and tests;
- `apps/backend/engram/console/views/workflow_runs.py` and tests;
- existing context/search/inspection/export/review/audit consumers and their
  focused tests for the shared quarantine predicate.

C1.3d owns scheduler/command files and final census tests. C1.3b owns session
files. Shared model/migration files remain with the C1.1 schema owner.

## RED Tests Before Switching Producers

1. Two concurrent creates for one occurrence leave one immutable snapshot; a
   later source change does not rewrite it.
2. Daily and weekly snapshots contain exact versions/body digests and reject
   mutable title/body reselection, cross-project refs, and unauthorized teams.
3. Empty authorized daily/weekly input resolves `no_op/no_input` and creates no
   package row.
4. A rollback after work/run/audit/package setup leaves all rows absent.
5. Worker fingerprint/body-digest drift fails before provider invocation.
6. Project output excludes every team-private source; team output admits exactly
   its selected team and rejects another team at selection, pre-call, and output.
7. Provider-result invalidation after pre-call causes no output/version/document.
8. Publication creates output/version/document/work completion atomically and
   never calls embedding while locks are held.
9. Legacy/malformed digest rows are withheld from every listed read/replay path,
   including flat admin capability, without row mutation.
10. Manual daily and weekly requests reuse work but create distinct linked runs;
    active-run conflicts create no rows.
11. Weekly current GET enqueues and returns `built=false`; exact proven output
    returns `built=true`; historical GET never creates work.
12. Linked rerun emits only the composite id-only task; unlinked legacy rerun is
    `409 legacy_work_unlinked` with no writes.
13. All new digest task payloads contain only `work_id` and optional `run_id`.

## CI And Verification

Run inside Compose once the backend test service is available:

```text
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/memory/digest_work_tests.py \
  engram/memory/workflow_work_tests.py engram/memory/tasks_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/console/views/project_digest_tests.py \
  engram/console/views/digests_tests.py engram/console/views/workflow_runs_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/context/ engram/search/ engram/inspection/ -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python manage.py check
git diff --check
```

The focused RED must fail for the old producer, then pass after the cutover.
Record exact pass counts, migration/system-check output, and the first failure
if a command cannot run. Do not claim full-suite or provider-live coverage when
it was not executed.

## Non-Goals

- No leases, fencing, logical retry scheduler, reconciliation, or CP2 repair.
- No late-arrival/carry-forward semantics for an already frozen window.
- No historical digest rewrite, delete, reindex, or semantic reinterpretation.
- No recursive digest inputs; every `kind='digest'` source is excluded.
- No new public `WorkflowWork` API or invariant evaluator endpoint.
- No transport mirror, broker polling, deployment, SSH, runtime repair, or
  D2 work.

## Stop Conditions And Acceptance

Stop before producer cutover if any source can reach the snapshot/provider/output
without the frozen policy, if a package signal is created outside the producer
transaction, if an empty set emits a task, if output publication is separate
from work completion, or if a legacy task payload must be reinterpreted.

Stop if any quarantine consumer still exposes title/body through a capability
union, if a provider call occurs before the locked revalidation, if tests cannot
prove no embedding call under output locks, or if a rollback would delete or
reinterpret historical rows.

C1.3c is accepted only when all RED cases pass, manual/current weekly paths and
linked reruns emit versioned id-only tasks, historical unlinked reruns fail
closed, the quarantine predicate covers every listed consumer, and C1.3d can
call the same freeze/work/signal helpers without mutable source arguments.
