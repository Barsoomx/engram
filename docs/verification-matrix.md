# Verification Matrix

This matrix records local commands, CI equivalents, status, and first decisive
failures for each completed Engram slice.

Current outbox contract (2026-06-25): Engram uses
`django-celery-outbox package transport`. Hook ingest queues
`engram.memory.process_observation_recorded` with the observation id through
the Celery task `.delay(...)` call. The Compose `relay` service runs the
package-owned `python manage.py celery_outbox_relay`; it is not an Engram
domain outbox processor.

## 2026-06-25: Worker Auto-Promotes Memory

Branch: `feat/worker-auto-promotes-memory`

Code commits in slice:

- `2cb084cf feat: auto promote observed memory`
- `c8767724 fix: redact auto-promoted memory evidence`
- `da06e17f test: prove worker-created memory in e2e`
- `60e83a02 fix: bind e2e to current worker observation`

Final review blocker correction after reviewed head
`38b5e5b591bca1aa9769db329f7512b5beffcf54`:

- The correction fixes the stale Compose DB acceptance gap in
  `scripts/e2e_golden_path.py`.
- Root cause: the previous golden path could pass against stale Compose
  volumes because it used deterministic hook/context ids, looked up approved
  memory only by project/title, and did not prove the `RetrievalDocument` came
  from the current hook observation.
- Fix: the golden path now clears Compose volumes before startup, generates a
  per-run id, uses that id in hook and context identities, queries by
  project/per-run memory title, verifies the source raw event's
  `client_event_id` and `request_id`, verifies
  `RetrievalDocument.source_observation_ids`, and asserts context against the
  per-run title/body.
- Residual risks after this correction: none for this focused checkpoint.

Scope:

- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/tasks.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `scripts/e2e_golden_path.py`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/verification-matrix.md`
- `docs/security/reviews/2026-06-25-worker-auto-promotes-memory.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| focused RED worker tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | fixed | Exit 1 before worker auto-promotion. Reported 4 failed / 14 passed. Representative failures: status expected promoted vs proposed, `Memory.DoesNotExist` for auto-promotion, missing `memory` on result, and task returning candidate path. |
| focused redaction and duplicate RED | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py::test_observation_recorded_worker_redacts_candidate_content_and_evidence engram/memory/memory_worker_tests.py::test_promote_memory_candidate_command_is_idempotent_for_duplicate_candidate -v` | Backend | yes | fixed | Exit 1 with 1 failed / 1 passed. Expected failure was raw `egk_test_memory_worker_...` leaked via persisted memory/retrieval file paths. Duplicate command regression already passed before production change because the command delegated to the idempotent promotion service. |
| focused worker tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | pass | Exit 0. Reported 19 passed. |
| focused worker lint | `cd apps/backend && poetry run ruff check engram/memory/services.py engram/memory/tasks.py engram/memory/memory_worker_tests.py` | Backend | yes | pass | Exit 0. `All checks passed!` |
| Compose focused worker tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_worker_tests.py -v"` | Backend / Compose E2E | yes | pass | Exit 0. Reported 19 passed. |
| repository runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | pass | Exit 0. Reported 11 tests passed and proves the golden path no longer calls manual promotion. |
| Python syntax gate | `python3 -m py_compile scripts/e2e_golden_path.py` | Repository Quality | yes | pass | Exit 0. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Output included Starting Compose services, Submitting hook observation, Waiting for worker-created retrieval document, Requesting future session context, Compose golden path passed, and Stopping Compose services. |
| full Compose backend, lint, and format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v && ruff check . && ruff format --check ."` | Backend / Compose E2E | yes | pass | Exit 0. Pytest reported 133 passed; ruff reported `All checks passed!`; format reported `68 files already formatted`. |
| Compose migration freshness | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend / Compose E2E | yes | pass | Exit 0. Migrations applied through `django_celery_outbox.0006` and `sessions.0001`; `No changes detected`. |
| repository checks | `python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py`; `git diff --check HEAD` | Repository Quality | yes | pass | Unit discovery exit 0 with 27 tests passed; layout exit 0 with no output; quality exit 0 with no output; whitespace exit 0 with no output. |
| final Compose golden path rerun | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0 through the same worker-created retrieval-document wait path. |
| Compose cleanup | `docker compose -f deploy/compose/docker-compose.yml ps --format json` | none | yes | pass | Exit 0 with no output. |
| focused security review | manual review recorded in `docs/security/reviews/2026-06-25-worker-auto-promotes-memory.md` | Security Review | yes | pass | Covers task payload secrecy, candidate/memory/retrieval redaction, tenant/project scoping, duplicate delivery idempotency, and context audit evidence. No open Critical or Important findings. |
| task reviews | read-only Task 1 and Task 2 reviews | Review | yes | pass | Task 2 review approved. Task 1 worker scope was clean after valid findings were fixed, and its remaining blocker was Task 2 golden path, now resolved. |
| final review stale-state RED | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | fixed | Exit 1 after adding the freshness contract. Expected failure: `run_id = secrets.token_hex(8)` was missing from the golden path script. |
| final review generated-query RED | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | fixed | Exit 1 after tightening the contract for generated shell constants. Expected failure: `client_event_id = {json.dumps(client_event_id)}` was missing from the generated worker-memory query. |
| final review syntax gate | `python3 -m py_compile scripts/e2e_golden_path.py` | Repository Quality | yes | pass | Exit 0 with no output after the stale-state correction. |
| final review repository contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | pass | Exit 0. Reported 12 tests passed, including the new freshness guard contract. |
| final review Compose golden path first rerun | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | fixed | Exit 1. First decisive failure was a generated Django shell `NameError: name 'client_event_id' is not defined`; the query compared against constants that had not been emitted into the shell snippet. |
| final review Compose golden path source-bound rerun | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Output included Clearing Compose state, Waiting for worker-created retrieval document, Requesting future session context, Compose golden path passed, and Stopping Compose services. |
| final review repository quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no output after docs update. |
| final review whitespace | `git diff --check` | Repository Quality | yes | pass | Exit 0 with no output after docs update. |
| final review final Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0 after docs update. Output included Clearing Compose state, Waiting for worker-created retrieval document, Requesting future session context, Compose golden path passed, and Stopping Compose services. |

Task 3 verification-command contract:

| Brief command | Recorded result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | Run directly. Exit 0. Reported 27 tests passed. |
| `cd apps/backend && poetry run pytest -v` | Not run directly in final verification because local `AGENTS.md` requires backend verification inside Docker Compose once Compose exists. Superseded by `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v && ruff check . && ruff format --check ."`: exit 0, pytest 133 passed. |
| `cd apps/backend && poetry run ruff check .` | Not run directly in final verification for the same Compose policy. Superseded by the full Compose backend/lint/format command: exit 0, `All checks passed!`. |
| `cd apps/backend && poetry run ruff format --check .` | Not run directly in final verification for the same Compose policy. Superseded by the full Compose backend/lint/format command: exit 0, `68 files already formatted`. |
| `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --skip-checks --settings=settings.test_settings` | Not run directly in final verification for the same Compose policy. Superseded by `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"`: exit 0, `No changes detected`. |
| `python3 scripts/e2e_golden_path.py` | Run directly. Exit 0. Final rerun completed through the worker-created retrieval-document wait path. |
| `git diff --check` | Rerun for the Task 3 docs/evidence correction. Exit 0. The original main-agent final whitespace command was `git diff --check HEAD`, exit 0. |

Residual risks: none for this focused checkpoint.

Final review blocker fixed: stale Compose state can no longer satisfy the
acceptance gate because the E2E clears volumes before startup and requires the
current run's hook raw event and source observation to match the retrieval
document used for context.

First decisive failures fixed during the TDD/debug loop:

- Worker auto-promotion RED failed before implementation: status stayed
  proposed instead of promoted, worker-created memory was missing, the result
  did not include `memory`, and the task returned a candidate path.
- Redaction RED failed because a raw `egk_test_memory_worker_...` token-shaped
  value leaked through persisted memory/retrieval file paths.
- Duplicate command regression was already green before the production change
  because the command delegated to the idempotent promotion service.

Security evidence:

- Hook enqueue remains package `.delay(str(observation.id))`; queued task
  payloads are observation-id only and do not include API keys, bearer tokens,
  provider secrets, prompt bodies, or raw tool payloads.
- Candidate title/body/evidence are redacted. Memory metadata file paths and
  `RetrievalDocument` file paths are redacted when token-shaped values appear.
- Worker promotion uses the loaded `Observation` organization/project/team.
  The E2E retrieval query filters by project id and title.
- Duplicate delivery reuses the same candidate, memory, version, and retrieval
  document. The manual command duplicate regression returns `duplicate true`
  with stable ids.
- The E2E verifies `ContextBundleItem` and `MemoryRetrieved` `AuditEvent`
  records for the returned context bundle, request, and retrieval document.

## 2026-06-25: Celery Outbox Package Refactor

Branch: `fix/use-celery-outbox-package`

Scope:

