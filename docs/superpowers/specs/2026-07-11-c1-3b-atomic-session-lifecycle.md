# C1.3b Atomic Session Lifecycle

Date: 2026-07-11
Status: implementation-ready specification
Owner: C1.3b contract/lifecycle slice

Depends on the merged C1.1/C1.2 work in
`docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`,
the C1.3a deterministic sequence backfill, and the CP0 transport contract.
This slice changes only sequence/normalization finalization, explicit/idle
session end, session distillation dispatch, and the bounded failed-distillation
compatibility retry. C1.3c/C1.3d digest and scheduler producers remain out of
scope.

## Contract and success boundary

After the C1.3a drain proof and before this release starts, all observations
have a positive, unique per-session server sequence and every session cursor is
the maximum sequence or zero. No retired writer may still hold a transaction.
The C1.3b gate is closed only when:

- `RawEventEnvelope.normalization_contract_version` is non-null and every row
  is an explicit v0 legacy row or a valid v1 observation/no-op row;
- `AgentSession.observation_sequence_cursor` and
  `Observation.session_sequence` are non-null, positive where applicable, and
  cursor-consistent;
- the one shared end primitive serializes explicit and idle end on the session
  row, freezes the useful upper watermark, and commits status, marker, work,
  and its initial package signal together;
- no-input generations are terminal `WorkflowWork(disposition=no_op,
  resolution_reason=no_input)` and never produce an outbox row;
- duplicate or concurrent endings create at most one generation and one
  initial signal, while post-end useful activity reactivates and can create a
  larger generation; and
- bounded retry creates a linked queued `WorkflowRun` and composite-id
  versioned task for an existing required work, never a legacy session-id task.

The package outbox remains the sole transport authority. This slice does not
read package delivery state or claim exactly-once provider execution.

## Final sequence and normalization contract

Add `0034_memory_loop_input_contract.py`, depending on the deployed C1.3a
backfill migration. Before applying it, the release record must contain the
C1.3a assertions (all observations sequenced, positive/unique per session,
cursor equals max or zero), exact retired-revision/PID inventory, and proof
that no retired writer transaction is open.

The migration is data-safe and deterministic. Its preflight must abort when a
raw row has only part of the normalization tuple populated or has an unknown
non-null version; such a row is not silently classified as v0.

1. In one bounded data step, mark every remaining all-null raw normalization
   tuple as `normalization_contract_version=0`. Do not infer a disposition or
   reason from event type, and do not mutate v1 rows.
2. Add the final check allowing exactly one of:
   `version=0, disposition=NULL, reason=NULL`;
   `version=1, disposition=observation, reason=NULL`; or
   `version=1, disposition=no_op, reason=evidence_only`.
3. Make normalization version `NOT NULL` (default is not used to fabricate v1
   evidence). Make the session cursor and observation sequence `NOT NULL`;
   retain cursor database default `0`, and make sequence positivity
   unconditional. Preserve the conditional unique `(session, sequence)` index.
4. Keep `end_work_contract_version` constrained to `0|1`, non-null with
   database default `0`. The migration must not rewrite legacy ended sessions
   to marker 1.

The migration must fail before DDL if any C1.3a assertion is false. It must
not scan or renumber observations, create historical work, or require a
long-lived table lock beyond the reviewed migration budget. Apply/reverse
tests cover a fresh database and a database that already recorded C1.1/C1.2;
after the non-null contract, rollback is behavior-forward only (never boot an
old writer that can insert null).

## Public and internal interfaces

Create `apps/backend/engram/memory/session_lifecycle.py` with these typed
interfaces. The public service owns its transaction; `_end_session_locked`
is callable only by tests and the service after a `select_for_update`.

```python
@dataclass(frozen=True, slots=True)
class EndSessionResult:
    session_id: uuid.UUID
    transitioned: bool
    work_id: uuid.UUID | None
    work_created: bool
    disposition: str | None
    upper_sequence_inclusive: int | None
    initial_signal_created: bool

class EndSession:
    def execute(
        self,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        session_id: uuid.UUID,
        ended_at: datetime | None,
        source: Literal['explicit', 'idle'],
    ) -> EndSessionResult: ...
```

`execute` validates organization/project/session scope, enters
`transaction.atomic()`, locks exactly the session row with
`select_for_update(of=('self',))`, and delegates to the locked primitive. It
returns only after the outer transaction commits. When called by hook ingest
inside an existing atomic block it uses a savepoint, not an independent
commit, so the raw envelope, observation, end transition, work, and package
row still commit or roll back together. A malformed scope, naive timestamp,
or foreign session fails before any write.

The locked primitive performs this exact order:

1. If status is not `ACTIVE`, return `transitioned=False` and all work/signal
   fields null; do not create or re-signal work.
