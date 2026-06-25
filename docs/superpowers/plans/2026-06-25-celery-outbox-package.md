# Celery Outbox Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Engram's custom outbox model/command and use the existing `django-celery-outbox` package through Celery task `.delay(...)`.

**Architecture:** Hook ingest persists domain rows and calls `process_observation_recorded.delay(str(observation.id))` inside the transaction. `django-celery-outbox` owns the transport queue in `CeleryOutbox`; the worker processes observations directly by id.

**Tech Stack:** Django 5.2, DRF, Celery, `django-celery-outbox`, pytest, Docker Compose.

## Global Constraints

- Use the existing `django-celery-outbox` dependency through Celery task `.delay(...)`.
- Do not add a new outbox framework, relay, model, or local polling command.
- Remove the live custom `OutboxEvent` and `OutboxStatus` model API.
- Keep queued task payloads id-only and free of API keys, bearer tokens, provider secrets, prompt bodies, and raw tool payloads.
- Keep branch and PR operations under the controller; implementation workers may edit their assigned files only.
- Run backend verification inside Docker Compose once Docker is available.

---

### Task 1: Backend Refactor

**Files:**
- Modify: `apps/backend/engram/core/models.py`
- Create: `apps/backend/engram/core/migrations/0003_delete_outboxevent.py`
- Modify: `apps/backend/engram/hooks/services.py`
- Modify: `apps/backend/engram/hooks/views.py`
- Modify: `apps/backend/engram/hooks/hook_ingest_tests.py`
- Modify: `apps/backend/engram/memory/services.py`
- Modify: `apps/backend/engram/memory/tasks.py`
- Modify: `apps/backend/engram/memory/memory_worker_tests.py`
- Delete: `apps/backend/engram/memory/management/commands/engram_process_observation_outbox.py`

**Interfaces:**
- Produces: Celery task `process_observation_recorded(observation_id: str) -> str`
- Produces: service input `MemoryCandidateWorkerInput(observation_id: uuid.UUID, worker_id: str = 'memory-worker')`
- Consumes: `django_celery_outbox.models.CeleryOutbox` only in tests to assert package transport rows

- [ ] **Step 1: Write/update failing tests**

Update hook ingest tests so accepted hook responses do not include
`outbox_event_id` and package transport rows contain the accepted
`observation_id`:

```python
queued = CeleryOutbox.objects.get()
assert queued.task_name == 'engram.memory.process_observation_recorded'
assert queued.args == [body['observation_id']]
assert queued.kwargs == {}
```

Update memory worker tests to build observations directly and call:

```python
ProcessObservationRecorded().execute(
    MemoryCandidateWorkerInput(observation_id=observation.id, worker_id='test-worker'),
)
```

Expected before implementation: tests fail because production code still
expects `OutboxEvent` ids and response payloads still include `outbox_event_id`.

- [ ] **Step 2: Replace hook enqueue flow**

In `IngestHookEvent.execute`, after `ObservationSource.objects.get_or_create`,
call:

```python
process_observation_recorded.delay(str(observation.id))
```

Remove `_get_or_create_outbox`, `outbox_event` from `HookIngestResult`, and
`outbox_event_id` from `HookIngestView`.

For duplicate hook requests, return the existing raw event and observation
without enqueueing a second CeleryOutbox row.

- [ ] **Step 3: Refactor worker to load observations directly**

Replace outbox-oriented worker code with:

```python
@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    observation_id: uuid.UUID
    worker_id: str = 'memory-worker'
```

`ProcessObservationRecorded.execute` must lock:

```python
Observation.objects.select_for_update().select_related(
    'organization',
    'project',
    'team',
    'raw_event',
).get(id=data.observation_id)
```

It must create or reuse `MemoryCandidate` by
`memory_candidate_content_hash(observation)`. Evidence must not include
`source_outbox_id`.

- [ ] **Step 4: Remove custom model API**

Remove `OutboxStatus` and `OutboxEvent` from `apps/backend/engram/core/models.py`.
Create migration:

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_remove_outboxevent_core_outbox_unique_idempotency_key_per_event_and_more'),
    ]

    operations = [
        migrations.DeleteModel(name='OutboxEvent'),
    ]
```

- [ ] **Step 5: Delete manual command**

Delete `apps/backend/engram/memory/management/commands/engram_process_observation_outbox.py`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm api pytest engram/hooks/hook_ingest_tests.py engram/memory/memory_worker_tests.py -v
```

Expected after implementation: pass.

- [ ] **Step 7: Commit**

Commit message:

```bash
fix: use celery outbox package for memory worker
```

### Task 2: Repository Contracts And Evidence

**Files:**
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `tests/repository/test_repository_layout.py`
- Modify: `docs/verification-matrix.md`
- Modify: `docs/security/reviews/2026-06-25-first-parity-gate-rollup.md`
- Modify: `docs/superpowers/specs/2026-06-25-hook-event-coverage-design.md`
- Modify: `docs/superpowers/plans/2026-06-25-hook-event-coverage.md`
- Modify: `docs/superpowers/specs/2026-06-25-memory-worker-design.md`
- Modify: `docs/superpowers/plans/2026-06-25-memory-worker.md`

**Interfaces:**
- Consumes: Task 1 deleted manual command and custom model
- Produces: repository checks that verify package transport behavior without requiring brittle custom outbox files

- [ ] **Step 1: Remove deleted command/layout assumptions**

Remove the deleted manual command from required paths and tests. Keep checks
that Compose has `relay` running `python manage.py celery_outbox_relay` because
that relay belongs to the package.

- [ ] **Step 2: Replace custom-outbox wording**

Update live evidence/docs to say Engram uses `django-celery-outbox` package
transport and memory worker tasks are queued by observation id. Do not rewrite
all historical specs; update only files that currently describe live behavior
or acceptance evidence.

- [ ] **Step 3: Run repository checks**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
git diff --check
```

Expected after implementation: pass.

- [ ] **Step 4: Commit**

Commit message:

```bash
test: refresh celery outbox package evidence
```

### Task 3: Final Verification

**Files:**
- Modify only documentation if evidence must be refreshed.

- [ ] **Step 1: Run backend verification in Docker**

Run:

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm api pytest -v
docker compose -f deploy/compose/docker-compose.yml run --rm api ruff check .
docker compose -f deploy/compose/docker-compose.yml run --rm api ruff format --check .
docker compose -f deploy/compose/docker-compose.yml run --rm api python manage.py makemigrations --check --dry-run --settings=settings.test_settings
```

Expected after implementation: all pass.

- [ ] **Step 2: Run Compose golden path**

Run:

```bash
python3 scripts/e2e_golden_path.py
```

Expected after implementation: pass and shut down Compose services.

- [ ] **Step 3: Final review**

Dispatch a review agent with the branch diff. Fix Critical/Important findings
before PR readiness.