- `apps/backend/engram/core/models.py`
- `apps/backend/engram/core/migrations/0003_delete_outboxevent.py`
- `apps/backend/engram/hooks/*`
- `apps/backend/engram/memory/*`
- `scripts/repository_layout.py`
- `tests/repository/*`
- `docs/superpowers/specs/2026-06-25-celery-outbox-package-design.md`
- `docs/superpowers/plans/2026-06-25-celery-outbox-package.md`
- historical outbox docs supersession notes

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| focused RED regression | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py::test_process_observation_recorded_task_rejects_malformed_observation_id -v` | Backend | yes | fixed | First ran exit 1 with `ValueError: badly formed hexadecimal UUID string`; task wrapper now raises `MemoryWorkerError('malformed observation id')`. |
| focused host backend | `cd apps/backend && poetry run pytest engram/core/core_models_tests.py engram/hooks/hook_ingest_tests.py engram/memory/memory_worker_tests.py -v` | Backend | yes | pass | Exit 0. Ran 57 tests for model cleanup, hook package transport, replay idempotency, memory candidate creation, redaction, malformed ids, and promotion. |
| full host backend | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 127 tests. |
| host lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| host format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. `68 files already formatted`. |
| host migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --skip-checks --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality | yes | pass | Exit 0. Ran 26 tests. |
| whitespace | `git diff --check` | Repository Quality whitespace step | yes | pass | Exit 0. |
| Docker image build | `docker compose -f deploy/compose/docker-compose.yml build api worker relay` | Compose E2E | yes | pass | Exit 0. Rebuilt images after code changes. |
| container focused backend | `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --with dev && poetry run pytest engram/hooks/hook_ingest_tests.py engram/memory/memory_worker_tests.py -v"` | Backend / Compose E2E | yes | pass | Exit 0. Ran 38 tests against Compose Postgres after fixing `FOR UPDATE` to lock only `Observation`. |
| container full backend plus lint | `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --with dev && poetry run pytest -v && poetry run ruff check . && poetry run ruff format --check ."` | Backend / Compose E2E | yes | pass | Exit 0 for pytest, ruff, and format. Ran 127 backend tests against Compose Postgres. |
| container migrations | `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "python manage.py migrate --noinput --settings=settings.test_settings && python manage.py makemigrations --check --dry-run --settings=settings.test_settings"` | Backend / Compose E2E | yes | pass | Exit 0. Applied through `core.0003_delete_outboxevent` and package migrations, then reported `No changes detected`. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Package relay delivered the observation-id Celery task, worker created a candidate, promotion succeeded, and a future context bundle included the memory. |
| Compose cleanup | `docker compose -f deploy/compose/docker-compose.yml ps --format json` | none | yes | pass | Exit 0 with no running services after E2E cleanup. |

First decisive failures fixed during the TDD/debug loop:

- Focused backend RED failed with missing `process_observation_recorded`.
- Direct container `pytest` failed because the production image intentionally
  installs only main dependencies; verification installs dev dependencies in
  the ephemeral test container.
- Container focused tests first failed on Postgres with `FOR UPDATE cannot be
  applied to the nullable side of an outer join`; `ProcessObservationRecorded`
  now locks only the `Observation` row with `select_for_update(of=('self',))`.
- Container `makemigrations` first failed with `celery_outbox.E006` before the
  default database schema existed; the management verification now runs
  `migrate` before migration freshness.

Accepted migration risk:

- `core.0003_delete_outboxevent` drops the old custom `core_outboxevent` table.
  Existing pending custom outbox rows are not migrated because the live contract
  now relies on `django-celery-outbox` transport rows. Production environments
  with custom outbox backlog must drain or snapshot it before applying this
  migration.

## 2026-06-25: Upstream Parity Audit Docs

Branch: `docs/parity-01-upstream-audit`

Scope:

