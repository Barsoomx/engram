# C1.3d — Scheduled Digest Producers And Final Census

Date: 2026-07-11
Status: focused implementation specification
Roadmap gate: Checkpoint 1, C1.3d

## Scope

C1.3d changes daily/weekly scheduler tasks and `engram_run_daily_digest` to
freeze inputs and create `WorkflowWork` through the C1.3c producer primitive.
It establishes stable UTC buckets, makes duplicate scheduler executions
converge, and proves no runtime producer still emits legacy digest task
signatures. C1.3c owns worker execution, visibility/quarantine, and manual or
rerun views; C1.3d does not duplicate those contracts.

The authoritative source is the C1.3 section of
`docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`.
This document is the scheduler/command implementation gate, not a deployment
or historical repair procedure.

## Goal And Success Boundary

For every eligible project and closed schedule window:

- the producer computes one canonical UTC occurrence key and exact window;
- authorization, source selection/classification, cap, and truncation happen
  before `WorkflowWork` creation;
- the first transaction freezes the snapshot; a second scheduler invocation
  reuses it without mutation or a duplicate initial package signal;
- no-input is terminal `no_op/no_input` and emits no task or package row;
- required work emits only `work_id` (plus an explicit `workflow_run_id` when a
  caller supplies one) through `dispatch_work_task` inside the transaction;
- scheduled tasks never pass memory ids, source bodies, prompts, or mutable
  project state through Celery;
- management-command runs use the same bucket/input identity as the scheduled
  producer, with `--window-days` changing the proposed frozen window only;
- a final repository census proves all runtime legacy producer call sites
  are gone except explicitly retained legacy task adapters during drain.

## Existing Scheduler And Command Seams

Current code to migrate:

- `apps/backend/engram/memory/tasks.py`:
  `run_scheduled_digests`, `run_scheduled_weekly_digests`,
  `daily_digest_window_start`, `recent_approved_memory_ids`, and legacy
  `generate_*_digest.delay(...)` calls;
- `apps/backend/engram/core/management/commands/engram_run_daily_digest.py`:
  per-project mutable memory-id query and `generate_daily_digest.delay(...)`;
- `apps/backend/engram/celeryconfig.py`: beat names `daily-digest` at 02:00
  UTC and `weekly-digest` on Monday at 03:00 UTC, both on `engram-batch`;
- `apps/backend/engram/memory/daily_digest_tests.py` and
  `weekly_digest_schedule_tests.py`: tests currently assert legacy arguments;
- the final census must include hook ingest, explicit end, idle sweep,
  scheduled daily/weekly, `ProjectDigestRunView`, workflow rerun,
  `WeeklyDigestView`, `engram_run_daily_digest`, and
  `RetryFailedDistillations`.

## Stable Bucket Contract

Add `apps/backend/engram/memory/digest_scheduler.py` with pure helpers:

```python
@dataclass(frozen=True, slots=True)
class DigestBucket:
    work_type: str
    schedule_key: str
    window_start: datetime
    window_end: datetime

def daily_bucket(
    *, as_of: datetime, window_days: int = 1, schedule_hour: int = 2,
) -> DigestBucket: ...
def weekly_bucket(*, as_of: datetime, schedule_hour: int = 3) -> DigestBucket: ...
```

`as_of` must be aware and is normalized to UTC. Daily uses one calendar-day
key, `daily:YYYY-MM-DD`, with the closed interval ending at the UTC daily cut;
the prior interval starts `window_days` days before that cut. The pure helper
defaults to one day; the scheduled task and command pass the configured
`ENGRAM_DAILY_DIGEST_WINDOW_DAYS` value when no override is supplied. A command
override is validated before being passed to this helper. Weekly
uses one ISO-week key, `weekly:YYYY-Www`, starts at Monday 03:00 UTC, and always
classifies a closed interval. The default cuts remain 02:00 and Monday 03:00
from `celeryconfig.py`; tests inject `as_of` rather than reading wall clock.

The key is derived only from work type, exact UTC boundaries, and selected team
when a future team-scoped weekly occurrence is requested. It never contains a
request UUID, process id, or last-success timestamp. A different explicit
window/override is a distinct proposal, but an existing occurrence winner is
never rewritten.

## Scheduler Producer Interface

`digest_scheduler.py` exposes transaction-owned functions consumed by Celery
and the command:

```python
def schedule_daily_project(
    *, project_id: UUID, bucket: DigestBucket, max_sources: int,
    workflow_run_id: UUID | None = None,
) -> ScheduleResult: ...

def schedule_weekly_project(
    *, project_id: UUID, bucket: DigestBucket, team_id: UUID | None = None,
) -> ScheduleResult: ...

@dataclass(frozen=True, slots=True)
class ScheduleResult:
    work_id: UUID
    created: bool
    disposition: str
    source_count: int
    task_enqueued: bool
```

