# Slice R2 — Distillation Backfill Ops Command (`engram_backfill_distillations`)

Status: design. Depends on R1 (output-budgeted reduce batching + first-class
truncation) being deployed first. Dogfood context: no prod compat, stop-the-world
deploy, in-flight distillation work droppable. Steady-state correctness
(idempotency, deterministic replay through the fence) is NOT waived.

## Problem and Evidence

On prod (verified 2026-07-21) sessions with >100 observations fail distillation
~100% (1 success / 153 works). 1306 `provider_output_malformed` runs across 77
sessions since 07-15, all on REDUCE stages. Root cause and fix are R1. R2 is the
ops recovery: re-drive the works that piled up terminal/blocked failures so their
sessions produce candidates once R1 lands.

Verified mechanics that constrain the recovery design:

- Failure code is stored on the run. `_malformed_failure()` builds
  `ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code=PROVIDER_OUTPUT_MALFORMED)`
  (`memory/distillation_provider_stage.py:1010-1011`, constant `PROVIDER_OUTPUT_MALFORMED = 'provider_output_malformed'` at `:55`). It reaches the
  `WorkflowRun` via `DistillationStageError` → `_record_claim_failure`
  (`memory/tasks.py:161-169`) → `fail_work_claim` → `_apply_failure_run`, which
  writes `run.failure_class='provider_transient'`, `run.failure_code='provider_output_malformed'`
  (`memory/work_execution.py:844-863`). So selecting on `WorkflowRun.failure_code`
  is exact.

- `provider_account_unavailable` is `CONFIGURATION` class, code
  `provider_account_unavailable` (`memory/work_failures.py:89-91`), produced by the
  402/401/403 window.

- Work state after failure (`_apply_failure_work`, `memory/work_execution.py:866-902`),
  for REQUIRED works:
  - `CONFIGURATION` → `BLOCKED` with `blocked_configuration_fingerprint`.
  - `INVALID_INPUT` → `TERMINAL_FAILURE`.
  - `failure_streak >= _failure_streak_limit()` (default **12**, env
    `ENGRAM_WORK_FAILURE_STREAK_LIMIT`, `:37-41`) → `TERMINAL_FAILURE`.
  - else → `RETRY_WAIT` with `next_retry_at = now + retry_backoff(...)`
    (`PROVIDER_TRANSIENT` backoff caps at 1800s, `memory/work_failures.py:22-27`).

- v1 SESSION_DISTILLATION retries are driven by **`session_work_reconciler`**, not
  `RetryFailedDistillations`. `RetryFailedDistillations` explicitly
  `.exclude(_v1_managed_session())` (`memory/distillation_reconciler.py:111`,
  `_v1_managed_session` = sessions with `end_work_contract_version=1`,
  `:51-57`) — it only services legacy v0 sessions. The malformed works are all
  v1 (extract/reduce contract is v1), so that reconciler never re-drives them.

- `session_work_reconciler._classify` (`memory/session_work_reconciler.py:161-194`):
  - `TERMINAL_FAILURE` → `TERMINAL_INPUT_FAILURE` → `report_only`, **not** in
    `_AUTO_REPAIR_CODES` (`:51-59`). These works are stranded forever.
  - `BLOCKED` → `_classify_blocked` (`:131-135`): auto-repairs (`CONFIGURATION_CHANGED`,
    which `_clear_block`s then queues) only when the current execution-config
    fingerprint differs from `blocked_configuration_fingerprint`; otherwise
    `CONFIGURATION_BLOCKED` → `report_only`. After a 402 window, whether these
    self-heal depends on whether the fingerprint actually moved — we do not rely on
    it; stragglers still `provider_account_unavailable` must be force-cleared.
  - `RETRY_WAIT` with `next_retry_at <= as_of` → `LOGICAL_RETRY_DUE` → auto-repairs.
    These would self-heal on the next reconcile after R1; R2 force-drives them now.