- `docs/parity/claude-mem-parity-map.md`
- `docs/reference-gates.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none yet | yes | pass | Shows intended new docs plus pre-existing `.gitignore` change. |
| whitespace | `git diff --check` | none yet | yes | pass | Exit 0. |
| placeholder scan | `rg -n "[T]BD|[T]ODO|[F]IXME|[P]LACEHOLDER" docs/parity docs/reference-gates.md docs/verification-matrix.md` | none yet | yes | pass | Exit 1 with no matches. |
| docs content review | `sed -n '1,980p' docs/parity/claude-mem-parity-map.md`, `sed -n '1,700p' docs/reference-gates.md`, and `sed -n '1,200p' docs/verification-matrix.md` | none yet | yes | pass | Manual review completed against `goal.md` parity-map requirements. |

At this checkpoint, CI was not yet implemented on `master` for Engram's new
architecture branch.

## 2026-06-25: Monorepo Skeleton And Repository Quality CI

Branch: `feat/parity-02-monorepo-skeleton-ci`

Scope:

- `apps/backend/README.md`
- `apps/frontend/README.md`
- `packages/cli/README.md`
- `packages/mcp/README.md`
- `packages/claude-plugin/README.md`
- `packages/codex-plugin/README.md`
- `plugin-repository/README.md`
- `deploy/compose/README.md`
- `scripts/repository_layout.py`
- `scripts/repository_quality.py`
- `tests/repository/*`
- `.github/workflows/repository-quality.yml`
- `docs/superpowers/specs/2026-06-25-monorepo-skeleton-ci-design.md`
- `docs/superpowers/plans/2026-06-25-monorepo-skeleton-ci.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-02-monorepo-skeleton-ci` plus pre-existing unstaged `.gitignore` edit. |
| layout contract | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no findings. The private reference path allowlist covers `docs/reference-gates.md`. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality | yes | pass | Exit 0. Ran 8 tests. |
| workflow contract | `python3 -m unittest tests.repository.test_repository_quality_workflow -v` | Repository Quality | yes | pass | Exit 0. Proves workflow calls the Python checks and no longer uses the brittle shell grep. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |

First decisive failures fixed during the TDD loop:

- `python` was not available locally; the plan and workflow commands use
  `python3`.
- Layout test first failed with missing `scripts.repository_layout`.
- Quality test first failed with missing `scripts.repository_quality`.
- Test discovery first returned exit 5 with zero tests; package markers now make
  `python3 -m unittest discover -s tests -v` run the repository tests.
- Workflow test first failed because the workflow did not call the new scripts
  and still contained `grep -RInE`.

## 2026-06-25: Backend Health And Compose Runtime

Branch: `feat/parity-03-backend-health-compose`

Scope:

- `apps/backend/manage.py`
- `apps/backend/pyproject.toml`
- `apps/backend/poetry.lock`
- `apps/backend/pytest.ini`
- `apps/backend/settings/*`
- `apps/backend/engram/health/*`
- `apps/backend/engram/celery_app.py`
- `apps/backend/Dockerfile`
- `deploy/compose/docker-compose.yml`
- `deploy/compose/.env.example`
- `.github/workflows/backend.yml`
- `tests/repository/test_backend_runtime_contract.py`
- `tests/repository/test_backend_workflow.py`
- `docs/superpowers/specs/2026-06-25-backend-health-compose-design.md`
- `docs/superpowers/plans/2026-06-25-backend-health-compose.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-03-backend-health-compose` plus pre-existing unstaged `.gitignore` edit. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 3 health endpoint tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Exit 1. `docker` command is not available in this WSL distro, so live `docker compose up -d --build --wait` was not run. |

First decisive failures fixed during the TDD loop:

- Backend runtime layout test first failed because backend paths were not
  registered in `scripts/repository_layout.py`.
- Health endpoint tests first failed with HTTP 404 for `/-/healthz/`,
  `/-/readyz/`, and `/-/startup/`.
- Ruff first failed because lint quote configuration did not match the
  formatter's single-quote style.
- Compose contract tests first failed because Dockerfile and Compose files were
  missing.
- Backend workflow test first failed because `.github/workflows/backend.yml`
  was missing.

## 2026-06-25: Core Models And Migrations

Branch: `feat/parity-04-core-models`

Scope:

- `apps/backend/engram/core/models.py`
- `apps/backend/engram/core/migrations/0001_initial.py`
- `apps/backend/engram/core/migrations/0002_remove_outboxevent_core_outbox_unique_idempotency_key_per_event_and_more.py`
- `apps/backend/engram/core/core_models_tests.py`
- `apps/backend/settings/settings.py`
- `.github/workflows/backend.yml`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`
- `tests/repository/test_backend_workflow.py`
- `docs/superpowers/specs/2026-06-25-core-models-design.md`
- `docs/superpowers/plans/2026-06-25-core-models.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-04-core-models` plus pre-existing unstaged `.gitignore` edit. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. Core model and initial migration paths are required. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests. |
| core model tests | `cd apps/backend && poetry run pytest engram/core/core_models_tests.py -v` | Backend | yes | pass | Exit 0. Ran 22 tests for scoped uniqueness, event replay, observation dedupe, source provenance, save-time cross-scope rejection, retrieval scope, context citations, and source-scoped outbox idempotency. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 25 backend tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| migration apply | `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings` | Backend | yes | pass | Exit 0. Applied Django auth/contenttypes/core/sessions migrations, including core 0001 and 0002, against the test database. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Docker is still unavailable in this WSL distro; live Compose smoke remains blocked until Docker Desktop WSL integration is enabled. |

First decisive failures fixed during the TDD loop:

- Core model test first failed with `ModuleNotFoundError: No module named
  'engram.core.models'`.
- Observation source provenance test first failed because `ObservationSource`
  did not exist.
- Repository gate tests first failed because the core model/migration paths were
  not listed in `scripts/repository_layout.py`.
- Backend workflow test first failed because `.github/workflows/backend.yml`
  did not run migration freshness or migration apply commands.
- `cd apps/backend && poetry run python manage.py migrate --check
  --settings=settings.test_settings` exited 1 on a fresh database because it
  detects unapplied migrations rather than applying them; the gate uses
  `migrate --noinput --settings=settings.test_settings` to prove the migration
  applies cleanly.
- Review pass found that model `clean()` methods did not run on normal saves;
  cross-scope `objects.create()` regression tests now cover the tenant/project
  consistency paths.
- Review pass found outbox idempotency lacked an explicit source dimension;
  `OutboxEvent` now carries `source_type` and `source_id`, and the uniqueness
  constraint includes event type, source, and idempotency key.

## 2026-06-25: Auth Scope And API Keys

Branch: `feat/parity-05-auth-scope`

Scope:

- `apps/backend/engram/access/models.py`
- `apps/backend/engram/access/services.py`
- `apps/backend/engram/access/access_scope_tests.py`
- `apps/backend/engram/access/migrations/0001_initial.py`
- `apps/backend/engram/access/migrations/0002_seed_default_roles.py`
- `apps/backend/settings/settings.py`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/superpowers/specs/2026-06-25-auth-scope-design.md`
- `docs/superpowers/plans/2026-06-25-auth-scope.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-05-auth-scope` plus pre-existing unstaged `.gitignore` edit. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. Access model, service, tests, and migrations are required paths. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests. |
| access scope tests | `cd apps/backend && poetry run pytest engram/access/access_scope_tests.py -v` | Backend | yes | pass | Exit 0. Ran 18 tests for seed roles/capabilities, hash-only API-key storage, prefix collisions, capability narrowing, project/team/org denial, resolved scope filters, unusable key states, and cross-scope FK rejection. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 43 backend tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| migration apply | `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings` | Backend | yes | pass | Exit 0. Applied core, access, Django auth/contenttypes, and sessions migrations against the test database. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Exit 1. Docker is still unavailable in this WSL distro; live Compose smoke remains blocked until Docker Desktop WSL integration is enabled. |

First decisive failures fixed during the TDD and review loop:

- Access scope test first failed with `ModuleNotFoundError: No module named
  'engram.access.models'`.
- Denial audits first disappeared because audit rows were written inside a
  transaction that rolled back when `AccessDeniedError` was raised.
- Focused lint first failed on import sorting, lambda argument names, and the
  domain exception class name.
- Local review found `key_prefix` was incorrectly unique and lookup used
  `.first()`; prefix collisions are now allowed and hash-verified.
- Security review found unbound API keys could use owner project-admin
  capability without the key carrying `projects:*` or `policy:admin`; unbound
  project expansion now requires effective key capability.
- Security review found unbound keys trusted client-supplied team ids; team
  hints now require effective team capability and same-organization project/team
  linkage.
- Security review found audit rows lacked resolved scope filters; allow/deny
  metadata now records resolved `organization_id`, `project_ids`, and
  `team_ids`, and single-project/team allows populate audit FK fields.

## 2026-06-25: Hook Dry-Run And Observation Ingest

Branch: `feat/parity-06-hook-ingest`

Scope:

- `apps/backend/engram/hooks/*`
- `apps/backend/settings/settings.py`
- `apps/backend/settings/urls.py`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/superpowers/specs/2026-06-25-hook-ingest-design.md`
- `docs/superpowers/plans/2026-06-25-hook-ingest.md`
- `docs/verification-matrix.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-06-hook-ingest` plus pre-existing unstaged `.gitignore` edit. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. Hook app config, serializers, services, URLs, views, and tests are required paths. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests. |
| backend runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 5 tests requiring hook app paths. |
| hook ingest tests | `cd apps/backend && poetry run pytest engram/hooks/hook_ingest_tests.py -v` | Backend | yes | pass | Exit 0. Ran 14 tests for dry-run, scope denial, ingest writes, thin payload normalization, redaction, key-bound team persistence, replay, race handling, validation, and session-end. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 57 tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. `36 files already formatted`. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| migration apply | `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings` | Backend | yes | pass | Exit 0. Applied core, access, Django auth/contenttypes, and sessions migrations against the test database. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. `All set!`. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| focused security review | Independent review agent plus local diff/readback | none yet | yes | pass | Review findings fixed: persisted secret redaction, key-bound team persistence, replay race handling, non-object payload validation, and request-signature deferral documentation. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Exit 1. Docker is still unavailable in this WSL distro; live Compose smoke remains blocked until Docker Desktop WSL integration is enabled. |

First decisive failures fixed during the TDD and review loop:

- Hook API tests first failed before implementation because `engram.hooks`
  endpoints and app files were missing.
- Review-driven red test first failed because raw hook payload and observation
  text persisted token-shaped values and secret-bearing keys.
- Review-driven red test first failed because a team-scoped API key could create
  teamless session/raw-event/observation/outbox rows when the request omitted
  `team_id`.
- Review-driven red test first failed because non-object JSON `payload` values
  were accepted instead of returning HTTP 400.
- Review-driven red test first failed because a replay insert race raised an
  uncaught `IntegrityError` on the raw-event uniqueness constraint.
- Focused lint first failed on import ordering in
  `engram/hooks/hook_ingest_tests.py`.

## 2026-06-25: Memory Candidate Worker

Branch: `feat/parity-07-memory-worker`

Scope:

- `apps/backend/engram/memory/*`
- `apps/backend/settings/settings.py`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/superpowers/specs/2026-06-25-memory-worker-design.md`
- `docs/superpowers/plans/2026-06-25-memory-worker.md`
- `docs/verification-matrix.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-07-memory-worker` plus pre-existing unstaged `.gitignore` edit. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. Memory app config, services, tasks, and tests are required paths. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests. |
| backend runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 5 tests requiring memory worker paths. |
| memory worker tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | pass | Exit 0. Ran 8 tests for candidate creation, redaction, downstream event emission, duplicate delivery, pending-source reuse, failure marking, malformed id handling, and Celery task delegation. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 65 tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. `41 files already formatted`. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| migration apply | `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings` | Backend | yes | pass | Exit 0. Applied core, access, Django auth/contenttypes, and sessions migrations against the test database. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. `All set!`. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| focused security review | Independent read-only review plus local diff/readback | none yet | yes | pass | Review findings fixed: malformed non-scalar `observation_id` now marks failed, and candidate title/body/evidence are redacted before persistence. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Exit 1. Docker is still unavailable in this WSL distro; live Compose smoke remains blocked until Docker Desktop WSL integration is enabled. |

First decisive failures fixed during the TDD loop:

- Memory worker tests first failed with `ModuleNotFoundError: No module named
  'engram.memory.services'`.
- The first implementation pass then made
  `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v`
  pass with 6 tests.
- Independent review found non-scalar `observation_id` could escape failed-row
  marking; a regression now covers the Django `ValidationError` path.
- Independent review found candidate evidence was not sink-redacted; a
  regression now covers candidate title, body, and evidence redaction.
- Focused lint first failed on a test sentinel path containing `/tmp/`; the
  sentinel now uses a repo-shaped path.

## 2026-06-25: CLI Lifecycle

Branch: `feat/parity-11-cli-lifecycle`

Scope:

- `packages/cli/*`
- `.github/workflows/backend.yml`
- `.github/workflows/repository-quality.yml`
- `scripts/repository_layout.py`
- `tests/repository/test_repository_layout.py`
- `tests/repository/test_backend_workflow.py`
- `tests/repository/test_repository_quality_workflow.py`
- `docs/superpowers/specs/2026-06-25-cli-lifecycle-design.md`
- `docs/superpowers/plans/2026-06-25-cli-lifecycle.md`
- `docs/verification-matrix.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-11-cli-lifecycle` plus pre-existing unstaged `.gitignore` edit. |
| CLI lifecycle tests | `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` | Backend and Repository Quality | yes | pass | Exit 0. Ran 16 tests for connect, doctor, disconnect, redaction, derived-only fingerprints, malformed URL handling, dry-run failure, hook manifests, and strict credential file mode. |
| CLI syntax | `python3 -m compileall packages/cli/engram_cli` | none yet | yes | pass | Exit 0. Compiled the package without syntax errors. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality and Backend | yes | pass | Exit 0 with no output. CLI package metadata, command modules, and lifecycle tests are required paths. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality and Backend | yes | pass | Exit 0 with no findings. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Ran 14 tests, including workflow assertions that both CI jobs run CLI tests. |
| backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Ran 79 backend tests. |
| backend lint | `cd apps/backend && poetry run ruff check .` | Backend | yes | pass | Exit 0. |
| backend format | `cd apps/backend && poetry run ruff format --check .` | Backend | yes | pass | Exit 0. |
| migration freshness | `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings` | Backend | yes | pass | Exit 0. `No changes detected`. |
| migration apply | `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings` | Backend | yes | pass | Exit 0. Applied migrations against the test database. |
| backend Poetry metadata | `cd apps/backend && poetry check` | Backend | yes | pass | Exit 0. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |
| live Compose availability | `docker compose version` | future Compose smoke | yes | blocked | Exit 1. Docker is still unavailable in this WSL distro; live Compose smoke remains blocked until Docker Desktop WSL integration is enabled. |

First decisive failures fixed during the TDD loop:

- CLI unittest discovery first exited 5 with zero tests because the new
  `engram_cli` directory was not importable; the package marker now lets
  discovery reach the intended missing-interface failure.
- CLI tests next failed with `ImportError: cannot import name 'main' from
  'engram_cli'`, proving production command code was not present before the
  tests.
- A redaction regression first failed because a server error detail echoing the
  submitted API key was printed verbatim; CLI error rendering now redacts the
  active credential in `connect` and `doctor`.
- Root repository tests first failed because CLI package paths and CI commands
  were not part of the layout/workflow contracts.
- Independent review found the credential fingerprint leaked raw key prefix
  material into stdout, config, and hook manifests; fingerprints now use only
  derived SHA-256 material, including for short keys.
- Independent review found malformed server URLs could reach transport and
  escape as Python exceptions; connect and doctor now validate server URLs
  before health or dry-run calls.

## 2026-06-25: Upstream Migration Import

Branch: `feat/parity-13-upstream-migration-import`

Scope:

- `apps/backend/engram/imports/management/__init__.py`
- `apps/backend/engram/imports/management/commands/__init__.py`
- `apps/backend/engram/imports/management/commands/engram_import_claude_mem.py`
- `apps/backend/engram/imports/services.py`
- `apps/backend/engram/imports/upstream_import_tests.py`
- `apps/backend/engram/core/redaction.py`
- `.github/workflows/backend.yml`
- `tests/repository/test_backend_workflow.py`
- `docs/security/reviews/2026-06-25-upstream-migration-import.md`
- `docs/verification-matrix.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-13-upstream-migration-import` with Task 5 implementation files modified by other workers. |
| command and CI review | Review over `.superpowers/sdd/task-5-workingtree-review.diff` | Backend | yes | pass | SPEC APPROVED / QUALITY APPROVED; no blocking findings. |
| repository workflow contract | `python3 -m unittest tests.repository.test_backend_workflow -v` | Backend and Repository Quality | yes | pass | Exit 0. Ran 4 tests OK. |
| stale container image check | `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --with dev --no-interaction && pytest engram/imports/upstream_import_tests.py -v && ruff check engram/imports engram/core/redaction.py && ruff format --check engram/imports engram/core/redaction.py"` | none | no | fixed | Exit 4. First decisive failure: `ERROR: file or directory not found: engram/imports/upstream_import_tests.py`; fixed by rerunning with `--build`. |
| focused importer container gate | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --with dev --no-interaction && pytest engram/imports/upstream_import_tests.py -v && ruff check engram/imports engram/core/redaction.py && ruff format --check engram/imports engram/core/redaction.py"` | Backend equivalent | yes | pass | Exit 0. Importer pytest reported 15 passed; ruff check and format check were clean. |
| final blocker RED tests | `cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v` | Backend | yes | fixed | Exit 1. Four new tests failed as expected: changed source-id rows created new memories, missing source sessions were not unsupported, `--team-id` was required, and imported agent id / JSON-string metadata leaked. |
| final blocker focused tests | `cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v` | Backend | yes | pass | Exit 0. Importer pytest reported 19 passed after fixes. |
| local migration contract | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend migration steps use PostgreSQL service | yes | pass | Exit 0. Compose/PostgreSQL reported `No migrations to apply` and `No changes detected`. |
| focused security review | Initial review, security fixes, and security re-review recorded in `docs/security/reviews/2026-06-25-upstream-migration-import.md` | none | yes | pass | Initial SECURITY CHANGES_REQUIRED findings were fixed; re-review SECURITY APPROVED with CRITICAL none, IMPORTANT none, MINOR none. |
| evidence docs quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 after evidence files were written. |
| whitespace | `git diff --check` | Repository Quality whitespace step | yes | pass | Exit 0 after security fixes. |

First decisive failures fixed during the Task 5 loop:

- Management-command red test first failed with
  `django.core.management.base.CommandError: Unknown command:
  'engram_import_claude_mem'`.
- Repository workflow test first failed because `.github/workflows/backend.yml`
  did not run `poetry run pytest engram/imports/upstream_import_tests.py -v`.
- Security red tests first failed because `settings.json` was not reported,
  mixed upstream projects did not raise `ClaudeMemImportError`, and Gemini,
  Telegram, and Slack token shapes could persist in imported records.
- Final blocker tests first failed because import idempotency still depended on
  content-derived observation hashes, missing upstream memory sessions were
  counted as duplicate rows instead of unsupported rows, the command required
  `--team-id`, and upstream `agent_id` / JSON-string sensitive metadata could
  persist in imported rows.
- The first container verification run used a stale image and exited 4 with
  `ERROR: file or directory not found:
  engram/imports/upstream_import_tests.py`; rerunning with `--build` passed.
- The local migration command contract was corrected to the Compose/PostgreSQL
  command above because host SQLite migration checks do not exercise the
  PostgreSQL behavior required by the backend outbox code.
- Evidence docs verification passed with `python3 scripts/repository_quality.py`
  exit 0 and `git diff --check` exit 0 after the security artifact, verification
  matrix entry, and evidence report were written.

## 2026-06-25: Hook Event Coverage

Branch: `feat/parity-14-hook-event-coverage`

Artifact head before evidence-head correction:
`76bd251c763513ce3d627967b592c3f9ef1fca8f`

Security fix commit: `3a3952fd303dfcf2d8a401f1cd10240380a97de2`

Scope:

- Task 2 backend hook endpoints: commit `c6ed9ae8`; review clean.
- Task 3 CLI commands and Codex adapter: commits `8da62b21` and README fix
  `400e75e8`; review clean.
- Task 4 Codex plugin contract: commit `f49e8aeb`; review clean.
- Session-start lifecycle payload fix: commit `c24669f3`; review clean.
- Stable hook idempotency fix: commit `3a3952fd`; focused security re-review
  approved.
- Evidence-only docs update for parity map, verification matrix, and Compose
  README: commit `76bd251c`.

PR status: merged through
`https://github.com/Barsoomx/engram/pull/11`.

Merge commit:
`cc2f1f2e5baa9b49af74b195774d18482eb94e4f`.

CI status on the merge commit: pass.

- Backend:
  `https://github.com/Barsoomx/engram/actions/runs/28172695242`
- Compose E2E:
  `https://github.com/Barsoomx/engram/actions/runs/28172695204`
- Repository Quality:
  `https://github.com/Barsoomx/engram/actions/runs/28172695225`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 after the stable-id fix. |
| backend hook tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py -v"` | Backend equivalent | yes | pass | Exit 0. Task 2 report: 21 passed at commit `c6ed9ae8`; review clean. |
| CLI hook tests | `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` | Backend and Repository Quality | yes | pass | Exit 0. Full CLI suite after stable-id fix: 28 tests OK. |
| CLI syntax | `python3 -m compileall packages/cli/engram_cli` | none yet | yes | pass | Exit 0. Fix verification after `c24669f3`: CLI package compiled. |
| Codex plugin contract tests | `python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v` | none yet | yes | pass | Exit 0. Task 4 report: OK after 2 tests at commit `f49e8aeb`; review clean. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Fresh Task 5 docs pass: 22 tests OK. |
| backend full tests first run | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Backend equivalent | yes | fixed | Exit 1 when run in parallel with `python3 scripts/e2e_golden_path.py`. First decisive failure: the E2E script ran `docker compose down -v` while pytest still used Postgres, causing `FATAL: the database system is shutting down` and `failed to resolve host 'postgres'`. |
| backend full tests serial rerun | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Backend equivalent | yes | pass | Exit 0. Serial rerun after the parallel Compose conflict: 114 passed. |
| Compose backend lint | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check ."` | Backend lint equivalent | yes | pass | Exit 0. |
| Compose backend format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff format --check ."` | Backend format equivalent | yes | pass | Exit 0. |
| Compose migration command | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend migration steps use PostgreSQL service | yes | pass | Exit 0. Fresh Task 5 docs pass: migrations applied and `No changes detected`. |
| local Compose golden path first run | `python3 scripts/e2e_golden_path.py` | future Compose E2E | yes | fixed | Exit 1. First decisive failure: future `engram_cli hook session-start` failed with `invalid_response: Server returned invalid JSON`; direct POST to `/v1/hooks/session-start` returned HTTP 500 HTML with `django.core.exceptions.ValidationError: {'payload': ['This field cannot be blank.']}` because the CLI sent an empty nested hook `payload={}` for session-start context input. |
| local Compose golden path rerun | `python3 scripts/e2e_golden_path.py` | future Compose E2E | yes | pass | Exit 0 after both CLI fixes. |
| current master Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0 on `cc2f1f2e5baa9b49af74b195774d18482eb94e4f`: Compose started, host CLI connected, hook observation submitted, relayed memory candidate promoted, future session context returned, and Compose stopped. |
| focused security review | Initial review, stable-id fix, and security re-review recorded in `docs/security/reviews/2026-06-25-hook-event-coverage.md` | none | yes | pass | Initial SECURITY CHANGES_REQUIRED finding was fixed in `3a3952fd`; re-review SECURITY APPROVED with CRITICAL none, IMPORTANT none, MINOR none. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0. Fresh Task 5 docs verification after evidence update. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. Fresh Task 5 docs verification after evidence update. |

First decisive failures fixed during the hook event coverage loop:

- Task 2 backend endpoint red test first failed with HTTP 404 for
  `/v1/hooks/session-start`; fixed by adding the endpoint and lifecycle event
  persistence.
- Task 3 CLI red test first failed with `KeyError: 'commands'`; fixed by
  writing event-specific hook commands and adding hook subcommands.
- Task 4 Codex plugin contract tests first failed because
  `packages/codex-plugin/.codex-plugin/plugin.json` and
  `packages/codex-plugin/plugin/hooks/codex-hooks.json` were missing; fixed by
  adding the package-local contract fixtures.
- Compose E2E first failed after Task 4 because `session-start` sent an empty
  nested hook payload to the backend lifecycle endpoint; fixed in `c24669f3`.
- Focused security review at `b7aeb007` found random fallback hook
  `event_id`, `idempotency_key`, and `content_hash` generation broke replay and
  idempotency; fixed in `3a3952fd` by deriving stable fallback values.
- A parallel verification run failed because `python3 scripts/e2e_golden_path.py`
  ran `docker compose down -v` while the full backend pytest command still used
  Postgres; serial rerun passed with 114 tests.

## 2026-06-25: First Parity Gate Evidence And Request Size Limits

Branch: `chore/parity-gate-evidence`

PR status: merged through
`https://github.com/Barsoomx/engram/pull/12`.

Merge commit:
`8de3c263928164a4581700bc1152b917e7023574`.

CI status on the merge commit: pass.

- Backend:
  `https://github.com/Barsoomx/engram/actions/runs/28175303549`
- Compose E2E:
  `https://github.com/Barsoomx/engram/actions/runs/28175303754`
- Repository Quality:
  `https://github.com/Barsoomx/engram/actions/runs/28175304695`

Scope:

- `docs/parity/2026-06-25-first-parity-gate-report.md`
- `docs/security/reviews/2026-06-25-first-parity-gate-rollup.md`
- `docs/verification-matrix.md`
- `apps/backend/engram/hooks/serializers.py`
- `apps/backend/engram/hooks/hook_ingest_tests.py`
- `apps/backend/engram/context/serializers.py`
- `apps/backend/engram/context/context_api_tests.py`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| checkpoint-start Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0 on checkpoint start SHA `cc2f1f2e5baa9b49af74b195774d18482eb94e4f`: Compose started, host CLI connected, hook observation submitted, relayed memory candidate promoted, future session context returned, and Compose stopped. |
| post-fix Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0 after the request-size-limit fix and evidence docs: Compose started, host CLI connected, hook observation submitted, relayed memory candidate promoted, future session context returned, and Compose stopped. |
| request-size-limit focused tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py engram/context/context_api_tests.py -v"` | Backend equivalent | yes | pass | Exit 0. Reported 44 passed after hook/context serializer caps were added. |
| backend full tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Backend | yes | pass | Exit 0. Reported 123 passed after request-size-limit fix. |
| request-size-limit lint and format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check engram/hooks engram/context && ruff format --check engram/hooks engram/context"` | Backend equivalent | yes | pass | Exit 0. Ruff check passed and format check reported 14 files already formatted. |
| backend lint | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check ."` | Backend | yes | pass | Exit 0. |
| backend format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff format --check ."` | Backend | yes | pass | Exit 0. Reported 64 files already formatted. |
| migration apply and freshness | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend | yes | pass | Exit 0. Migrations applied and `No changes detected`. |
| CLI tests | `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` | Backend and Repository Quality | yes | pass | Exit 0. Reported 28 tests OK. |
| Codex plugin contract tests | `python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v` | none yet | yes | pass | Exit 0. Reported 2 tests OK. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality and Backend | yes | pass | Exit 0. Reported 22 tests OK. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 after evidence docs were added. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 after evidence docs were added. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0 after evidence docs were added. |
| focused security roll-up | Independent read-only security review and re-review recorded in `docs/security/reviews/2026-06-25-first-parity-gate-rollup.md` | none | yes | pass | Initial SECURITY CHANGES_REQUIRED finding for missing hook/context size caps was fixed; re-review SECURITY APPROVED with CRITICAL none, IMPORTANT none, MINOR none. |

First decisive failures fixed during this checkpoint:

- Fresh gate audit found the evidence report was missing and the hook-event
  matrix still said CI was pending even though PR `#11` and the master push CI
  were green.
- Focused security roll-up found authenticated hook/context request content had
  no Engram-level per-event/per-field caps before persistence and retrieval
  processing.
- TDD red tests first failed because oversized hook inputs returned HTTP 202 and
  oversized context inputs returned HTTP 200 instead of HTTP 400.
- A follow-up context repository metadata red test first failed because
  oversized `repository_url`, `repository_root`, and `cwd` still returned HTTP
  200 instead of HTTP 400.
- A reviewer-driven context metadata red test first failed with a Django
  validation error on oversized `AuditEvent.correlation_id`; context serializer
  caps now reject oversized `agent_version`, `agent_external_id`,
  `correlation_id`, `trace_id`, and `branch` before records are created.

## 2026-06-25: Memory Feedback Loop

Branch: `feat/memory-feedback-loop`

Implementation review SHA:
`824d532bfc627d3209a5f586e63cbe738bdb5103`

Scope:

- `apps/backend/engram/memory/serializers.py`
- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/views.py`
- `apps/backend/engram/memory/urls.py`
- `apps/backend/engram/memory/memory_feedback_tests.py`
- `apps/backend/settings/urls.py`
- `docs/superpowers/specs/2026-06-25-memory-feedback-loop-design.md`
- `docs/superpowers/plans/2026-06-25-memory-feedback-loop.md`
- `docs/security/reviews/2026-06-25-memory-feedback-loop.md`
- `docs/verification-matrix.md`

This checkpoint proves only the backend stale/refuted memory feedback loop:
authorized `memories:review` callers can mark memory stale or refuted, the
retrieval projection flags are updated, audit metadata is redacted, and future
context retrieval excludes corrected memory. It does not prove frontend/admin
review UI, MCP `memory.feedback`, daily curator jobs, provider/model-policy
calls, semantic/vector retrieval, or broader memory quality workflows.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch`; `git rev-parse HEAD`; `git rev-parse origin/master`; `git rev-parse upstream` | none | yes | pass | Exit 0. Branch was `feat/memory-feedback-loop`; final code/test head was `824d532bfc627d3209a5f586e63cbe738bdb5103`; `origin/master` was `9967f2156e3126f225745ea9bdfff114ec7ac2ff`; `upstream` was `3fe0725a97e18b5edf3e61cde60e181ab2b6c997`. |
| focused memory feedback tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_feedback_tests.py -v"` | Backend equivalent | yes | pass | Exit 0. Reported 9 passed, covering stale/refuted updates, future context exclusion, missing `memories:review`, project-scope denial, cross-team denial for team-visible memory, project-visible memory correction, oversized reason/request/correlation rejection, audit target/capability, and raw key redaction. |
| adjacent context/access tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py engram/access/access_scope_tests.py -v"` | Backend equivalent | yes | pass | Exit 0. Reported 37 passed, covering retrieval filters, team/project scope denial, key capability narrowing, and access audit scope filters. |
| full backend tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Backend | yes | pass | Exit 0. Reported 132 passed. |
| backend lint and format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. Ruff check reported `All checks passed!`; format check reported 68 files already formatted. |
| migration apply and freshness | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend | yes | pass | Exit 0. Migrations applied against Compose PostgreSQL and `makemigrations --check --dry-run` reported `No changes detected`. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Compose started, host CLI connected, hook observation submitted, relayed memory candidate promoted, future session context returned, and Compose stopped. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no findings. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0 with no findings. |
| focused security review | Manual diff/readback plus command evidence recorded in `docs/security/reviews/2026-06-25-memory-feedback-loop.md` | none | yes | pass | SECURITY APPROVED for the backend stale/refuted feedback checkpoint. CRITICAL none, IMPORTANT none, MINOR none. |

No decisive verification failures occurred during this Task 2 evidence run.
Final whole-branch review found missing endpoint proof for cross-team feedback
denial and oversized `request_id`/`correlation_id`; the follow-up
`test: cover memory feedback denials` added those tests and reran the evidence
commands above.

## 2026-06-25: Claude Code Client Package

Branch: `feat/parity-15-claude-code-client`

Implementation commit:
`128b2afed125d8880b85195cd27ceb9afd4e4ea8`.

Scope:

- `packages/claude-plugin/.claude-plugin/plugin.json`
- `packages/claude-plugin/hooks/hooks.json`
- `packages/claude-plugin/claude_plugin_contract_tests.py`
- `packages/claude-plugin/README.md`
- `packages/cli/engram_cli/main.py`
- `packages/cli/engram_cli/commands.py`
- `packages/cli/engram_cli/cli_lifecycle_tests.py`
- `docs/parity/claude-mem-parity-map.md`
- `docs/security/reviews/2026-06-25-claude-code-client.md`
- `docs/superpowers/specs/2026-06-25-claude-code-client-design.md`
- `docs/superpowers/plans/2026-06-25-claude-code-client.md`
- `scripts/repository_layout.py`

This checkpoint proves native Claude Code package and CLI response-format
coverage only for the currently implemented Engram hook events:
`SessionStart`, `PostToolUse`, `Error`, and `Decision`. It does not prove
`UserPromptSubmit`, `PreToolUse`, or `Stop`.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| TDD red CLI tests | `PYTHONPATH=packages/cli python3 -m unittest packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_connect_writes_event_specific_hook_commands packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_hook_session_start_claude_code_response_format_emits_claude_output_only packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_hook_non_session_claude_code_response_format_emits_empty_ack -v` | none | yes | pass | Exit 1 before implementation, with failures for missing `claude-code` response format and old hook command strings. |
| focused CLI tests | `PYTHONPATH=packages/cli python3 -m unittest packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_connect_writes_event_specific_hook_commands packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_hook_session_start_claude_code_response_format_emits_claude_output_only packages.cli.engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_hook_non_session_claude_code_response_format_emits_empty_ack -v` | none | yes | pass | Exit 0. Reported 3 tests OK. |
| full CLI tests | `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` | Repository Quality equivalent | yes | pass | Exit 0. Reported 30 tests OK. |
| Claude plugin contract tests | `python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v` | none | yes | pass | Exit 0. Reported 2 tests OK. |
| Codex plugin contract tests | `python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v` | none | yes | pass | Exit 0. Reported 2 tests OK. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality equivalent | yes | pass | Exit 0. Reported 30 tests OK. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0 with no output. |
| focused security review | Independent read-only security review plus command evidence recorded in `docs/security/reviews/2026-06-25-claude-code-client.md` | none | yes | pass | SECURITY APPROVED. CRITICAL none, IMPORTANT none, MINOR none. Live Claude Code plugin install was not run; this checkpoint validates manifest and CLI contracts only. |

## 2026-06-25: Admin Inspection API

Branch: `feat/admin-inspection-api`

Final checkpoint SHA is recorded in the status report after commit.

Scope:

- `apps/backend/engram/inspection/__init__.py`
- `apps/backend/engram/inspection/apps.py`
- `apps/backend/engram/inspection/serializers.py`
- `apps/backend/engram/inspection/services.py`
- `apps/backend/engram/inspection/views.py`
- `apps/backend/engram/inspection/urls.py`
- `apps/backend/engram/inspection/inspection_api_tests.py`
- `apps/backend/settings/settings.py`
- `apps/backend/settings/urls.py`
- `docs/security/reviews/2026-06-25-admin-inspection-api.md`
- `docs/superpowers/specs/2026-06-25-admin-inspection-api-design.md`
- `docs/superpowers/plans/2026-06-25-admin-inspection-api.md`
- `scripts/repository_layout.py`

This checkpoint proves the minimal V1 operational inspection API:
read-only memory inspection, context-bundle inspection, and audit-event
inspection through `/v1/inspection/*`. It does not add a custom frontend, MCP
bridge, semantic retrieval, provider/model-policy code, or memory mutation
beyond the existing feedback endpoint.

Memory and context-bundle inspection require `memories:admin`; regular
developer-style `memories:read` keys are denied. Audit inspection requires
`audit:read`.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| TDD red inspection tests | `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` | none | yes | pass | Exit 1 before implementation. All four tests failed with 404 because `/v1/inspection/*` routes did not exist. |
| string redaction RED tests | `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py::test_memory_inspection_lists_authorized_memories_and_redacts_detail_metadata engram/inspection/inspection_api_tests.py::test_context_bundle_inspection_returns_items_and_hides_other_team_bundles -v` | none | yes | pass | Exit 1 after initial implementation. Failures proved raw token-shaped values leaked through memory title/body and context `rendered_text`; output redaction was expanded before green. |
| security-fix RED tests | `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` | none | yes | pass | Exit 1 after independent review. Failures proved regular `memories:read` keys could inspect memory/context data and audit identifiers were returned raw. |
| focused inspection tests | `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` | Backend equivalent | yes | pass | Exit 0 after security fixes. Reported 4 passed. |
| adjacent access/context/memory tests | `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py engram/access/access_scope_tests.py engram/context/context_api_tests.py engram/memory/memory_feedback_tests.py -v` | Backend equivalent | yes | pass | Exit 0 after security fixes. Reported 50 passed. |
| host manage.py check | `cd apps/backend && poetry run python manage.py check` | none | yes | fixed | Exit 1 because the host process cannot resolve the Compose-only `postgres` hostname. Reran inside Compose after migration. |
| Compose migrate and manage.py check | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check"` | Backend equivalent | yes | pass | Exit 0. Fresh Compose DB migrated and system check reported no issues. |
| full backend tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Backend | yes | pass | Exit 0. Reported 159 passed. |
| backend lint and format | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. Ruff check passed; format check reported 101 files already formatted. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality equivalent | yes | pass | Exit 0. Reported 30 tests OK. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0 with no output. |
| focused security review | Independent read-only security review plus fix re-review recorded in `docs/security/reviews/2026-06-25-admin-inspection-api.md` | none | yes | pass | Initial review found weak capability gate, identifier redaction gaps, and audit-listing self-noise. Fix re-review verified code-level findings resolved; stale doc statuses were updated before commit. |

## 2026-06-25: Celery SLA Compose Topology

Branch: `chore/celery-sla-compose-topology`

Final checkpoint SHA is recorded in the status report after commit.

Scope:

- `deploy/compose/docker-compose.yml`
- `deploy/compose/README.md`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/security/reviews/2026-06-25-celery-sla-compose-topology.md`
- `docs/superpowers/specs/2026-06-25-celery-sla-compose-topology-design.md`
- `docs/superpowers/plans/2026-06-25-celery-sla-compose-topology.md`
- `docs/verification-matrix.md`

This checkpoint keeps the existing Engram Celery foundation and makes Compose
consume its SLA queues explicitly. Compose now runs dedicated workers for
`engram-realtime`, `engram-near-realtime`, `engram-batch`,
`engram-highmemory`, and `engram-domain-events`; the package relay remains a
separate `python manage.py celery_outbox_relay` service.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| delegated RED repository contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | fixed | Exit 1 before implementation. The new contract failed against the old single generic worker because `worker-realtime` and queue routing were missing. |
| repository runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | pass | Exit 0. Reported 15 tests OK. |
| Compose service graph | `docker compose -f deploy/compose/docker-compose.yml config --quiet` | Compose E2E | yes | pass | Exit 0 with no output. |
| backend Celery foundation tests | `cd apps/backend && poetry run pytest engram/core/celery_foundation_tests.py -v` | Backend | yes | pass | Exit 0. Reported 7 passed. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality | yes | pass | Exit 0. Reported 31 tests OK. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0 with no output. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Output included worker-created retrieval document, future context injection, Compose golden path passed, and stopped Compose services. |
| focused security review | Independent read-only review plus command evidence recorded in `docs/security/reviews/2026-06-25-celery-sla-compose-topology.md` | none | yes | pass | Reviewer reported no Critical, Important, or Minor findings. Verified queue coverage, separate relay, unchanged broker/result configuration, no public-doc private reference leakage, and sufficient Compose/E2E evidence. |

## 2026-06-25: Model Policy Secrets Foundation

Branch: `feat/model-policy-secrets`

Final checkpoint SHA is recorded in the status report after commit.

Scope:

- `apps/backend/engram/model_policy/__init__.py`
- `apps/backend/engram/model_policy/apps.py`
- `apps/backend/engram/model_policy/models.py`
- `apps/backend/engram/model_policy/migrations/0001_initial.py`
- `apps/backend/engram/model_policy/serializers.py`
- `apps/backend/engram/model_policy/services.py`
- `apps/backend/engram/model_policy/views.py`
- `apps/backend/engram/model_policy/urls.py`
- `apps/backend/engram/model_policy/model_policy_tests.py`
- `apps/backend/settings/settings.py`
- `apps/backend/settings/urls.py`
- `apps/backend/pyproject.toml`
- `apps/backend/poetry.lock`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`
- `docs/security/reviews/2026-06-25-model-policy-secrets.md`
- `docs/superpowers/specs/2026-06-25-model-policy-secrets-design.md`
- `docs/superpowers/plans/2026-06-25-model-policy-secrets.md`

This checkpoint adds the first backend provider secret and model policy
foundation. It supports organization/team provider secret references, encrypted
database envelopes, project/team/organization model policy resolution, fake
provider adapter selection, and provider call audit records. It does not add
real provider network calls, semantic retrieval, frontend/admin UI, MCP tools,
or AI workflow jobs.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| initial RED focused test | `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py -v` | Backend | yes | fixed | Exit 2 before implementation. Failure: `ModuleNotFoundError: No module named 'engram.model_policy.models'`. |
| cross-team secret RED regression | `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py::test_provider_secret_detail_and_rotation_hide_other_team_secret -v` | Backend | yes | fixed | Exit 1 after initial implementation. Detail returned `200` for another team's provider secret; scoped secret filtering now returns 404 and rotate returns `secret_scope_denied`. |
| team-bound project policy RED regression | `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py::test_model_policy_resolution_prefers_project_then_team_then_organization_and_rejects_cross_scope -v` | Backend | yes | fixed | Exit 1 after initial resolver. Another team received a project policy bound to the first team's secret; resolver now skips team-bound project policies for other teams. |
| focused model-policy tests | `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py -v` | Backend | yes | pass | Exit 0. Reported 7 passed. |
| focused model-policy lint/format | `cd apps/backend && poetry run ruff check engram/model_policy && poetry run ruff format --check engram/model_policy` | Backend | yes | pass | Exit 0. All checks passed; 10 files already formatted. |
| adjacent access/context/memory/inspection tests | `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py engram/access/access_scope_tests.py engram/context/context_api_tests.py engram/memory/memory_feedback_tests.py engram/inspection/inspection_api_tests.py -v` | Backend | yes | pass | Exit 0. Reported 57 passed. |
| migration drift | `cd apps/backend && DJANGO_SETTINGS_MODULE=settings.test_settings poetry run python manage.py makemigrations --check --dry-run` | Backend | yes | pass | Exit 0. Reported no changes detected. |
| repository runtime contract | `python3 -m unittest tests.repository.test_backend_runtime_contract -v` | Repository Quality | yes | pass | Exit 0. Reported 15 tests OK. |
| poetry lock check | `cd apps/backend && poetry check --lock` | Backend | yes | pass | Exit 0. Reported all set. |
| repository layout | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| independent security review | Independent read-only security review plus fix verification recorded in `docs/security/reviews/2026-06-25-model-policy-secrets.md` | none | yes | pass | Initial `CHANGES_REQUIRED`; fix verification `PASS`. Resolved org-secret mutation by team-scoped `secrets:*`, disabled-secret resolver candidates, and production encryption-key fail-closed behavior. |
| Karpathy simplicity/scope re-review | Independent read-only Karpathy-style review agent | none | yes | pass | Initial `CHANGES_REQUIRED`; code findings resolved. Schema placeholder surface accepted as non-blocking for this checkpoint. |
| final Compose backend gate | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. No pending migrations, system check clean, 166 passed, Ruff clean, 111 files already formatted. |

## 2026-06-26: Provider Memory Worker

Branch: `feat/provider-memory-worker`

Final checkpoint SHA is recorded in the status report after commit.

Scope:

- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `apps/backend/engram/model_policy/services.py`
- `apps/backend/engram/model_policy/model_policy_tests.py`
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- `apps/backend/engram/core/golden_path_tests.py`
- `apps/backend/engram/celeryconfig.py`
- `apps/backend/engram/core/celery_foundation_tests.py`
- `scripts/e2e_golden_path.py`
- `docs/security/reviews/2026-06-26-provider-memory-worker.md`
- `docs/superpowers/specs/2026-06-26-provider-memory-worker-design.md`
- `docs/superpowers/plans/2026-06-26-provider-memory-worker.md`
- `docs/verification-matrix.md`

This checkpoint makes the memory worker use model-policy-resolved provider
generation before creating memory candidates. It keeps exact retrieval,
semantic retrieval, embeddings, frontend/admin UI, MCP tools, and digest
scheduling out of scope. It also applies the reference-backend Celery Redis Sentinel
result-backend pattern while preserving Engram SLA queues, confirm-publish, and
the package outbox transport.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| TDD RED focused tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py engram/model_policy/model_policy_tests.py -v` | Backend | yes | fixed | Initial RED exited 1 with 5 failures for missing provider call/provenance and missing policy fallback. Fix RED exited 1 with 3 failures for xoxb redaction and existing-candidate policy bypass. Karpathy-fix RED exited 1 with 4 failures for local candidate text, missing generated fields, and missing existing-candidate provenance update. |
| focused memory/model-policy/Celery/golden-path tests | `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py engram/model_policy/model_policy_tests.py engram/core/golden_path_tests.py engram/core/celery_foundation_tests.py -v` | Backend | yes | pass | Exit 0. Reported 41 passed. |
| full backend tests | `cd apps/backend && poetry run pytest -v` | Backend | yes | pass | Exit 0. Reported 172 passed. |
| focused lint/format | `cd apps/backend && poetry run ruff check engram/celeryconfig.py engram/core/celery_foundation_tests.py engram/memory/services.py engram/memory/memory_worker_tests.py engram/model_policy/services.py engram/model_policy/model_policy_tests.py engram/core/management/commands/engram_bootstrap_golden_path.py engram/core/golden_path_tests.py && poetry run ruff format --check engram/celeryconfig.py engram/core/celery_foundation_tests.py engram/memory/services.py engram/memory/memory_worker_tests.py engram/model_policy/services.py engram/model_policy/model_policy_tests.py engram/core/management/commands/engram_bootstrap_golden_path.py engram/core/golden_path_tests.py` | Backend | yes | pass | Exit 0. All checks passed; 8 files already formatted. |
| adjacent hook/context/memory feedback/Celery tests | `cd apps/backend && poetry run pytest engram/hooks/hook_ingest_tests.py engram/context/context_api_tests.py engram/memory/memory_feedback_tests.py engram/core/celery_foundation_tests.py engram/core/golden_path_tests.py -v` | Backend | yes | pass | Exit 0. Reported 63 passed before the Karpathy fix; covered adjacent queue/context/feedback/Celery behavior. |
| migration drift | `cd apps/backend && DJANGO_SETTINGS_MODULE=settings.test_settings poetry run python manage.py makemigrations --check --dry-run` | Backend | yes | pass | Exit 0. Reported no changes detected. |
| repository checks | `python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0. Repository unittest suite reported 31 tests OK; layout and quality scripts exited with no output. |
| Compose golden path E2E | `python3 scripts/e2e_golden_path.py` | Backend | yes | fixed | Exit 1 after provider-generated titles landed because the E2E still searched by the old observation title. Updated the script to find memory by source raw event and assert provider-generated title/body; rerun exited 0. |
| independent security review | Independent read-only security review and re-review recorded in `docs/security/reviews/2026-06-26-provider-memory-worker.md` | none | yes | pass | Initial review PASS; re-review after generated provider output PASS. Residual non-blocking hardening note: add DB uniqueness before real provider network side effects. |
| Karpathy simplicity/scope review | Independent read-only Karpathy-style review agent plus re-review | none | yes | pass | Initial `CHANGES_REQUIRED`; fixed provider-generated title/body consumption and existing-candidate provenance update. Re-review `PASS_CODE`. |
| final Compose backend gate | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. Applied migrations, system check clean, 172 passed, Ruff clean, 111 files already formatted. |

## 2026-06-26: Semantic Retrieval Foundation

Branch: `feat/semantic-retrieval-foundation`

Scope:

- `apps/backend/engram/core/models.py` (`RetrievalDocument.embedding_vector`)
- `apps/backend/engram/core/migrations/0004_retrievaldocument_embedding_vector.py`
- `apps/backend/engram/model_policy/services.py` (`EMBEDDING_DIMENSION`,
  `EmbeddingCallInput`, `EmbeddingCallResult`, `_embedding_grams`,
  `generated_embedding`, `FakeProviderGateway.embed`)
- `apps/backend/engram/context/services.py` (`SEMANTIC_MIN_SIMILARITY`,
  `cosine_similarity`, `IndexMemoryVersion._embed_document`,
  `BuildContextBundle._rank_matches`/`_semantic_matches`/
  `_resolve_query_embedding`, `_audit_retrieval`)
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- `apps/backend/engram/core/core_models_tests.py`
- `apps/backend/engram/model_policy/model_policy_tests.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `apps/backend/engram/context/context_api_tests.py`
- `apps/backend/engram/core/golden_path_tests.py`
- `docs/superpowers/specs/2026-06-26-semantic-retrieval-foundation-design.md`
- `docs/superpowers/plans/2026-06-26-semantic-retrieval-foundation.md`
- `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md`

This checkpoint lifts the `claude-mem` parity-map semantic-retrieval deferral
(the first exact CLI/hooks/API E2E loop is green) and adds the first working
hybrid retrieval path: a deterministic character 3-gram embeddings provider
adapter, embedding-vector persistence on `RetrievalDocument`, and a cosine
semantic fallback inside `BuildContextBundle` that fires only when exact
matching returns fewer items than the requested limit. Exact matching stays
authoritative. It does not add pgvector, real OpenAI network calls, a new
HTTP endpoint, prompt-submit injection, or backfill.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| focused RED model test | `cd apps/backend && poetry run pytest engram/core/core_models_tests.py::test_retrieval_document_defaults_to_empty_embedding_vector -v` | Backend | yes | fixed | Exit 1 before the field existed; passed after `embedding_vector` JSONField + migration `0004`. |
| focused RED embeddings adapter | `docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps -e ENGRAM_DATABASE_URL=sqlite:///:memory: -v "$PWD/apps/backend:/srv/app" api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/model_policy/model_policy_tests.py -v"` | Backend | yes | fixed | Exit 1 before `FakeProviderGateway.embed`/`generated_embedding`; initial norm assertion (`round(norm,6)==1.0`) failed at `1.000001` and was relaxed to `pytest.approx(1.0, abs=1e-3)` because rounding each component to 6 decimals perturbs the norm slightly. |
| focused RED indexer lifecycle | `docker compose ... pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | fixed | Exit 1 before `IndexMemoryVersion._embed_document`; the first semantic-fallback retrieval test then failed because `Memory` was created without `team` while the embeddings policy is team-scoped, so `ResolveModelPolicy(team_id=None)` filtered `team__isnull=True` and missed it. Added `team=team` to the test memories; vector then populated. |
| focused RED retrieval fallback | `docker compose ... pytest engram/context/context_api_tests.py -v` | Backend | yes | fixed | Exit 1 before `_semantic_matches`/`_resolve_query_embedding`; body did not expose `metadata`, so assertions were switched to `ContextBundle.objects.get().metadata`. |
| audit RED | `docker compose ... pytest engram/context/context_api_tests.py::test_context_bundle_returns_semantic_fallback_when_exact_misses -v` | Backend | yes | fixed | Exit 1 after independent review found `_audit_retrieval` hard-coded `retrieval_strategy: 'exact'` and dropped `semantic_provider_call_id`; commit `e48c07e1` threads `has_semantic`/`embedding_result` into the audit and adds a regression assertion. |
| focused model-policy tests | `docker compose ... pytest engram/model_policy/model_policy_tests.py -v` | Backend | yes | pass | Exit 0. Reported 13 passed. |
| focused memory worker tests | `docker compose ... pytest engram/memory/memory_worker_tests.py -v` | Backend | yes | pass | Exit 0. Reported 26 passed including four embedding lifecycle tests. |
| focused context API tests | `docker compose ... pytest engram/context/context_api_tests.py -v` | Backend | yes | pass | Exit 0. Reported 22 passed including three semantic fallback tests. |
| focused golden path tests | `docker compose ... pytest engram/core/golden_path_tests.py -v` | Backend | yes | pass | Exit 0. Reported 2 passed. |
| full backend tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. System check clean; pytest reported 184 passed; Ruff `All checks passed!`; format reported 112 files already formatted. |
| migration apply and freshness | `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` | Backend | yes | pass | Exit 0. Applied through `core.0004_retrievaldocument_embedding_vector`; `No changes detected`. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Unchanged exact fixture stayed green: Compose started, host CLI connected, hook observation submitted, worker-created retrieval document observed, future session context returned, Compose stopped. |
| repository checks | `python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py`; `git diff --check HEAD` | Repository Quality | yes | pass | Exit 0. Repository unittest reported 31 OK; layout and quality scripts exited with no output; whitespace clean. |
| focused security review | Independent read-only security review agent (opus) recorded in `docs/security/reviews/2026-06-26-semantic-retrieval-foundation.md` | none | yes | pass | Initial `SECURITY CHANGES_REQUIRED` (I-1 audit hard-code); fixed in `e48c07e1`. Re-review `SECURITY APPROVED`: no Critical/Important remain. |
| Karpathy simplicity/scope review | Independent read-only Karpathy-style review agent (opus) | none | yes | pass | Initial `CHANGES_REQUIRED`; the substantive finding (audit hard-code) was the same as I-1 and is fixed. Duplication between `call`/`embed`, cosmetic `list(...)` conversions, and the split constants are accepted risks for the fake single-consumer gateway. |

First decisive failures fixed during the TDD loop:

- Embedding-vector model test failed before the field/migration existed.
- Embeddings adapter norm assertion failed at `1.000001` due to 6-decimal
  rounding; relaxed to `pytest.approx`.
- Semantic-fallback retrieval test failed because the test `Memory` lacked a
  team while the embeddings policy is team-scoped; `ResolveModelPolicy` then
  found no policy and left `embedding_vector` empty. Fixed by binding the test
  memory to the team.
- Context API tests asserted on `body['metadata']`, which the response contract
  does not expose; switched to `ContextBundle.objects.get().metadata`.
- Independent review found `_audit_retrieval` hard-coded the retrieval strategy
  and dropped the query-embedding provider call id; fixed in `e48c07e1`.

Security evidence:

- Embedding input is redacted before tokenization; token-shaped secrets cannot
  reach the vector, the provider call record, or logs.
- Semantic candidates come only from the authorized document set; no
  cross-organization/project/team document can enter via the semantic path.
- Query embedding is computed only when exact matches are below the requested
  limit; no eager provider call on the hot path.
- Missing embeddings policy degrades silently to exact-only; a disabled
  embeddings secret skips embedding with a structured warning log.
- The `MemoryRetrieved` audit records the real `retrieval_strategy` plus
  `semantic_provider_call_id` and `semantic_document_ids` when the fallback
  activates.

Accepted risks: deterministic character 3-gram embeddings are an interface-only
stand-in for real OpenAI embeddings; `JSONField` vector storage without
dimension validation while there is one producer at dimension 64; gateway
record-creation duplication deferred to the real-adapter slice.

## 2026-06-26: Memory Search API

Branch: `feat/memory-search-api` (stacked on `feat/semantic-retrieval-foundation`;
decision: user delegated slice selection and branch decisions, so stacked work
is explicit and documented; merge order is semantic-retrieval first, then
memory-search).

Scope:

- `apps/backend/engram/search/` (new app: `apps.py`, `serializers.py`,
  `services.py`, `views.py`, `urls.py`, `search_api_tests.py`)
- `apps/backend/engram/context/services.py` — extracted module helpers
  `score_retrieval_document` and `authorized_retrieval_documents`;
  `BuildContextBundle._score_document`/`_authorized_documents` delegate.
- `apps/backend/settings/settings.py` — registered `'engram.search'`.
- `apps/backend/settings/urls.py` — added `/v1/search/` route.
- `scripts/repository_layout.py` — required search app paths.
- `docs/superpowers/specs/2026-06-26-memory-search-api-design.md`
- `docs/security/reviews/2026-06-26-memory-search-api.md`

This checkpoint adds the missing `POST /v1/search` endpoint from
`docs/architecture.md`'s public API surface: an authorized, stateless, exact
read that returns cited ranked memory matches without persisting a context
bundle. It unblocks a future search MCP tool and `engram search` CLI. Semantic
recall is intentionally deferred for search.

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| focused RED search tests | `docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps -e ENGRAM_DATABASE_URL=sqlite:///:memory: -v "$PWD/apps/backend:/srv/app" api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/search/search_api_tests.py -v"` | Backend | yes | pass | Exit 0. Reported 6 passed: ranked cited matches, capability denial, wrong-project denial, cross-team exclusion, oversized-query rejection, missing bearer key. |
| extraction safety | `docker compose ... pytest engram/context/context_api_tests.py -v` | Backend | yes | pass | Exit 0. Reported 22 passed after extracting `score_retrieval_document` and `authorized_retrieval_documents`; behavior preserved. |
| full backend tests | `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` | Backend | yes | pass | Exit 0. System check clean; pytest reported 190 passed; Ruff `All checks passed!`; format reported 119 files already formatted. |
| migration apply and freshness | covered by the full-gate `migrate --noinput` above (no migration added) | Backend | yes | pass | Exit 0. No model change; migrations unchanged. |
| Compose golden path | `python3 scripts/e2e_golden_path.py` | Compose E2E | yes | pass | Exit 0. Unchanged exact fixture stayed green; search endpoint is not exercised by the golden path and introduced no regression. |
| repository checks | `python3 -m unittest discover -s tests -v`; `python3 scripts/repository_layout.py`; `python3 scripts/repository_quality.py`; `git diff --check HEAD` | Repository Quality | yes | pass | Exit 0. Repository unittest reported 31 OK; layout and quality scripts exited with no output; whitespace clean. |
| focused security review | Self-review recorded in `docs/security/reviews/2026-06-26-memory-search-api.md` | none | yes | pass | `SECURITY APPROVED`. Read-only reuser of proven authorization and retrieval primitives; no new write path, secret surface, provider boundary, or untrusted-content rendering. |

First decisive failures fixed during the TDD loop:

- Initial `ruff check` reported five import-ordering errors in the new search
  package; applied `ruff check --fix` and `ruff format` before amending the
  search commit.

Accepted risks: search is exact-only (semantic recall deferred); no dedicated
search audit event beyond `AccessScopeResolved`.

### 2026-06-26: Memory Search Semantic Recall (extension)

Stacked on `feat/memory-search-api`. Lifts the search-slice deferral: search now
uses hybrid retrieval identical to context. `_semantic_matches` and
`_resolve_query_embedding` were extracted to module functions
(`semantic_retrieval_matches`, `resolve_query_embedding`) and reused by both
`BuildContextBundle` and `SearchMemories`.

| Check | Local command | Status | Notes |
| --- | --- | --- | --- |
| focused search + context tests | `docker compose ... pytest engram/search/search_api_tests.py engram/context/context_api_tests.py -v` | pass | 29 passed; new `test_search_returns_semantic_match_when_exact_misses` green; context semantic tests unchanged after extraction. |
| full backend gate | `docker compose ... run --build --rm api sh -ec "poetry install ... && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` | pass | 191 passed; ruff clean; 119 files formatted. |

Security unchanged: the query-embedding provider call on the search path is
authorized under the same scope, redacts the query text, and is made only when
exact matches are below the requested limit.