Each function resolves organization/project/team by scoped ids, invokes the
C1.3c `freeze_daily_digest_input` or `freeze_weekly_digest_input`, then opens
one `transaction.atomic()` block. Inside it, call `create_work` with
`CreateWorkflowWorkInput` and, only for newly created required work, invoke
`create_digest_work_and_signal`. A supplied run must already be linked to the
same work scope; scheduler invocations normally pass no run. The package row
is part of the same transaction as the work row. No scheduler function reads
Celery outbox state to infer completion.

For a duplicate occurrence, reload the winning snapshot, verify its scope and
canonical fingerprint, return `created=False`, and never call a second initial
signal. If the proposed source set differs, log the frozen decision and retain
the first snapshot. Scope collision, malformed snapshot, or integrity failure
aborts the transaction and emits no signal.

## Daily Producer

`run_scheduled_digests()` remains the registered Celery task name but delegates
to `daily_bucket(as_of=timezone.now())` and calls `schedule_daily_project` once
per project in deterministic project UUID order. It must not call
`recent_approved_memory_ids` or `generate_daily_digest.delay`.

Eligibility is determined by the C1.3c project-output policy: only approved,
non-stale, non-refuted, non-`kind='digest'` project-visible versions inside
the frozen interval are considered. Selection, cap, exact version refs, title
redaction, and input digest are all frozen before work creation. An empty
authorized project still creates terminal no-input work so a repeated scheduler
run observes a complete occurrence rather than repeatedly probing the database.

Return the existing aggregate shape with exact counters:

```python
{'scheduled_projects': int, 'required_work': int,
 'no_input_projects': int, 'task_enqueued': int}
```

The names may not encode queue depth or broker delivery; they count database
producer outcomes only.

## Weekly Producer

`run_scheduled_weekly_digests()` uses `weekly_bucket(as_of=timezone.now())`,
iterates projects in deterministic UUID order, and calls
`schedule_weekly_project`. Weekly classification is based on authorized closed-
window transitions, not the current "recent approved memory" proxy. Refutation,
retirement, supersession, and lineage-only changes therefore create a real
weekly input. `kind='digest'` outputs are excluded before classification.

The task returns the same aggregate fields as daily. A project with no
authorized changes receives `no_op/no_input` work and no package row. A
duplicate Monday beat delivery reuses the same `weekly:YYYY-Www` occurrence.

## Management Command Cutover

`apps/backend/engram/core/management/commands/engram_run_daily_digest.py` keeps
the command name and `--window-days` option. It must:

1. parse a non-negative integer override and reject values outside the existing
   configured maximum with a normal command error;
2. compute one injected `as_of=timezone.now()` and one daily bucket (passing the
   validated override as `window_days`) for the entire invocation;
3. iterate projects in deterministic UUID order;
4. call `schedule_daily_project` with the same project, bucket, and max-source
   policy used by the scheduled task; an override proposes a different
   `window_start` but cannot rewrite an already frozen occurrence;
5. print deterministic counts for scheduled, no-input, and skipped/failure
   outcomes without exposing source bodies or ids.

The command never calls a Celery task directly with organization/project/memory
arguments. Its transaction and idempotency behavior must be byte-for-byte the
same as the scheduled daily producer. Running the command twice for one bucket
creates one work row and one initial package row at most.

## Beat Registration

Retain these public beat keys and task names in
`apps/backend/engram/celeryconfig.py`:

| Beat key | Task | Schedule | Queue |
|---|---|---|---|
| `daily-digest` | `engram.memory.run_scheduled_digests` | 02:00 UTC | `engram-batch` |
| `weekly-digest` | `engram.memory.run_scheduled_weekly_digests` | Monday 03:00 UTC | `engram-batch` |

Only task internals change in C1.3d. Do not add per-project schedule tuning,
new beat keys, or a second scheduler transport.

## Files And Ownership

C1.3d owns these files and tests:

- create `apps/backend/engram/memory/digest_scheduler.py` and
  `digest_scheduler_tests.py` (bucket and producer primitives);
- modify `apps/backend/engram/memory/tasks.py` and
  `daily_digest_tests.py`/`weekly_digest_schedule_tests.py` (scheduler task
  adapters and aggregate counters);
- modify `apps/backend/engram/core/management/commands/engram_run_daily_digest.py`
  and its command tests;
- modify `apps/backend/engram/celeryconfig.py` only if route assertions need
  the versioned task names;
- add `apps/backend/engram/memory/legacy_producer_census_tests.py` (static
  call-site contract).

