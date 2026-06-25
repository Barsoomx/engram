# Verification Matrix

This matrix records local commands, CI equivalents, status, and first decisive
failures for each completed Engram slice.

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
