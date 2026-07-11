# C1.3a Deterministic Session-Sequence Backfill

Date: 2026-07-11

Status: implementation-ready focused specification

Roadmap slice: Checkpoint 1, C1.3a

Depends on:

- `docs/superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md`;
- `docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`;
- migration `apps/backend/engram/core/migrations/0032b_agentsession_end_work_db_default.py`;
- the C1.2 writer cutover and its post-cutover sequence tests.

## Goal

Assign one deterministic, positive, unique `Observation.session_sequence` to
every existing observation and set `AgentSession.observation_sequence_cursor`
to the session maximum. The operation is resumable, idempotent, and safe to
rerun after a process or database failure. It prepares the nullable expand
schema for C1.3b; it does not install the non-null contract or switch session
lifecycle producers.

## Success boundary

After the migration succeeds:

- every observation has `session_sequence >= 1`;
- sequences are contiguous `1..N` within each session, in `(created_at, id)`
  order, where `N` is the observation count;
- every session cursor is `N`, or `0` when the session has no observations;
- `(session, session_sequence)` is unique and the expand checks remain valid;
- rerunning the migration performs no writes for an already consistent session;
- a failure leaves completed earlier sessions committed, the failing session
  unchanged, and the migration unapplied so the same command can resume;
- no production process, queue, transport row, raw event, or workflow work is
  inspected or mutated by this slice.

The C1.3b contract migration is the only consumer allowed to make the cursor
and sequence fields non-null. C1.3a must finish before that migration is
invoked.

## Data and ordering contract

The migration uses historical models obtained from `apps.get_model('core', ...)`:

- `AgentSession` is the locked parent row;
- `Observation` is the child row set filtered by `session_id`.

For each session, the authoritative order is ascending `created_at`, then
ascending UUID `id`. The client sequence, prompt number, observed timestamp,
event time, content hash, and UUID lexical order by itself are not ordering
inputs. A tie on `created_at` is always broken by `id`.

The migration must first verify whether the session is already normalized:
the ordered child rows contain exactly `1, 2, ..., N`, no value is null or
non-positive, and the parent cursor equals `N` (including `0` for an empty
session). A normalized session is skipped without updating either table.

For a session requiring repair, all child sequences are set to null before
new values are assigned. Values `1..N` are then assigned in the authoritative
order and the parent cursor is written as `N`. The temporary null state is
legal because the expand schema is nullable; clearing first also prevents the
conditional unique constraint from colliding with old or duplicate numbers.

## Migration shape and interfaces

Create exactly:

`apps/backend/engram/core/migrations/0033_backfill_observation_sequence.py`

The module exposes these reviewed constants and callables for migration tests:

```python
MAX_OBSERVATIONS_PER_SESSION = 10_000
UPDATE_BATCH_SIZE = 500
SESSION_LOCK_TIMEOUT = '5s'
STATEMENT_TIMEOUT = '60s'

def backfill_observation_sequences(apps, schema_editor) -> None: ...
def noop_reverse(apps, schema_editor) -> None: ...
```

`Migration.dependencies` is exactly
`[('core', '0032b_agentsession_end_work_db_default')]`. The class sets
`atomic = False` and has one operation:

```python
migrations.RunPython(backfill_observation_sequences, noop_reverse)
```

The migration is the only execution interface. No new public API, Celery task,
transport integration, or management command is added. The operational command
is the standard, resumable Django invocation:

```text
python manage.py migrate core 0033_backfill_observation_sequence --noinput
```

Do not use `--fake`; the migration is considered applied only after every
session passes its invariant. A direct test may call the exported function
through `MigrationExecutor`, but runtime code must not import migration
modules.

## Preflight, caps, and no-mutation failure

Before opening any per-session mutation transaction, run a read-only cap
preflight over all sessions. It must identify every session with more than
`MAX_OBSERVATIONS_PER_SESSION` observations and fail before changing any row.
The failure is a `RuntimeError` whose message includes the first offending
session UUID and its count; the migration remains unapplied.

The cap is deliberately hard-coded at 10,000. The implementation must not
silently truncate, split, or partially normalize an oversized session. A
future large-session path requires a separate reviewed slice and migration;
C1.3a stops instead.