- Dispatch is `queue_work_attempt` (`memory/work_dispatch.py:93-140`): creates (or
  resignals within a 5-min window) a QUEUED v1 run and `send_task`s
  `engram.memory.distill_session_work_v1`. It does **not** check attempt budget —
  budget is enforced only by `claim_work._short_circuit_state`
  (`memory/work_execution.py:460-477`): a work in `TERMINAL_FAILURE` (absorbed) or
  `RETRY_WAIT` with `now < next_retry_at` short-circuits and the queued run is never
  claimed. Therefore re-driving a stranded work **requires resetting its execution
  state first**, or the dispatch is inert.

Conclusion: the command must (1) select works whose latest v1 run FAILED with a
target code, (2) reset the execution-state bookkeeping that the fence uses to
short-circuit, through a documented sanctioned reset, then (3) dispatch via the
existing `queue_work_attempt`. It must not touch identity/contract fields.

## Design

Decisions:

1. **Thin command over a small service module.** New module
   `engram/memory/distillation_backfill.py` owns selection + per-work redrive
   (testable without the CLI); `core/management/commands/engram_backfill_distillations.py`
   is a thin argparse/IO wrapper. Mirrors the reconciler/command split already in
   the repo.
   - Rejected: put all logic in `Command.handle` — untestable without `call_command`
     plumbing and violates the one-view-per-file spirit.

2. **Reset is a new, documented sanctioned path** — `reset_work_for_redrive(work)` —
   not a raw `.update()` scattered in the command. It generalizes the existing
   `_clear_block` (`session_work_reconciler.py:354-367`, which already resets
   `execution_state=READY`, `blocked_configuration_fingerprint=''`,
   `failure_streak=0`) to also apply from `TERMINAL_FAILURE`/`RETRY_WAIT` and to
   clear `next_retry_at` and lease fields. It touches only recovery bookkeeping.
   - Rejected: mark old failed runs as poisoned-cleanup so the reconciler re-counts
     (the v0 `_terminalize_poisoned_runs` trick) — irrelevant here because v1 budget
     is `failure_streak` on the work, not a run count.
   - Rejected: bump `ENGRAM_WORK_FAILURE_STREAK_LIMIT` env instead of resetting —
     global, non-idempotent, does not clear `TERMINAL_FAILURE`/`BLOCKED` state.

3. **Fields the reset must NOT touch** (fence/contract integrity): `fencing_token`
   (monotonic; next `claim_work` increments it, `work_execution.py:439`),
   `contract_version`, `input_snapshot`, `input_fingerprint`, `disposition` (stays
   `REQUIRED`), `resolution_reason`/`resolved_at` (null, work never settled),
   `subject_*`, `occurrence_key`, `team_id`. KNOWN TRAP honored: `contract_version`
   defaults are LIVE semantics — untouched.

4. **Selection by latest v1 run**, computed with `Subquery` annotations, filtered to
   `latest_status=FAILED AND latest_code IN codes`. Because a successful re-drive
   makes the newest run QUEUED, the same work will not re-select — idempotency falls
   out of the data model, no cursor/state file.

5. **Reset+dispatch under `select_for_update`** with a re-check of the latest run
   inside the lock (selection→lock race with a concurrent reconcile). If the latest
   run is no longer FAILED-in-set, skip with a reason. Deterministic, no double
   dispatch.

6. **Origin = `WorkflowRunOrigin.RECONCILIATION`.** This is automatic recovery, and
   RECONCILIATION runs are absorbed as terminal on redelivery
   (`work_execution.py:559`) — the right semantics. (MANUAL origin would defeat
   `absorb_terminal` and is for operator single-shot reruns.)

7. **Cost self-throttles at the engine.** Each attempt is capped at
   `max_provider_calls_per_attempt()` (default **8**, cap 64,
   `distillation_window.py:40-71`); a giant session advances across many
   `continue_required` runs rather than one huge fan-out. The command's `--limit`/
   `--sleep` throttle *dispatch rate*, not per-work cost.