C1.3c owns `digest_work.py`, worker adapters, manual/rerun views, and the shared
quarantine helper. C1.3d must consume those interfaces without changing their
semantics. C1.3b owns session sweep/retry producers. No model or migration file
is owned here.

## RED Tests Before Switching Producers

1. Injected `as_of` at either side of a daily cut produces the expected one-day
   key and exact UTC boundaries; equivalent local timestamps normalize equally.
2. Injected `as_of` across Monday 03:00 produces one stable ISO-week key and a
   closed window; timezone-naive input fails before a write.
3. Two concurrent daily scheduler calls for one project/bucket converge on one
   work row, one frozen `input_digest`, and one package row.
4. A changed proposed source list on a duplicate occurrence does not mutate the
   winning snapshot or emit a second task.
5. Daily empty input creates `no_op/no_input`, returns `task_enqueued=0`, and
   emits no package row; repeat invocation is idempotent.
6. Weekly lineage-only/refutation/retirement changes are eligible even when no
   current approved-memory proxy row exists; digest outputs are excluded.
7. Same-org foreign project/team references fail before work creation/provider
   access, including project-wide daily and weekly paths.
8. Scheduler task payloads contain only stable work/run ids; source ids and
   bodies never reach Celery.
9. `engram_run_daily_digest` and `run_scheduled_digests` call the same helper,
   share the same bucket, and converge when run in either order.
10. `--window-days` changes only the first proposed snapshot and cannot mutate
    a winner already frozen for that occurrence.
11. Beat keys, task names, Monday/day cuts, and `engram-batch` routing remain
    registered exactly once.
12. The census fails while any runtime `.delay`/`.apply_async` call uses a
    legacy digest task/signature and passes after all listed producers switch.

## Final Legacy-Producer Census

Add a test that scans tracked Python sources under `apps/backend/engram` and
fails on legacy task calls outside the retained adapter definitions. The
forbidden calls are `generate_daily_digest.delay`,
`generate_weekly_digest.delay`, `generate_daily_digest.apply_async`, and
`generate_weekly_digest.apply_async`, plus any `delay`/`apply_async` of the
legacy session task in the C1.3b-owned producer set.

The census report must enumerate and classify these producer surfaces:

- hook observation ingest and explicit end;
- idle/stale session sweep;
- scheduled daily and weekly tasks;
- `ProjectDigestRunView.post`;
- `WorkflowRunViewSet.rerun`;
- `WeeklyDigestView.get` current-window path;
- `engram_run_daily_digest`;
- `RetryFailedDistillations` and any compatibility retry adapter.

It must permit only the legacy task function definitions and a documented
drain adapter; no active producer may pass mutable ids, source lists, or window
arguments. The final output records the exact zero count and matching file list
for the release checkpoint.

## CI And Verification

Run in the backend Compose test container:

```text
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/memory/digest_scheduler_tests.py \
  engram/memory/daily_digest_tests.py engram/memory/weekly_digest_schedule_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/core/management/commands/engram_run_daily_digest_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python -m pytest engram/memory/legacy_producer_census_tests.py -q
docker compose -f deploy/compose/docker-compose.yml run --rm api \
  python manage.py check
git diff --check
```

Run focused tests sequentially; record exact counts and exit codes. If Compose
is unavailable, report the command and first decisive environment failure
instead of substituting a host-side success claim. The final census must be
run after all C1.3b/C1.3c producer changes are present.

## Non-Goals

- No changes to provider execution, digest publication, visibility quarantine,
  manual/rerun semantics, or the `WorkflowWork` schema.
- No leases, fencing, logical retry scheduler, reconciliation, or CP2 repair.
- No historical missing-work backfill or mutation of legacy digest rows.
- No late-arrival/carry-forward behavior for a frozen daily/weekly occurrence.
- No per-team schedule tuning, new beat transport, queue mirror, deployment,
  SSH, runtime repair, or D2 work.

## Stop Conditions And Acceptance

Stop before enabling a scheduler producer if its bucket depends on last-success
state, if a duplicate invocation can mutate a snapshot, if no-input emits a
task, if source selection occurs after work creation, or if a Celery payload
contains source ids/body/window data instead of stable work/run ids.

Stop if command and beat paths do not converge on one helper, if a foreign
project/team can enter a snapshot, if the census cannot distinguish retained
legacy adapters from active producers, or if the final zero-call result is not
reproducible from tracked source.

C1.3d is accepted only when bucket and scheduler RED tests pass, the command
and both beat tasks use C1.3c freeze/work/signal interfaces, duplicate windows
produce one immutable work identity and initial signal, empty windows are
terminal no-input, beat registrations remain stable, and the final census has
zero active legacy producer calls. C1.3d then observes one complete daily and
weekly cycle in the parent rollout gate; this spec does not perform deployment
or external observation itself.
