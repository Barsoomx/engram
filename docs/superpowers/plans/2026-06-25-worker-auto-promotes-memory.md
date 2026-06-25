# Worker Auto-Promotes Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the package-relayed observation worker create approved searchable memory so the Compose parity loop no longer needs manual candidate promotion.

**Architecture:** Keep hook ingest as id-only `django-celery-outbox` task enqueueing. Reuse the existing `PromoteMemoryCandidate` service from inside `ProcessObservationRecorded` so candidate creation, approved memory creation, versioning, and retrieval indexing happen idempotently in the worker path.

**Tech Stack:** Django 5.2, DRF, Celery, `django-celery-outbox`, pytest, Docker Compose.

## Global Constraints

- Use `django-celery-outbox` through `process_observation_recorded.delay(str(observation.id))`; do not add a custom outbox model, relay, or polling command.
- Keep queued task payloads id-only and free of API keys, bearer tokens, provider secrets, prompt bodies, and raw tool payloads.
- The manual `engram_promote_memory_candidate` command remains available, but `scripts/e2e_golden_path.py` must not call it.
- Worker-created memory must be idempotent for duplicate task delivery.
- Keep branch and PR operations under the controller; implementation workers may edit only assigned files.
- Run backend verification inside Docker Compose when finalizing this checkpoint.

---

### Task 1: Worker Auto-Promotion

**Files:**
- Modify: `apps/backend/engram/memory/services.py`
- Modify: `apps/backend/engram/memory/tasks.py`
- Modify: `apps/backend/engram/memory/memory_worker_tests.py`

**Interfaces:**
- Produces: `MemoryCandidateWorkerResult(candidate, memory, memory_version, retrieval_document, duplicate)`
- Produces: `process_observation_recorded(observation_id: object) -> str`, returning the approved memory id
- Consumes: `PromoteMemoryCandidate.execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))`

- [ ] **Step 1: Write the failing worker test**

Add a test near `test_observation_recorded_worker_creates_candidate_with_redacted_evidence`:

```python
@pytest.mark.django_db
def test_observation_recorded_worker_auto_promotes_memory_and_indexes_retrieval() -> None:
    _organization, team, project, _session, raw_event, observation = create_observation_recorded_scope()

    result = execute_worker(observation)

    candidate = MemoryCandidate.objects.get()
    memory = Memory.objects.get()
    version = MemoryVersion.objects.get()
    document = RetrievalDocument.objects.get()

    assert result.duplicate is False
    assert result.candidate.id == candidate.id
    assert result.memory.id == memory.id
    assert result.memory_version.id == version.id
    assert result.retrieval_document.id == document.id
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id == memory.id
    assert memory.organization_id == project.organization_id
    assert memory.project_id == project.id
    assert memory.team_id == team.id
    assert memory.status == MemoryStatus.APPROVED
    assert memory.title == candidate.title
    assert memory.body == candidate.body
    assert version.memory_id == memory.id
    assert version.source_observation_id == observation.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.file_paths == observation.files_read + observation.files_modified
    assert RAW_KEY not in f'{candidate.evidence} {memory.title} {memory.body} {document.full_text}'
```

Expected before implementation: fail because `MemoryCandidateWorkerResult` has
no `memory`, `memory_version`, or `retrieval_document`, and the worker does not
promote the candidate.

- [ ] **Step 2: Write the duplicate-delivery regression**

Replace `test_observation_recorded_worker_is_idempotent_for_duplicate_delivery`
with assertions for all durable rows:

```python
@pytest.mark.django_db
def test_observation_recorded_worker_is_idempotent_for_duplicate_delivery() -> None:
    _organization, _team, _project, _session, _raw_event, observation = create_observation_recorded_scope()
    first = execute_worker(observation)

    second = execute_worker(observation)

    assert second.duplicate is True
    assert second.candidate.id == first.candidate.id
    assert second.memory.id == first.memory.id
    assert second.memory_version.id == first.memory_version.id
    assert second.retrieval_document.id == first.retrieval_document.id
    assert MemoryCandidate.objects.count() == 1
    assert Memory.objects.count() == 1
    assert MemoryVersion.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1
```

Expected before implementation: fail because the worker currently does not
create memory/version/retrieval rows.

- [ ] **Step 3: Update task delegation expectation**

Change `test_process_observation_recorded_task_delegates_by_observation_id`:

```python
memory_id = process_observation_recorded.run(str(observation.id))

memory = Memory.objects.get()

assert memory_id == str(memory.id)
assert RetrievalDocument.objects.get().memory_id == memory.id
```

Expected before implementation: fail because the task returns the candidate id.

- [ ] **Step 4: Implement minimal auto-promotion**

Update result dataclass:

```python
@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    duplicate: bool
```

In `ProcessObservationRecorded.execute`, after `_get_or_create_candidate`:

```python
promotion = PromoteMemoryCandidate().execute(
    PromoteMemoryCandidateInput(candidate_id=candidate.id),
)

return MemoryCandidateWorkerResult(
    candidate=promotion.candidate,
    memory=promotion.memory,
    memory_version=promotion.memory_version,
    retrieval_document=promotion.retrieval_document,
    duplicate=not candidate_created or promotion.duplicate,
)
```

Keep `_lock_observation` unchanged with `select_for_update(of=('self',))`.

In `process_observation_recorded`, return:

```python
return str(result.memory.id)
```

- [ ] **Step 5: Adjust promotion command tests**