## API / Contract Changes

No public API, no migration, no model change. New internal module + command.

### `engram/memory/distillation_backfill.py`

```python
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.aware_time import require_aware
from engram.memory.work_dispatch import queue_work_attempt

logger = structlog.get_logger(__name__)

DEFAULT_FAILURE_CODES = (
    'provider_output_malformed',
    'provider_output_truncated',
    'provider_account_unavailable',
)

_RESET_FIELDS = (
    'execution_state',
    'failure_streak',
    'next_retry_at',
    'blocked_configuration_fingerprint',
    'lease_owner',
    'lease_expires_at',
    'heartbeat_at',
    'updated_at',
)


@dataclass(frozen=True, slots=True)
class BackfillTarget:
    work_id: uuid.UUID
    session_id: uuid.UUID
    latest_run_id: uuid.UUID
    failure_code: str
    execution_state: str


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    dispatched: tuple[uuid.UUID, ...] = ()
    skipped: tuple[tuple[uuid.UUID, str], ...] = ()
```

### Selection

```python
def select_targets(
    *,
    failure_codes: tuple[str, ...],
    limit: int,
    organization_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> list[BackfillTarget]:
    latest = (
        WorkflowRun.objects.filter(
            work_id=OuterRef('id'),
            execution_contract_version=1,
        )
        .order_by('-created_at', '-id')
    )
    works = (
        WorkflowWork.objects.filter(
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
            contract_version=1,
            disposition=WorkflowWorkDisposition.REQUIRED,
        )
        .annotate(
            latest_run_id=Subquery(latest.values('id')[:1]),
            latest_status=Subquery(latest.values('status')[:1]),
            latest_code=Subquery(latest.values('failure_code')[:1]),
        )
        .filter(
            latest_status=WorkflowRunStatus.FAILED,
            latest_code__in=failure_codes,
        )
        .order_by('created_at', 'id')
    )
    if organization_id is not None:
        works = works.filter(organization_id=organization_id)
    if project_id is not None:
        works = works.filter(project_id=project_id)

    return [
        BackfillTarget(
            work_id=work.id,
            session_id=work.subject_id,
            latest_run_id=work.latest_run_id,
            failure_code=work.latest_code,
            execution_state=work.execution_state,
        )
        for work in works[:limit]
    ]
```

`--limit` is applied as a slice; re-running drains the next batch (each dispatched
work drops out because its newest run becomes QUEUED).

### Sanctioned reset + redrive (one work, one transaction)

```python
def reset_work_for_redrive(work: WorkflowWork) -> None:
    work.execution_state = WorkflowWorkExecutionState.READY
    work.failure_streak = 0
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.lease_owner = ''
    work.lease_expires_at = None
    work.heartbeat_at = None
    work.save(update_fields=list(_RESET_FIELDS))

    return


def redrive_target(
    *,
    work_id: uuid.UUID,
    failure_codes: tuple[str, ...],
    now: datetime,
) -> uuid.UUID | None:
    require_aware(now, field='now')

    with transaction.atomic():
        try:
            work = WorkflowWork.objects.select_for_update().get(
                id=work_id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                contract_version=1,
                disposition=WorkflowWorkDisposition.REQUIRED,
            )
        except WorkflowWork.DoesNotExist:
            return None

        latest = (
            WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
            .order_by('-created_at', '-id')
            .first()
        )
        if (
            latest is None
            or latest.status != WorkflowRunStatus.FAILED
            or latest.failure_code not in failure_codes
        ):
            return None

        reset_work_for_redrive(work)
        run = queue_work_attempt(
            work_id=work.id,
            now=now,
            origin=WorkflowRunOrigin.RECONCILIATION,
        )
        if run.dispatched_at != now:
            return None

        logger.info(
            'distill_backfill_redriven',
            work_id=str(work.id),
            session_id=str(work.subject_id),
            run_id=str(run.id),
            prior_state=work.execution_state,
        )

        return run.id
```

