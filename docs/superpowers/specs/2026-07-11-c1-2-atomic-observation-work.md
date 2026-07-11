# C1.2 Atomic Observation Work

Date: 2026-07-11
Status: focused implementation specification
Roadmap: Checkpoint 1, C1.2 — hook/API atomic creation and all observation writers

Authoritative source sections (do not restate their broader contracts):

- `2026-07-09-autonomous-memory-loop-roadmap.md`, “Checkpoint 1 — Lossless
  Work Creation” (P1/P2, package ownership, C1.2 gate and rollout).
- `2026-07-10-checkpoint-1-lossless-work-creation.md`, “Raw Event Normalization
  Disposition”, “Server Sequence And Transaction Order”, “Policy Snapshot And
  Duplicate Repair”, “Work Creation Primitive”, “Versioned Task Boundary”,
  “C1.2 Writer Cutover”, and “C1.2 Hook/API” tests.

## Goal

For newly accepted hook evidence, commit the scoped raw envelope, normalized
observation/source, required `WorkflowWork`, and exactly one package-owned
id-only task row in one database transaction. A rollback leaves none of those
rows; a process ending after commit cannot erase them. Duplicate delivery
reuses evidence and logical work, repairs historical missing work once, and
never advances the server sequence twice.

All hook and import observation writers use a row-locked server sequence. New
v1 evidence carries an explicit normalization disposition. Accepted hook work
captures its realtime policy once and workers receive only stable IDs.

This slice changes no session-generation producer: lifecycle/session-end work
remains on the legacy path until C1.3.

## Non-goals and hard boundaries

- No leases, attempts, fencing, logical retry scheduling, reconciliation,
  repair command, or CP2 operational state.
- No C1.3 sequence backfill/non-null migration, active-to-ended primitive,
  idle sweep, digest work, schedules, reruns, or legacy task census.
- No new broker, relay, package status mirror, queue polling, or transport
  retry/dead-letter model. `django-celery-outbox` remains transport authority.
- No historical bulk repair. Duplicate repair is a single request-scoped
  missing-work repair only.
- No observation/session semantic promotion redesign, candidate coverage,
  atomic promotion, Memory CI, retrieval, or public `WorkflowWork` API.
- Imported observation/summary rows remain source-materialized: they are
  promoted by the existing import transaction and do not create observation or
  session work. Prompt-only imports create a typed raw `no_op/evidence_only`.
  Missing-session/unsupported rows create no raw event or fabricated disposition.
- Context bundle session creation only initializes/locks session state; it
  never creates observation work.

## Current code to replace or preserve

`IngestHookEvent.execute` currently checks duplicates before the transaction,
creates an unlocked session, omits v1 normalization and work policy, and uses
`transaction.on_commit()` for `process_observation_recorded` (and for legacy
`distill_session` on session end). C1.2 removes the hook callback only for
non-lifecycle observation work; the lifecycle callback remains until C1.3.

`ClaudeMemImporter` owns a surrounding transaction. Its observation/summary
writer currently creates raw/observation/source rows and immediately promotes
memory; its prompt writer creates only raw evidence. Preserve that behavior,
adding the sequence lock and v1 disposition. Lock touched sessions in sorted
UUID order before writing to avoid import/hook lock inversion.

`BuildContextBundle._get_or_create_session` creates/updates a session outside
the bundle transaction. It must explicitly initialize a new cursor to zero and
lock an existing session before any one-time legacy-team adoption; it creates
no observation, work, or task.

`create_work(CreateWorkflowWorkInput)` is the domain primitive. It requires an
active transaction, resolves the typed subject by organization/project,
derives the team, canonicalizes the snapshot, computes the v1 fingerprint, and
returns `(work, created)`. It never imports task functions or reads package
tables. `dispatch_work_task(task, work_id, workflow_run_id=None)` is the
approved id-only adapter; for automatic work it uses task id
`workflow-work:<work UUID>` and `apply_async(args=[str(work_id)], task_id=...)`.

The approved task is
`engram.memory.process_observation_work_v1(work_id, workflow_run_id=None)`.
Its payload is only `work_id` for this slice; it reloads and validates the
scoped observation and frozen fingerprint before invoking the existing
observation processor. The legacy task remains registered for draining but no
new C1.2 producer calls it.

## v1 evidence and policy contract

Every newly created hook/import raw envelope sets exactly one of:

| writer/input | normalization fields | work behavior |
|---|---|---|
| hook, including lifecycle | `version=1, disposition=observation,
  reason=NULL` and one same-scope source | non-lifecycle + captured realtime
  policy true creates observation work/task; lifecycle never does |
