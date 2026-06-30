# Design: Weekly structured digest scheduling

> Branch `feat/weekly-digest-schedule`, off current master. Roadmap Слой 3: the weekly
> structured digest is implemented + tested but has no automatic trigger. Tests on pg+pgvector.

## Problem
`BuildWeeklyStructuredDigest` + `run_weekly_digest_with_tracking` (`memory/services.py:1193,1386`)
are complete and unit-tested (`weekly_structured_digest_tests.py`), but `celeryconfig.py`
`beat_schedule` only has `'daily-digest'` — the weekly digest never runs automatically. It is
dormant. (Mirror of how daily is wired.)

## Target
Schedule the weekly digest exactly like the daily one: a Celery beat entry → a fan-out task
that enqueues a per-project digest task → `run_weekly_digest_with_tracking`. No model change.

## Existing daily wiring (the template)
- `memory/tasks.py:80` `generate_daily_digest(self, organization_id, project_id, memory_ids)` →
  `run_daily_digest_with_tracking(...)`.
- `memory/tasks.py:119` `run_scheduled_digests()` → for each project with recent approved
  memories, `generate_daily_digest.delay(org_id, project_id, [memory_ids])`.
- `celeryconfig.py:107` `beat_schedule = {'daily-digest': {'task': 'engram.memory.run_scheduled_digests', 'schedule': crontab(hour=2, minute=0)}}`.
- Note: `run_weekly_digest_with_tracking(organization_id, project_id, *, window_days=7,
  request_id, correlation_id)` takes **no** `memory_ids` (it computes the 7-day window itself).

## Design

### `memory/tasks.py`
1. Import `run_weekly_digest_with_tracking` (alongside the existing `run_daily_digest_with_tracking`).
2. Add task `generate_weekly_digest(self, organization_id, project_id)` (mirror
   `generate_daily_digest`, but no `memory_ids`): parse the two UUIDs (raise
   `MemoryWorkerError('malformed weekly digest input')` on bad input), bind structlog
   contextvars with `request_id = f'weekly-digest:{project_id}'`, call
   `run_weekly_digest_with_tracking(organization_id=..., project_id=..., request_id=...,
   correlation_id=...)` with the same retryable-error handling (`exc.retryable` → `self.retry`),
   `finally` clear contextvars, return `str(result.memory.id)`. Task name
   `'engram.memory.generate_weekly_digest'`, `bind=True, max_retries=_MAX_RETRIES`.
3. Add fan-out task `run_scheduled_weekly_digests()` (mirror `run_scheduled_digests`, name
   `'engram.memory.run_scheduled_weekly_digests'`): for each `Project` that has at least one
   approved memory, `generate_weekly_digest.delay(str(project.organization_id), str(project.id))`;
   return `{'enqueued_projects': n, 'enqueued_tasks': n}`. Skip projects with no approved
   memories (mirror daily's skip-empty behaviour; reuse the existing approved-memory check or a
   simple `.exists()` — pick the smallest correct predicate and keep it consistent with daily).

### `celeryconfig.py`
4. Add to `beat_schedule`:
   ```python
   'weekly-digest': {
       'task': 'engram.memory.run_scheduled_weekly_digests',
       'schedule': crontab(day_of_week=1, hour=3, minute=0),
   },
   ```
   (Monday 03:00 — offset from the daily 02:00 so they don't collide.)

## TDD / tests (`memory/` — real tests, NOT celery-eager-skipped)
Put in a new `weekly_digest_schedule_tests.py` (or extend `weekly_structured_digest_tests.py`):
1. `test_weekly_beat_schedule_is_registered`: import `beat_schedule` from `engram.celeryconfig`;
   assert `beat_schedule['weekly-digest']['task'] == 'engram.memory.run_scheduled_weekly_digests'`
   and its `'schedule'` is a `crontab` (pure config — runs without celery).
2. `test_run_scheduled_weekly_digests_enqueues_per_project_with_memories`: create 2 projects (one
   with an approved memory, one empty) via factories; `mock.patch.object(generate_weekly_digest,
   'delay')`; call `run_scheduled_weekly_digests()`; assert `delay` called once, with the
   memory-bearing project's `(org_id, project_id)`. (Mocking `.delay` is synchronous — no eager
   needed; do NOT `@pytest.mark.skip` this. If it genuinely cannot run without eager, mirror the
   daily skip AND keep test #1 + #3 as the real coverage.)
3. `test_generate_weekly_digest_builds_digest_and_returns_memory_id`: seed a project with memory
   activity; call the task body directly (`generate_weekly_digest.run(org_id, project_id)` or call
   the underlying function) ; assert it returns a memory id and a `WorkflowRun` of type
   `WEEKLY_DIGEST` with status SUCCEEDED exists. (Delegates to the already-tested service — thin
   assertion; reuse fixtures from `weekly_structured_digest_tests.py`.)

## Out of scope
Per-org schedule customization, configurable cron via settings, digest delivery/notification.

## Conventions / gate
single quotes; absolute imports; pytest function tests; factories for models; blank line after
return/raise; no `Co-Authored-By`. Full gate (`engram-prod` + `engram-pg`):
`ruff check .`, `ruff format --check .`, `migrate`, `makemigrations --check --dry-run` → **No
changes detected** (no model change), `pytest -q` green (current baseline 723 passed, 6 skipped).
