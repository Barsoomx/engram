# Memory Candidate Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first server-side worker path that consumes
`ObservationRecorded` outbox events and creates idempotent proposed
`MemoryCandidate` rows.

**Architecture:** Create one `engram.memory` Django app with a domain service
for deterministic observation-to-candidate processing and a Celery task wrapper
that accepts only an outbox id. The service locks the source outbox row, reloads
authoritative database state, writes a candidate plus `MemoryCandidateCreated`
outbox event idempotently, and marks the source event done or failed.

**Tech Stack:** Django 5.2, Celery 5.5, PostgreSQL target with sqlite test
database, Poetry, pytest-django, Ruff.

## Global Constraints

- Work on branch `feat/parity-07-memory-worker`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use pytest function tests named `*_tests.py`.
- Use TDD: write failing tests before production code.
- Do not add provider calls, provider secrets, model policy resolution,
  embeddings, retrieval documents, context bundle APIs, CLI behavior, frontend
  files, MCP tools, memory promotion, or semantic search.
- Celery tasks accept ids only and reload authoritative state from PostgreSQL.
- Candidate evidence and outbox payloads must not contain raw API keys, bearer
  tokens, hook payloads, or unredacted tool output.
- Docker Compose live checks are recorded as blocked while Docker is unavailable
  in this WSL distro.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-memory-worker-design.md`
- Create: `docs/superpowers/plans/2026-06-25-memory-worker.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/north-star.md`, `docs/architecture.md`,
  `docs/backend-contracts.md`, `docs/agent-integrations.md`,
  `docs/parity/claude-mem-parity-map.md`, and the current
  `engram.core`/`engram.hooks` code.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write the design and plan**

Document worker scope, deterministic candidate generation, source outbox status
transitions, downstream event emission, explicit deferrals, tests, and
verification.

- [ ] **Step 2: Run docs sanity checks**

Run:

```bash
python3 scripts/repository_quality.py
git diff --check HEAD
```

Expected: both commands exit 0.

- [ ] **Step 3: Commit**

Commit:

```bash
git add docs/superpowers/specs/2026-06-25-memory-worker-design.md docs/superpowers/plans/2026-06-25-memory-worker.md
git commit -m "chore: add memory worker plan"
```

### Task 2: Failing Worker Contract Tests

**Files:**

- Create: `apps/backend/engram/memory/memory_worker_tests.py`

**Interfaces:**

- Consumes: existing `engram.core` models and `ObservationRecorded` outbox
  events created by the hook slice.
- Produces: failing service/task tests for candidate creation, idempotency,
  failure marking, and Celery task delegation.

- [ ] **Step 1: Add test helpers**

Create helpers in `memory_worker_tests.py` that:

- create organization, team, project, agent, session, raw event, observation,
  and source outbox rows directly through models;
- create an `ObservationRecorded` outbox row with payload ids only:

```python
{
    'raw_event_id': str(raw_event.id),
    'observation_id': str(observation.id),
    'agent_session_id': str(session.id),
    'event_type': raw_event.event_type,
}
```

- use a local `RAW_KEY` constant only as a sentinel to prove it is not
  persisted in worker evidence.

- [ ] **Step 2: Add candidate creation test**

Add a test named:

```python
def test_observation_recorded_worker_creates_candidate_and_downstream_outbox() -> None:
```

Assert:

- `ProcessObservationRecorded().execute(...)` returns `duplicate is False`;
- one `MemoryCandidate` exists with `status == CandidateStatus.PROPOSED`;
- candidate scope, source observation, title, body, visibility, confidence, and
  content hash are set;
- candidate evidence contains observation/raw-event/outbox ids;
- source outbox status is `OutboxStatus.DONE`, attempts is `1`, and
  `processed_at` is set;
- one `MemoryCandidateCreated` outbox row exists with id-only payload;
- `RAW_KEY` is not present in candidate evidence or downstream outbox payload.

- [ ] **Step 3: Add duplicate delivery test**

Add a test named:

```python
def test_observation_recorded_worker_is_idempotent_for_duplicate_delivery() -> None:
```

Call `execute()` twice for the same outbox id. Assert the second result has
`duplicate is True` and row counts remain:

- one `MemoryCandidate`;
- two outbox rows total: the original `ObservationRecorded` and the downstream
  `MemoryCandidateCreated`.

- [ ] **Step 4: Add pending-source reuse test**

Add a test named:

```python
def test_observation_recorded_worker_reuses_existing_candidate_before_marking_done() -> None:
```

Create the expected candidate and downstream outbox row before calling the
service while the source outbox remains pending. Assert the service reuses both
rows and marks the source outbox done.

- [ ] **Step 5: Add failure tests**

Add tests that prove:

- an outbox row with `event_type='OtherEvent'` is marked `failed`, records a
  redacted `last_error`, and creates no candidate;
- an `ObservationRecorded` row missing `payload.observation_id` is marked
  `failed`, records a redacted `last_error`, and creates no candidate.

- [ ] **Step 6: Add Celery task wrapper test**

Add a test named:

```python
def test_process_observation_recorded_outbox_task_delegates_by_outbox_id() -> None:
```

Call `process_observation_recorded_outbox.run(str(outbox.id))` and assert the
candidate is created and the source event is done.

- [ ] **Step 7: Run focused tests and verify first failure**

Run:

```bash
cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v
```

Expected before implementation: collection fails with missing
`engram.memory` module or service import.

### Task 3: Memory Worker Service And Task

**Files:**

- Create: `apps/backend/engram/memory/__init__.py`
- Create: `apps/backend/engram/memory/apps.py`
- Create: `apps/backend/engram/memory/services.py`
- Create: `apps/backend/engram/memory/tasks.py`
- Modify: `apps/backend/settings/settings.py`

**Interfaces:**

- Consumes: tests from Task 2 and existing `MemoryCandidate`, `Observation`,
  and `OutboxEvent` models.
- Produces:
  - `MemoryCandidateWorkerInput(outbox_event_id: uuid.UUID, worker_id: str)`
  - `MemoryCandidateWorkerResult`
  - `ProcessObservationRecorded.execute(data: MemoryCandidateWorkerInput)`
  - Celery task `process_observation_recorded_outbox(outbox_event_id: str)`.

- [ ] **Step 1: Add app registration**

Add `MemoryConfig`, install `engram.memory` in `INSTALLED_APPS`, and keep Celery
autodiscovery using the existing `engram.celery_app`.

- [ ] **Step 2: Implement DTOs and domain error**

In `services.py`, define:

```python
@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    outbox_event_id: uuid.UUID
    worker_id: str = 'memory-worker'