The preflight may use an aggregate count query, but it must be deterministic:
offending sessions are ordered by UUID and the first one is reported. It must
not acquire row locks, call Celery, or query package/outbox tables.

## Per-session transaction and lock budget

After preflight, iterate session UUIDs in ascending UUID order. For each UUID,
open a fresh `transaction.atomic(using=connection.alias)` block and execute:

```sql
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
```

The session row is selected with `select_for_update(of=('self',))` before any
child read or write. Only the session row is locked; no global advisory lock,
table lock, or cross-session lock is allowed. All child reads, the null reset,
the batched updates, and the cursor update occur inside this same transaction.

Load only `id` and `session_sequence`, ordered by `created_at`, `id`. Use
`bulk_update(..., ['session_sequence'], batch_size=UPDATE_BATCH_SIZE)` for
chunks of at most 500 rows. Set the cursor with a scoped queryset update (or
an equivalent historical-model save) after all child batches. Do not hold a
database lock while doing work for another session.

The timeout settings apply to every statement in the session transaction. A
lock timeout, statement timeout, serialization error, missing parent, or
other database error rolls back only that session's transaction. The migration
must emit completed, skipped, and failed counts, then re-raise the first
failure so Django leaves migration `0033` unapplied. It must not continue and
claim success with an incomplete cohort.

## Restart and resume invariants

Non-atomic migration execution commits each successful session independently.
On a rerun, the sorted scan repeats the same normalization check and skips
every session whose sequence list and cursor are already consistent. A session
that failed before commit is observed as its pre-attempt state and is fully
repaired on the next run. A session that committed is never renumbered again.

The implementation must not use a timestamp, row count, offset, random UUID,
or process-local cursor as a resume token. The only resume state is the data
itself: deterministic child ordering plus the parent cursor invariant.

After the migration command exits successfully, run these read-only assertions
before C1.3b:

```text
COUNT(observation.session_sequence IS NULL) = 0
MIN(observation.session_sequence) >= 1 for every non-empty session
COUNT(DISTINCT (session_id, session_sequence)) = observation row count
session.observation_sequence_cursor = MAX(sequence), or 0 for empty sessions
```

The assertions must be scoped to all organizations/projects and must report
the first violating session instead of silently coercing it.

## Old-writer drain precondition (CI/dev contract)

C1.3a assumes the C1.2 cutover has retired every observation writer that can
insert without the locked server allocator. This is a release/CI contract,
not an operation performed by the migration itself.

Before invoking `manage.py migrate` in CI or a development rehearsal, the
pipeline must prove all of the following from the exact candidate revision:

1. a static call-site census finds every observation creation path and each
   path uses the session-row lock plus cursor allocator; migration fixtures
   and test factories are the only exceptions;
2. the C1.2 writer-cutover tests pass, including concurrent append versus
   allocator and duplicate reuse tests;
3. the deployment evidence records that old API/worker/import/command
   processes are stopped and no transaction from a retired revision remains;
4. the migration image contains `0032b` and `0033`, while no C1.3b contract
   migration or lifecycle producer switch is present.

The migration itself must not inspect process lists, Celery workers, broker
queues, deployment state, or transaction metadata; must not stop/drain a
writer; and must not take a process-wide or database-wide lock to simulate a
drain. If the CI/dev evidence is missing, the gate fails before migration
invocation. Production rollout tooling owns the evidence and is outside this
file.

## Exact RED tests

Add migration-focused tests to
`apps/backend/engram/core/migrations_tests.py`. Keep the existing
`MigrationExecutor` pattern, use `@pytest.mark.django_db(transaction=True)`,
and restore the original leaf migration in `finally` blocks.

1. `test_0033_orders_by_created_at_then_id_and_sets_cursor`: create one
   session with tied and untied timestamps; migrate to `0033`; assert ordered
   rows have sequences `1..N` and cursor `N`.
2. `test_0033_sets_zero_cursor_for_empty_session`: migrate an empty session and
   assert cursor `0` with no observation rows.
3. `test_0033_repairs_null_duplicate_and_wrong_sequences`: seed null, duplicate,
   and out-of-order values plus a wrong cursor; assert a contiguous deterministic
   result and no uniqueness error.