Return `None` = skip (already re-dispatched by a concurrent reconcile, or a resignal
returned the existing queued run with a stale `dispatched_at`). `queue_work_attempt`
runs its own nested `transaction.atomic()`; nesting a `select_for_update` on the same
work in the outer txn is safe (same connection, reentrant savepoint).

### Command `engram_backfill_distillations.py`

```
--failure-codes  comma list; default 'provider_output_malformed,provider_output_truncated,provider_account_unavailable'
--limit          int, default 100 (dispatches per invocation)
--sleep          float seconds between dispatches, default 0.0
--dry-run        select and print only, no state change, no dispatch
--organization   optional UUID scope
--project        optional UUID scope
```

`handle`:
1. Parse `--failure-codes` into a tuple; validate non-empty.
2. `targets = select_targets(...)`.
3. If `--dry-run`: print one line per target
   (`work=<id> session=<id> state=<state> code=<code> latest_run=<id>`) then
   `selected=<n> dispatched=0 skipped=0 dry_run=1` and return.
4. Else loop targets: `run_id = redrive_target(work_id=..., failure_codes=..., now=timezone.now())`;
   accumulate dispatched/skipped; `time.sleep(--sleep)` between iterations when > 0.
5. Print structured summary: `selected=<n> dispatched=<d> skipped=<s>` followed by
   one `skipped work=<id> reason=<reason>` line per skip.

Structured logs use keyword fields (`distill_backfill_started`,
`distill_backfill_redriven`, `distill_backfill_summary`).

## Data Flow

1. `select_targets` reads `WorkflowWork` + latest v1 `WorkflowRun` (annotated),
   filtered to REQUIRED distillation works whose newest run FAILED with a target
   code. No writes.
2. Per target, `redrive_target` locks the work, re-verifies the latest run under the
   lock, `reset_work_for_redrive` (READY, streak 0, cleared retry/lease/block),
   `queue_work_attempt` (new QUEUED v1 run + `send_task`).
3. The Celery worker later claims the QUEUED run via the normal fenced path
   (`claim_work` → `_do_claim`, token+1), executes windows/reductions under R1's
   fixed batching, and either completes (`continue_required`/`product_*`) or fails
   into the normal state machine again.
4. Re-running the command picks up only works still FAILED-in-set (giant sessions
   that need more `continue_required` cycles have their newest run non-FAILED while
   in flight, so they are not re-dispatched mid-flight).

## Error Handling

- Work vanished / not REQUIRED / not v1 between select and lock → skip
  (`DoesNotExist` or filter miss), reason `not_eligible`.
- Latest run no longer FAILED-in-set under the lock → skip, reason
  `already_redispatched`.
- `queue_work_attempt` returned an existing queued run outside the resignal window
  (stale `dispatched_at`) → skip, reason `resignal_returned_existing`.
- `--failure-codes` empty after parse → command error (non-zero exit), message
  `at least one failure code is required`.
- No base-`Exception` catch. DB errors propagate and abort the invocation; the
  command is re-runnable, so a partial batch is safe.
- `require_aware(now)` guards the timezone contract, consistent with dispatch.

## Test Plan (TDD order)

New file, colocated:
`apps/backend/engram/core/management/commands/engram_backfill_distillations_tests.py`
(command integration via `call_command`) and
`apps/backend/engram/memory/distillation_backfill_tests.py` (service unit).

Fixtures build works through the **real** `EndSession` service and drive
`claim_work`/`fail_work_claim` to reach each state (the pattern in
`session_work_reconciler_tests.py`), so fingerprints and run history are authentic —
stubs over mocks; typed fixtures; `f_`/`m_` only for injected args.

Helpers (typed):
- `f_scope: tuple[Organization, Project, AgentSession]` — scope + ended v1 session
  with N seeded observations (reuse `observation_work_tests.create_scope`,
  `_seed`, `EndSession`).