@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    source_outbox: OutboxEvent
    candidate: MemoryCandidate
    downstream_outbox: OutboxEvent
    duplicate: bool


class MemoryWorkerError(Exception):
    pass
```

- [ ] **Step 3: Implement stable candidate hash**

Add:

```python
def memory_candidate_content_hash(observation: Observation) -> str:
    source = f'{observation.id}:{observation.content_hash}'

    return hashlib.sha256(source.encode()).hexdigest()
```

- [ ] **Step 4: Implement candidate body/title/evidence helpers**

Create helpers that:

- use `observation.title[:255]` for title;
- use `observation.body.strip()` when present, otherwise `observation.title`;
- include evidence keys `observation_id`, `raw_event_id`, `source_outbox_id`,
  `event_type`, `title`, `files_read`, and `files_modified`;
- never include raw event payload or headers.

- [ ] **Step 5: Implement `execute()` success path**

Inside `transaction.atomic()`:

- lock the source outbox with `select_for_update()`;
- validate event type;
- if already done, return existing rows with `duplicate=True`;
- set status to `processing`, increment attempts, set `locked_by` and
  `locked_at`;
- reload the observation in the source outbox scope;
- get or create `MemoryCandidate`;
- get or create `MemoryCandidateCreated` outbox;
- mark source outbox `done`, clear `last_error`, set `processed_at`;
- return result.

- [ ] **Step 6: Implement failure marking**

Wrap the success path so `MemoryWorkerError`, missing source rows, malformed
payloads, and missing scoped observations mark the source outbox `failed` with:

- attempts incremented if the row was still pending;
- `locked_by` and `locked_at`;
- `last_error` containing the domain error class and message, truncated to 1000
  characters;
- `next_retry_at = timezone.now() + timedelta(minutes=1)`.

Re-raise the domain error after marking failure.

- [ ] **Step 7: Implement Celery task wrapper**

In `tasks.py`:

```python
from __future__ import annotations

import uuid

from celery import shared_task

from engram.memory.services import MemoryCandidateWorkerInput, ProcessObservationRecorded


@shared_task(name='engram.memory.process_observation_recorded_outbox')
def process_observation_recorded_outbox(outbox_event_id: str) -> str:
    result = ProcessObservationRecorded().execute(
        MemoryCandidateWorkerInput(outbox_event_id=uuid.UUID(outbox_event_id)),
    )

    return str(result.candidate.id)
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v
```

Expected: all focused memory worker tests pass.

### Task 4: Repository Gates And Verification Matrix

**Files:**

- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: memory app files and passing focused tests.
- Produces: repository-level gates requiring memory worker files and recorded
  command evidence.

- [ ] **Step 1: Add repository layout requirements**

Require:

- `apps/backend/engram/memory/apps.py`;
- `apps/backend/engram/memory/services.py`;
- `apps/backend/engram/memory/tasks.py`;
- `apps/backend/engram/memory/memory_worker_tests.py`.

- [ ] **Step 2: Run repository contract test**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract -v
```

Expected: pass.

- [ ] **Step 3: Update verification matrix**

Add the `2026-06-25: Memory Candidate Worker` checkpoint with branch, scope,
commands, exit codes, CI status, review findings, and first decisive TDD
failures.

### Task 5: Review And Final Verification

**Files:** no new owned files unless review findings require fixes.

**Interfaces:**

- Consumes: completed memory worker implementation.
- Produces: fixed/refuted review findings and a coherent checkpoint commit.

- [ ] **Step 1: Run focused security and simplicity review**

Check:

- task payloads contain ids only;
- candidate evidence does not include raw hook payloads, raw API keys, bearer
  tokens, or unredacted tool output;
- source outbox status transitions are idempotent;
- duplicate delivery cannot create duplicate candidates or downstream outbox
  rows;
- malformed rows fail closed and remain retryable;
- no provider/retrieval/context scope was added.

- [ ] **Step 2: Run full verification**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
cd apps/backend && poetry check
git diff --check HEAD
docker compose version
```

Expected: all commands exit 0 except Docker Compose availability if Docker is
still unavailable in this WSL distro.

- [ ] **Step 3: Commit implementation checkpoint**

Commit:

```bash
git add apps/backend/settings/settings.py apps/backend/engram/memory scripts/repository_layout.py tests/repository/test_backend_runtime_contract.py docs/verification-matrix.md
git commit -m "feat: add memory candidate worker"
```
