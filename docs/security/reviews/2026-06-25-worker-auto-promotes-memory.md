# Worker Auto-Promotes Memory Security Review

Date: 2026-06-25

Branch: `feat/worker-auto-promotes-memory`

Reviewed head: `da06e17f10a75acd483182a3ad36934a9e67b01e`

Result: SECURITY APPROVED for the worker auto-promotion checkpoint.

## Scope Reviewed

- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/tasks.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `scripts/e2e_golden_path.py`
- `tests/repository/test_backend_runtime_contract.py`
- Worker task payload secrecy.
- Candidate, memory, and retrieval document redaction.
- Tenant/project/team scoping for worker-created memory.
- Duplicate worker delivery and manual command idempotency.
- Context bundle and retrieval audit evidence.

The focused review covered the security risks introduced by automatically
promoting useful worker-observed memory instead of stopping at proposed
candidates. It did not cover unrelated memory-quality workflows, frontend
review UI, MCP tooling, provider/model-policy calls, deployment changes, or
future semantic retrieval work.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| Task 1 RED worker tests | Exit 1. `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` reported 4 failed / 14 passed before worker auto-promotion. Representative failures: status expected promoted vs proposed, `Memory.DoesNotExist` for auto-promotion, missing `memory` on result, and task returning candidate path. |
| Task 1 redaction and duplicate RED | Exit 1. `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py::test_observation_recorded_worker_redacts_candidate_content_and_evidence engram/memory/memory_worker_tests.py::test_promote_memory_candidate_command_is_idempotent_for_duplicate_candidate -v` reported 1 failed / 1 passed. Expected failure was raw `egk_test_memory_worker_...` leaked via persisted memory/retrieval file paths. Duplicate command regression already passed before production change because the command delegated to the idempotent promotion service. |
| focused worker tests | Exit 0. `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` reported 19 passed. |
| focused worker lint | Exit 0. `cd apps/backend && poetry run ruff check engram/memory/services.py engram/memory/tasks.py engram/memory/memory_worker_tests.py` reported `All checks passed!` |
| Compose focused worker tests | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v"` reported 19 passed. |
| repository runtime contract | Exit 0. `python3 -m unittest tests.repository.test_backend_runtime_contract -v` reported 11 tests passed. |
| Python syntax gate | Exit 0. `python3 -m py_compile scripts/e2e_golden_path.py`. |
| Task 2 E2E | Exit 0. `python3 scripts/e2e_golden_path.py` output included Starting Compose services, Submitting hook observation, Waiting for worker-created retrieval document, Requesting future session context, Compose golden path passed, and Stopping Compose services. |
| full Compose backend, lint, and format | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v && ruff check . && ruff format --check ."` reported pytest 133 passed, ruff `All checks passed!`, and format `68 files already formatted`. |
| Compose migration freshness | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` applied migrations through `django_celery_outbox.0006` and `sessions.0001`; `No changes detected`. |
| repository checks | Exit 0 for `python3 -m unittest discover -s tests -v` with 27 tests passed; `python3 scripts/repository_layout.py` with no output; `python3 scripts/repository_quality.py` with no output; `git diff --check HEAD` with no output. |
| final Compose golden path rerun | Exit 0. `python3 scripts/e2e_golden_path.py` completed through the same worker-created retrieval-document wait path. |
| Compose cleanup | Exit 0. `docker compose -f deploy/compose/docker-compose.yml ps --format json` returned no output. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

None open.

Resolved during the slice: token-shaped values leaked through persisted
memory/retrieval file paths during the redaction RED. Candidate title/body and
evidence were already in scope for redaction; the fix extended protection to
memory metadata file paths and `RetrievalDocument` file paths.

### MINOR

None.

## Security Checks

- Task payload secrecy: hook enqueue remains package
  `.delay(str(observation.id))`. Queued task payloads are id-only and do not
  carry API keys, bearer tokens, provider secrets, prompt bodies, or raw tool
  payloads.
- Candidate, memory, and retrieval redaction: candidate title/body/evidence
  are redacted. Memory metadata file paths and `RetrievalDocument` file paths
  are redacted when token-shaped values appear. Regression tests cover the
  leak.
- Tenant/project scoping: the worker loads `Observation` by id and promotes
  using that observation's organization, project, and team. The E2E retrieval
  query filters by project id and title.
- Duplicate delivery idempotency: duplicate worker delivery reuses the same
  candidate, memory, version, and retrieval document. The manual command
  duplicate regression proves the second command returns `duplicate true` with
  stable ids.
- Context audit evidence: E2E verifies `ContextBundleItem` and
  `MemoryRetrieved` `AuditEvent` records for the returned context bundle,
  request, and retrieval document.

## Fixes Applied

- Worker processing now promotes worker-created useful memory instead of
  leaving it as a proposed candidate.
- Worker result evidence includes the promoted memory path needed by callers.
- Token-shaped values are redacted from memory metadata file paths and
  retrieval document file paths.
- The E2E golden path waits for the worker-created retrieval document and then
  verifies future-session context and audit evidence.
- Repository runtime contract proves the golden path no longer uses manual
  promotion.

## Regression Tests Added

- Worker auto-promotion creates memory and retrieval state from an observed
  hook event.
- Worker duplicate delivery is idempotent across candidate, memory, version,
  and retrieval document ids.
- Task delegation uses only the observation id.
- Redaction regression covers token-shaped values in persisted memory and
  retrieval evidence paths.
- Manual command duplicate regression proves the second command reports
  `duplicate true` with stable ids.
- Compose golden path proves worker-created retrieval state reaches future
  session context and audit records.

## Accepted Risk

No accepted security risk remains for this focused checkpoint.

Task 2 review approved the golden path. Task 1 worker scope was clean after
valid findings were fixed, and its remaining blocker was Task 2 golden path,
now resolved.