2. Query the same-scope observations and compute
   `Max(session_sequence)` after excluding trusted persisted lifecycle event
   types `session_start` and `session_end` from
   `Observation.source_metadata['event_type']`. Use zero when none exists.
   Never use the cursor, client sequence, prompt number, timestamps, or UUID
   order as the watermark.
3. Set `status=ENDED`, server `ended_at`, and
   `end_work_contract_version=1`; save all three in this transaction.
4. Build the canonical snapshot
   `{'schema':'session_distillation_input/v1','session_id':str(session_id),
   'lower_sequence_exclusive':0,'upper_sequence_inclusive':upper}` and call
   `create_work(CreateWorkflowWorkInput(..., work_type=SESSION_DISTILLATION,
   subject_type=AGENT_SESSION, subject_id=session_id, snapshot))`.
5. If `upper == 0` and the work is still `required`, resolve it with
   `resolve_work_no_input(...)`. It is terminal no-op and emits no task. If a
   reused work is already terminal, preserve that disposition and never
   rewrite it. If `upper > 0`, leave newly created work `required`; only when
   `created=True` emit the initial versioned signal described below.
6. Return the work identity, disposition, frozen upper, and whether this
   transaction created that initial signal.

The final observation-writer order remains explicit: resolve scope; enter the
transaction; lock the session; recheck raw/observation duplication; if the
locked session is ended and this is non-end activity, set `ACTIVE`, clear
`ended_at`, reset marker 0; allocate `cursor + 1` (or max existing sequence +
1 for a legacy zero cursor); persist that value to both cursor and observation;
then write the v1 normalization/source disposition. A duplicate reuses its
existing sequence and does not increment the cursor. New session creators
write cursor zero explicitly even while the database default remains present.

The helper must be idempotent for an existing work: a repeated lifecycle-only
end reuses the same fingerprint and never re-signals a still-required work.
`create_work` remains task-free and must be called only inside the producer's
transaction.

## Initial signal and worker boundary

For a newly created useful session work, call the OutboxCelery task boundary
before leaving the same transaction:

```python
distill_session_work_v1.apply_async(
    args=[str(work.id)],
    kwargs={},
    task_id=f'workflow-work:{work.id}',
)
```

The persisted package row contains only the work UUID, no session content,
snapshot, prompt, or provider data. Do not use `transaction.on_commit`,
`distill_session.delay(session_id)`, or a second domain outbox. Any exception
from work or package creation rolls back the end status and marker as well.

`distill_session_work_v1(work_id, workflow_run_id=None)` must parse UUIDs,
load and scope-check `WorkflowWork`, recompute its fingerprint, and reject a
non-session work or altered snapshot. A no-op work returns idempotently without
provider access. For an automatic signal with no run id, create or adopt the
one initial `WorkflowRun` linked to that work under a work-row lock; duplicate
delivery adopts an existing queued/running/terminal initial attempt and never
creates a second automatic run. A terminal failed initial attempt is left for
the bounded compatibility retry producer. For an explicit run id, require a
queued run linked to the same work and scope.

The worker passes the frozen upper bound into the distillation domain service:

```python
DistillSessionInput(
    session_id=session.id,
    upper_sequence_inclusive=snapshot['upper_sequence_inclusive'],
    request_id=request_id,
    correlation_id=correlation_id,
    run_id=str(run.id),
)
```

Its input query is same-scope, non-lifecycle observations with
`0 < session_sequence <= upper`, ordered by `session_sequence`. A useful row
accepted after this end transaction is excluded and will belong to a later
generation. On an untruncated semantic result resolve `complete/succeeded` or
`complete/no_signal`; on `truncated=True`, record the attempt but leave work
`required` for CP3 continuation. Provider calls remain outside write locks.

## Reactivation, duplicate, and concurrency semantics

All observation writers and the end primitive lock the session row before
allocating a sequence or changing lifecycle state. A writer that wins the lock
before end gets its useful observation inside that generation. A writer that
wins after end sees `ENDED`, reactivates it (`ACTIVE`, `ended_at=NULL`), resets
`end_work_contract_version=0`, then allocates the next sequence. The next end
sets marker 1 and uses the larger useful upper. Lifecycle-only activity may
reactivate but does not change the maximum useful upper, so a later end reuses
the prior generation.

An idempotency duplicate returns the existing raw event/observation under the
session lock and does not advance the cursor or call `EndSession`. Two distinct
concurrent end requests serialize: the first active-to-ended request owns the
work/signal; the second observes `ENDED`, returns `transitioned=False`, and
creates neither a new work row nor a package row. An idle scan may race with a
hook end; the same lock and status predicate make only one transition win.
Team identity is never widened during end; the existing non-null session team
must remain the work's derived team.

## Compatibility retry cutover