| imported observation/summary | `version=1, disposition=observation,
  reason=NULL` and one same-scope source | immediate existing import
  materialization; no observation/session work/task |
| imported prompt-only | `version=1, disposition=no_op,
  reason=evidence_only` and zero sources | no work/task |
| unsupported/missing-session import | no raw envelope | report outcome only |

The stored hook metadata contains exactly `work_policy_v1`:

```json
{
  "schema": "hook_work_policy/v1",
  "realtime_candidates_enabled": true,
  "legacy_policy_fallback": false
}
```

The policy is read from the resolved organization setting once, inside the
evidence transaction, before work creation. A later settings change never
reinterprets accepted evidence. Lifecycle classification uses the trusted
persisted adapter event type (`Observation.source_metadata.event_type`), not
the client observation type.

For a legacy duplicate with no policy, lock the evidence/session, read the
current scoped setting once, persist the same policy with
`legacy_policy_fallback=true`, and use that value for missing-work repair. Do
not upgrade its nullable normalization fields to v1. A duplicate with an
existing policy never re-reads settings.

## Interfaces and ownership

Add/reuse these narrow interfaces; names may vary only with an equivalent
typed signature and behavior:

```python
def lock_session_for_observation(
    *, organization_id: UUID, project_id: UUID, session_id: UUID,
) -> AgentSession

def allocate_observation_sequence(session: AgentSession) -> int

def create_work(data: CreateWorkflowWorkInput) -> tuple[WorkflowWork, bool]

def dispatch_work_task(
    task: object, work_id: UUID, workflow_run_id: UUID | None = None,
) -> object
```

`allocate_observation_sequence` runs only with the caller’s active atomic block
and locked session. If the nullable cursor is legacy, use the maximum existing
positive sequence (or zero), increment once, save the cursor, and return the
new value. Reuse of an existing observation returns its stored sequence and
does not advance the cursor. Client sequence numbers, timestamps, prompt
numbers, and UUID ordering are never authoritative.

Owned mutable files:

- `apps/backend/engram/hooks/services.py` and `hook_ingest_tests.py`;
- `apps/backend/engram/imports/services.py` and import service tests;
- `apps/backend/engram/context/services.py` and focused session tests;
- the observation-work task adapter/tests and post-cutover P1/P2 evaluator tests.

Do not edit C1.1 models/migrations/workflow identity, session sweep,
distillation/reconciler, digest/scheduler/rerun producers, or deployment files.

## Required transaction order

### Hook/API event

1. Resolve organization, project, effective team, agent identity, and session
   subject by scope. Reject foreign project/team before any product write.
2. Enter `transaction.atomic()`; lock the scoped session with
   `select_for_update(of=('self',))`. New-session creation uses an inner
   savepoint around the uniqueness race, then reloads and locks the winner.
3. Recheck idempotency/client-event duplicate while holding the session lock.
   For a duplicate, retain its raw event, observation, and sequence; ensure
   its source exists; then perform only the missing-work repair below.
4. For a new event, persist agent/session fields (new sessions explicitly set
   `observation_sequence_cursor=0`), allocate one server sequence, and persist
   raw event plus trusted adapter metadata.
5. Set the typed v1 normalization fields and create/reuse the observation with
   that same server sequence. Create exactly one same-scope `ObservationSource`.
6. If the trusted event is non-lifecycle and captured policy enables realtime,
   build the v1 observation snapshot (server content digest plus policy), call
   `create_work`, and require the returned subject/team/fingerprint to match.
7. Only when `created is True`, call `process_observation_work_v1.apply_async`
   through `dispatch_work_task` before leaving the atomic block. No
   `transaction.on_commit`, `delay`, broker call, evidence text, or secret may
   be used as the task payload.
8. Return the accepted result only after the outer transaction commits. A
   database/package insertion exception aborts the entire transaction.

### Duplicate missing-work repair

The duplicate path is idempotent and scope-locked. If the persisted policy
requires realtime and the observation is non-lifecycle, call `create_work`.
If it returns a new work row, create exactly one id-only outbox row in the same
transaction. If it returns an existing work row, emit nothing. A terminal work
row is not re-signaled. Never inspect package rows to infer completion or need.

### Import batch

The existing `_apply_import` transaction remains the owner. Resolve and verify
organization/project/team first. Before creating imported observations, lock all
touched sessions by UUID ascending order; use the same allocator and explicit
cursor-zero default for new sessions. For each source row, create/reuse raw
event, observation, source, and typed normalization in that transaction, then
run the existing immediate materialization/promotion. Do not call an
observation task, create `WorkflowWork`, or use `on_commit`. A repeated import
reuses the source/evidence/sequence and does not increment the cursor.