4. `test_0033_preflight_cap_aborts_before_any_session_mutation`: make an earlier
   session inconsistent and a later session with 10,001 observations; assert
   migration raises before the earlier session changes and `0033` is unapplied.
5. `test_0033_uses_500_row_batches`: create 1,001 observations, instrument the
   migration's historical queryset `bulk_update`, and assert batch sizes are
   at most 500 and the final sequence/cursor invariant holds.
6. `test_0033_skips_consistent_session_on_rerun`: apply `0033`, reverse only
   `0033` (its reverse is a no-op), reapply it, and assert child and cursor
   values plus their `updated_at` values are unchanged.
7. `test_0033_failed_session_rolls_back_and_prior_sessions_remain_committed`:
   hold the second session row in a separate PostgreSQL connection, reduce
   `SESSION_LOCK_TIMEOUT` for the test, and assert the first session remains
   normalized while the second remains unchanged and the migration is
   unapplied.
8. `test_0033_retry_after_failure_resumes_without_renumbering`: release the
   lock, rerun the migration, and assert the first session is skipped and the
   second receives exactly `1..N` with cursor `N`.
9. `test_0033_does_not_use_client_or_prompt_order`: seed misleading prompt and
   observed values; assert only `(created_at, id)` determines the sequence.
10. `test_0033_has_non_atomic_runpython_and_noop_reverse`: inspect the migration
    class and assert `atomic is False`, the dependency is `0032b`, and reverse
    execution does not null or delete sequence data.

The RED phase must add these tests before implementing `0033`; the first run
must fail because the migration module and behavior do not yet exist. The
GREEN phase reruns the focused file and records exact pass counts.

## Ownership and file boundary

The C1.3a owner may create or modify only:

- `apps/backend/engram/core/migrations/0033_backfill_observation_sequence.py`;
- `apps/backend/engram/core/migrations_tests.py` for the tests listed above;
- this specification during review.

No edits belong in `core/models.py`, `0032`, `0032b`, hook/import/session
writers, `session_sweep.py`, distillation services/tasks, workflow work, or
deployment tooling. The git owner alone stages, commits, publishes, and runs
release operations. C1.3b owns the contract migration and lifecycle producers.

## Non-goals

C1.3a does not:

- make either field non-null or add a default to `observation_sequence_cursor`;
- create or repair `WorkflowWork`, `WorkflowRun`, raw-event dispositions, or
  package/outbox rows;
- create a management command, scheduler, Celery task, lease, retry record, or
  reconciliation state;
- change ordering for new observations or alter the C1.2 allocator;
- drain, stop, pause, or inspect external writers or transport;
- normalize only a requested organization/project subset;
- process sessions above the cap or fabricate missing observations;
- implement session end, idle sweep, distillation, late-input generation, or
  any C1.3b/C1.3c behavior.

## Rollback

`noop_reverse` is intentional. Reversing `0033` must preserve the assigned
sequences and cursor; deleting or nulling them would destroy the deterministic
input prepared for the next slice. If a failed migration needs a retry, rerun
the exact migration command after correcting the lock/statement failure; prior
committed sessions are safe to skip. Do not mark the migration fake.

If code must be rolled back before C1.3b, keep the nullable expand schema and
the backfilled values in place, deploy a forward-compatible writer image, and
rerun the migration if needed. Dropping the expand schema is only the reviewed
rollback of the entire pre-contract expand history and is outside this slice.

## Container and CI gates

Run sequentially in the backend Compose container (never host Python tests once
Compose is available):

```text
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -q engram/core/migrations_tests.py"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run && python manage.py check"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "ruff check engram/core/migrations/0033_backfill_observation_sequence.py engram/core/migrations_tests.py && ruff format --check engram/core/migrations/0033_backfill_observation_sequence.py engram/core/migrations_tests.py"
```

The CI gate additionally runs the existing backend workflow's full pytest,
Ruff, and format jobs, plus the C1.2 writer-cutover/static-census contract.
The slice is green only when migration apply, reverse/no-op verification,
freshness, Django checks, focused tests, and the writer-drain evidence all
pass. Record commands, exit codes, exact test counts, migration revision, and
any lock-timeout rehearsal in the checkpoint handoff.