- `_fail_work(work, *, code, failure_class, times)` — claim+`fail_work_claim` a
  `ClassifiedWorkFailure` `times` times to accumulate `failure_streak`.
- `_latest_run(work)` helper.

`distillation_backfill_tests.py`:
1. `test_select_targets_picks_latest_failed_in_set` — work whose latest run failed
   `provider_output_malformed` is selected; a work whose latest run SUCCEEDED (older
   failed) is not; a work whose latest failure code is outside the set is not.
2. `test_select_targets_scope_and_limit` — org/project filter narrows; `--limit`
   equivalent slice caps count; deterministic `created_at,id` order.
3. `test_redrive_resets_terminal_failure_and_dispatches` — drive streak to 12 →
   `TERMINAL_FAILURE`; `redrive_target` → work `READY`, `failure_streak=0`,
   `next_retry_at=None`, and a new QUEUED v1 run exists (assert on `WorkflowRun`
   and the `CeleryOutbox` / `send_task` signal, as reconciler tests do).
4. `test_redrive_clears_blocked_config` — fail `provider_account_unavailable`
   (CONFIGURATION) → `BLOCKED` with fingerprint; redrive clears
   `blocked_configuration_fingerprint`, sets `READY`, dispatches.
5. `test_redrive_preserves_identity_fields` — assert `fencing_token`,
   `contract_version`, `input_fingerprint`, `input_snapshot`, `disposition` unchanged
   by the reset.
6. `test_redrive_idempotent_second_call_skips` — after redrive the newest run is
   QUEUED; a second `redrive_target` returns `None` (already_redispatched), no second
   dispatch.
7. `test_redrive_reset_reclaimable` — after redrive, `claim_work` succeeds
   (`_do_claim`, token+1) proving the reset actually lifts the short-circuit that
   stranded it.

`engram_backfill_distillations_tests.py`:
8. `test_dry_run_prints_selection_no_state_change` — `--dry-run` lists targets,
   `dispatched=0`, work state unchanged, no queued run created.
9. `test_command_dispatches_and_summary` — full run reports
   `selected/dispatched/skipped` matching reality.
10. `test_command_limit_throttle` — `--limit 1` dispatches one, leaves the rest;
    re-run drains the next.
11. `test_command_sleep_arg_accepted` — `--sleep 0` path (patch `time.sleep` via a
    stub to assert call count == dispatches-1, no real delay).
12. `test_command_custom_failure_codes` — `--failure-codes provider_output_truncated`
    selects only truncated-latest works.
13. `test_command_empty_failure_codes_errors` — empty/blank arg → non-zero exit.

Run (unique compose project to avoid the shared `engram-test` DB collision):
```
docker compose -p engram-r2-backfill run --rm app pytest -q \
  engram/memory/distillation_backfill_tests.py \
  engram/core/management/commands/engram_backfill_distillations_tests.py
```

## Ops

Precondition: **R1 must be deployed first.** Re-driving before R1 lands re-runs the
same output-unbounded reduce and re-fails `provider_output_malformed`, burning
provider spend for nothing.

Recovery run (recommended):
```
# 1. Dry-run to size the batch and eyeball the selection.
docker compose run --rm app python manage.py engram_backfill_distillations --dry-run

# 2. Drain in throttled batches (idempotent, re-runnable).
docker compose run --rm app python manage.py engram_backfill_distillations --limit 25 --sleep 2
# repeat until dry-run reports selected=0
```

Cost estimate (order-of-magnitude, honest):
- Population: ~77 sessions / 125 works with malformed-latest, plus
  `provider_account_unavailable` stragglers. Most are the >100-observation sessions.
- Per attempt is capped at `max_provider_calls_per_attempt()=8`
  (`distillation_window.py:41`). A work advances across multiple
  `continue_required` runs; it is not a single unbounded fan-out.