### Context bundle session

Resolve scope before reads. For a new session, write cursor zero. For an
existing session, lock it in a short atomic block before adopting a legacy null
team; a different non-null team fails closed. Bundle creation remains its own
transaction and creates no raw event, observation, work, or task.

## Failure matrix and RED tests

Each row must be a focused failing test before implementation, then GREEN:

| boundary | expected result |
|---|---|
| exception after raw/observation/source, before commit | no raw, observation, source, work, or outbox row |
| exception after work/task insert, before commit | same complete rollback |
| request/process ends immediately after commit | all committed rows remain and task payload is recoverable |
| broker unavailable/relay stopped | PostgreSQL evidence/work/outbox intent commits; no broker dependency |
| outer transaction wraps ingest and reads before commit | rows are visible in that transaction; callback list stays empty |
| duplicate with historical missing work | one repaired `WorkflowWork` and one id-only outbox row |
| second duplicate after repair | no second work, sequence, raw event, or outbox row |
| concurrent duplicate submissions | one evidence identity, sequence, work identity, and initial signal |
| duplicate existing observation | stored sequence reused; cursor unchanged |
| realtime disabled or lifecycle event | v1 observation/source only; no observation work/task |
| setting changes after acceptance | worker uses frozen policy, not current setting |
| legacy duplicate without policy | fallback policy persisted once with `legacy_policy_fallback=true`; normalization remains legacy |
| client sequence/time altered | server sequence remains monotonic under session lock |
| import prompt-only / unsupported session | typed no-op or report-only outcome; no fabricated observation/work |
| import repeated/concurrent sessions | sorted lock order, idempotent sequence, no deadlock/duplicate |
| foreign project/team | denial before evidence, work, task, provider, or source reads |
| malformed v1 disposition/source cardinality | database/service rejects before acceptance |
| task payload inspection | exact task name and only `[work_id]`; no text, prompt, provider result, or secret |
| legacy task callsite scan | no new C1.2 producer invokes `process_observation_recorded` |

Replace current hook tests that expect zero outbox rows inside the surrounding
transaction or post-commit dispatch. Keep lifecycle `distill_session` callback
expectations explicitly marked C1.3 compatibility behavior.

## Development gate

Development may start only after C1.1 schema/identity/versioned-task tests are
merged on the same branch base and the amended migration is usable on a fresh
database plus a seeded legacy-null database. No C1.3 migration or historical
repair is required to run these tests. The gate is closed when:

- focused RED/GREEN tests above pass, including transaction rollback and
  concurrent duplicate cases;
- `WorkflowWork` identity tests prove scope, fingerprint, policy snapshot, and
  id-only task payload;
- typed post-cutover P1/P2 checks are green while legacy gaps remain reported;
- `git diff --check`, Ruff, format, migration freshness, and focused pytest
  pass; no `on_commit` remains on the migrated hook path;
- a read-only producer census shows lifecycle/import/scheduler producers are
  unchanged and C1.3 is not accidentally activated.

## CI commands

Run from `apps/backend` (the repository CI uses PostgreSQL service and Poetry):

```text
poetry check
poetry run ruff check .
poetry run ruff format --check .
poetry run python manage.py migrate --noinput --settings=settings.test_settings
poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
poetry run pytest engram/hooks/hook_ingest_tests.py engram/imports/services_tests.py \
  engram/imports/batch_services_tests.py engram/context/services_tests.py \
  engram/memory/workflow_work_tests.py engram/memory/tasks_tests.py -q
poetry run pytest -q
```

Record commands, exit codes, migration freshness, focused test count, full-test
result, and review findings. Do not claim full CI when PostgreSQL or Poetry is
unavailable.

## Stop conditions

Stop and escalate before editing beyond this file group if any of the
following is true: package `.apply_async` cannot persist in the caller’s
transaction; a producer must use `on_commit` or task payload data; scope/team
cannot be resolved before writes; duplicate repair cannot prove exact
observation/policy identity; session lock order would invert across hook/import;
client ordering is required; imported materialization would need fabricated
work; a lifecycle producer must move before C1.3; tests require non-null
sequence/backfill, CP2 leases/retry/reconciliation, digest/scheduler changes,
historical bulk repair, deployment mutation, or a public API; or any rollback
would delete/reinterpret evidence.

Completion is the spec plus the focused implementation/tests and recorded
development evidence. Deployment, SSH, runtime migration, and D2 work are not
part of C1.2.