Rewrite `RetryFailedDistillations` to enumerate only post-cutover
`WorkflowWork(work_type=session_distillation, disposition=required)` and its
linked runs. Historical sessions/runs with no exact work remain unlinked and
are reported, not backfilled. Preserve current cooldown, transient markers,
and non-transient/transient attempt caps. A succeeded run (including a
truncated result) suppresses this bounded failed-run retry; CP3 owns
continuation.

For each eligible work, use a per-work transaction: lock the work and latest
run, re-evaluate caps/cooldown/status, create one queued `WorkflowRun` with a
copy of the immutable work snapshot, and emit:

```python
distill_session_work_v1.apply_async(
    args=[str(work.id), str(run.id)],
    kwargs={},
    task_id=f'workflow-work:{work.id}:run:{run.id}',
)
```

Return work/run pairs for logging and metrics. Never call the legacy
`distill_session.delay(session_id)` producer after this cutover. Keep the
legacy task registered until the separately recorded package-drain gate has
closed; it may execute only already-persisted legacy package rows.

## Files and ownership

- `apps/backend/engram/core/models.py` and
  `apps/backend/engram/core/migrations/0034_memory_loop_input_contract.py`:
  final non-null state and migration tests in `core/migrations_tests.py`.
- `apps/backend/engram/memory/session_lifecycle.py` (new),
  `session_sweep.py`, and their tests: shared explicit/idle primitive,
  watermark, marker, and lock behavior.
- `apps/backend/engram/hooks/services.py` and hook tests: call the primitive
  for explicit end, retain row-locked reactivation/sequence allocation, and
  remove end-path post-commit dispatch.
- `apps/backend/engram/memory/tasks.py`, `distillation.py`, and focused task
  tests: work-id worker, frozen-prefix query, run adoption, and truncation.
- `apps/backend/engram/memory/distillation_reconciler.py` and tests:
  work-linked compatibility retry and legacy-unlinked reporting.
- C1.3b invariant/P2 tests: prove only marker-1 ended sessions enter the
  exact post-cutover cohort; v0 history remains visible but not repaired.

Do not edit digest services, scheduler/management-command producers,
retrieval authorization, deployment manifests, or transport-package code in
this slice.

## Required RED and fault tests

The implementation starts with failing tests for each item, then turns them
green without weakening the assertion:

1. Migration marks null normalization as v0 and rejects partial/unknown tuples;
   null cursor/sequence inserts fail after contract application.
2. Fresh and already-published migration histories preserve old-session insert
   compatibility before 0034 and reject it after 0034.
3. Useful upper excludes lifecycle rows, uses server sequence rather than
   cursor, and freezes exactly the accepted prefix.
4. Explicit and idle end both use the same service; status, ended time,
   marker, work, and package row are visible together inside the transaction.
5. Forced rollback, package `.apply_async` failure, and simulated broker
   unavailability leave no ended marker/work/package residue.
6. Empty/lifecycle-only end creates one terminal no-op and zero outbox rows.
7. Two concurrent ends converge on one generation and one initial signal;
   append-vs-end barriers place the append in exactly one generation.
8. A duplicate end does not advance sequence, create work, or re-signal; a
   reactivated session resets marker 0 and later useful input yields a larger
   fingerprint/upper.
9. Duplicate task delivery adopts one initial run; worker input excludes rows
   above the frozen upper and keeps truncated work required.
10. Retry cutover creates one linked run and composite-id package per retry,
    converges under concurrent reconciler calls, preserves caps, and never
    invokes a legacy task producer.
11. Scope, team, malformed UUID/timestamp, altered snapshot, and foreign work
    negative controls create no rows and no package signal.
12. Repository callsite census finds no runtime legacy session-id producer
    outside the registered legacy task adapter.

## CI gates, non-goals, and stop conditions

The slice gate requires focused Django tests for migration, hooks, session
sweep, distillation, tasks, and reconciler (inside Compose once available;
pytest parallelism must not exceed `-n 2`), Ruff/format checks, Django system
checks, migration apply/reverse/freshness checks, `git diff --check`, and the
legacy-producer census. Record exact commands, counts, exit codes, and any
unrun full-suite checks in the checkpoint report. Deployment, SSH, runtime
migration, and D2 work are not part of this spec.

Non-goals are leases/fencing, general reconciliation, package delivery state,
historical work backfill, digest/scheduler migration, semantic promotion,
stage coverage, and a general exactly-once claim.

Stop before implementation if the C1.3a assertions or writer-drain proof are
missing, any session exceeds the reviewed backfill/lock budget, the migration
would require dropping the database default, a lock-order test can deadlock,
or a required producer cannot be converted to stable work/run ids without
changing the public API or data model. Escalate those findings as a new,
reviewed slice rather than weakening this contract.