- Rough per-session calls: `extract` ≈ ceil(total_observation_chars / 40000)
  (chunk_char_budget, `distillation_window.py:46`) plus reduce levels
  (R1: bounded ≤4 levels). A ~100-observation session ≈ tens of calls total,
  spread over several dispatched runs.
- Giant sessions: the 8248-observation session fans to many windows; at 8
  calls/attempt it needs dozens of `continue_required` runs to complete. Drive it
  with a small `--limit` and non-zero `--sleep` so the worker pool and provider
  quota absorb it gradually. Watch provider spend during the first batch and adjust
  `--sleep`.
- Whole backfill: hundreds-to-low-thousands of provider calls total; dominated by
  the handful of giant sessions. No single command invocation should dispatch more
  than `--limit` works.

Env levers already in place: `ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT`
(per-attempt cap), `ENGRAM_WORK_FAILURE_STREAK_LIMIT` (terminalization budget) — do
not change for the backfill; the reset handles the budget per-work.

### Verification queries (run after each batch and at the end)

Ordinary read-only SQL against the app DB (adjust to psql/console tooling):

```sql
-- (V1) Zero works remain whose LATEST v1 run failed with a target code.
WITH latest AS (
  SELECT DISTINCT ON (work_id) work_id, status, failure_code
  FROM core_workflowrun
  WHERE execution_contract_version = 1
  ORDER BY work_id, created_at DESC, id DESC
)
SELECT count(*) AS stranded
FROM core_workflowwork w
JOIN latest l ON l.work_id = w.id
WHERE w.work_type = 'session_distillation'
  AND w.contract_version = 1
  AND w.disposition = 'required'
  AND l.status = 'failed'
  AND l.failure_code IN (
    'provider_output_malformed','provider_output_truncated','provider_account_unavailable'
  );
-- expect: 0

-- (V2) Distillation works settled (candidates produced or no_signal) after backfill.
SELECT execution_state, disposition, resolution_reason, count(*)
FROM core_workflowwork
WHERE work_type = 'session_distillation' AND contract_version = 1
GROUP BY 1,2,3 ORDER BY 4 DESC;
-- expect: 'terminal_failure' bucket drained; 'settled'/'complete' growing.

-- (V3) Candidates created per recovered session bucket.
SELECT count(DISTINCT session_id) AS sessions_with_candidates
FROM core_memorycandidate
WHERE created_at >= '<backfill_start>';

-- (V4) Curation queue depth sane (candidate-decision backlog not exploding).
SELECT execution_state, count(*)
FROM core_workflowwork
WHERE work_type = 'candidate_decision'
GROUP BY 1 ORDER BY 2 DESC;
-- expect: 'ready'/'retry_wait' drains over time; no runaway 'terminal_failure'.

-- (V5) No new malformed on REDUCE after R1 (confirms fix, not just masking).
SELECT failure_code, count(*)
FROM core_workflowrun
WHERE execution_contract_version = 1
  AND run_type = 'session_distillation'
  AND created_at >= '<backfill_start>'
  AND status = 'failed'
GROUP BY 1 ORDER BY 2 DESC;
-- expect: no/near-zero provider_output_malformed; any provider_output_truncated
--         resolves via R1 deterministic split, not a stuck loop.
```

(Table names are illustrative — resolve exact `db_table`s from `core/models.py`
before running; the identifying columns above are the ones this design relies on.)

## Out of Scope

- The R1 fix itself (output-budgeted reduce batching, index-based source refs,
  dedup-not-compression reduce, first-class truncation). R2 assumes it is deployed.
- Raising completion caps.
- Recovering legacy v0 distillation works (handled by `RetryFailedDistillations`).
- Touching the candidate-decision / curation pipeline beyond re-driving the roots
  that feed it.
- Any migration, model, or public-API change.
- Automatic scheduling of the backfill (Celery beat) — this is a one-shot manual op.

## Review Reconciliation

(append-only; initially empty)