Where tests need an unpromoted candidate for `engram_promote_memory_candidate`,
create `MemoryCandidate` directly from the existing observation fixture instead
of calling `execute_worker(observation).candidate`. Use the same fields as the
existing `test_observation_recorded_worker_reuses_existing_candidate` setup:
organization, project, team, source observation, title, body,
`CandidateStatus.PROPOSED`, `VisibilityScope.PROJECT`, evidence, content hash,
and confidence.

Keep command tests proving:

- explicit candidate id promotion works;
- `--project-id ID --latest` chooses only a candidate from that project;
- idempotent promotion still returns existing memory/version/document for a
  promoted candidate.

- [ ] **Step 6: Run focused worker tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v
```

Expected: pass.

### Task 2: Compose Golden Path Evidence

**Files:**
- Modify: `scripts/e2e_golden_path.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`

**Interfaces:**
- Consumes: worker-created `RetrievalDocument`
- Produces: E2E proof that future context receives worker-created memory and persisted context audit evidence

- [ ] **Step 1: Write repository test for no manual promotion**

Update `test_golden_path_waits_for_relayed_tasks_instead_of_manual_outbox_processing`:

```python
self.assertNotIn('engram_process_observation_outbox', script)
self.assertNotIn('engram_promote_memory_candidate', script)
self.assertIn('Waiting for worker-created retrieval document', script)
```

Expected before script changes: fail because the golden path still calls
`engram_promote_memory_candidate`.

- [ ] **Step 2: Replace promotion polling with retrieval polling**

In `scripts/e2e_golden_path.py`:

- rename `NO_PROMOTABLE_CANDIDATE_ERROR` to remove it;
- replace `wait_for_promotion(project_id, api_key)` with
  `wait_for_worker_memory(project_id, api_key)`;
- make `wait_for_worker_memory` run a Django shell command inside the `api`
  container that returns JSON when an approved `Memory`, `MemoryVersion`, and
  `RetrievalDocument` exist for the project and `MEMORY_TITLE`;
- keep timeout and polling constants.

The shell command body should query by `project_id`, `title=MEMORY_TITLE`, and
`status='approved'`, then return:

```json
{
  "memory_id": "11111111-1111-4111-8111-111111111111",
  "memory_version_id": "22222222-2222-4222-8222-222222222222",
  "retrieval_document_id": "33333333-3333-4333-8333-333333333333"
}
```

If no row exists, it must exit nonzero with a stable message:

```text
worker-created retrieval document not ready
```

- [ ] **Step 3: Add context audit evidence assertion**

After `assert_context_response(context)`, call a helper that runs Django shell in
the `api` container and verifies:

- a `ContextBundleItem` exists for the returned `context_bundle_id` and
  `retrieval_document_id`;
- a `MemoryRetrieved` `AuditEvent` exists for the same context bundle and
  request id.

Return JSON:

```json
{
  "context_bundle_item_id": "44444444-4444-4444-8444-444444444444",
  "audit_event_id": "55555555-5555-4555-8555-555555555555"
}
```

- [ ] **Step 4: Run repository tests**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract -v
```

Expected: pass.

### Task 3: Evidence And Final Verification

**Files:**
- Modify: `docs/verification-matrix.md`
- Create: `docs/security/reviews/2026-06-25-worker-auto-promotes-memory.md`

**Interfaces:**
- Produces: checkpoint evidence with commands, exit codes, first failures, and residual risks

- [ ] **Step 1: Update verification matrix**

Add a new top section:

```markdown
## 2026-06-25: Worker Auto-Promotes Memory

Branch: `feat/worker-auto-promotes-memory`

Scope:

- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/tasks.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `scripts/e2e_golden_path.py`
- `tests/repository/test_backend_runtime_contract.py`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| focused RED worker tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py::test_observation_recorded_worker_auto_promotes_memory_and_indexes_retrieval engram/memory/memory_worker_tests.py::test_observation_recorded_worker_is_idempotent_for_duplicate_delivery engram/memory/memory_worker_tests.py::test_process_observation_recorded_task_delegates_by_observation_id -v` | Backend | yes | fixed | Record first failing assertion and final pass count. |
| focused worker tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | pass | Record final pass count. |
| repository runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | pass | Proves golden path no longer calls manual promotion. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Proves relayed worker creates retrieval state and context audit evidence. |
| full backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Record final pass count. |
| backend lint and format | `cd apps/backend && poetry run ruff check . && poetry run ruff format --check .` | Backend | yes | pass | Record exact output. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --skip-checks --settings=settings.test_settings` | Backend | yes | pass | Must report `No changes detected`. |
| repository checks | `python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py`; `git diff --check` | Repository Quality | yes | pass | Record final pass count and exit codes. |
```

Record focused worker tests, repository tests, full backend tests, lint/format,
migration freshness, Compose golden path, security review, and first decisive
failures.

- [ ] **Step 2: Write focused security review**

Create `docs/security/reviews/2026-06-25-worker-auto-promotes-memory.md` with:

- scope reviewed;
- commands/tools run;
- findings by severity;
- fixes applied or accepted risks;
- regression tests for fixed issues.

Security review must cover task payload secrecy, candidate/memory/retrieval
redaction, tenant/project scoping by loaded observation, duplicate delivery
idempotency, and context audit evidence.

- [ ] **Step 3: Run full local verification**

Run:

```bash
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --skip-checks --settings=settings.test_settings
python3 scripts/e2e_golden_path.py
git diff --check
```

Expected: all pass.

- [ ] **Step 4: Final read-only review**

Generate a review package for `origin/master..HEAD` and dispatch a read-only
review agent. Fix Critical/Important findings before creating the checkpoint PR.
