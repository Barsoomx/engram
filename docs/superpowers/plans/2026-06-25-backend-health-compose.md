# Backend Health And Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first Django backend runtime shell with health endpoints, Poetry tooling, Docker image, Compose services, and backend CI.

**Architecture:** `apps/backend` is the Django backend root. Health endpoints are tested behavior; Docker and Compose are tested repository contracts because Docker is not available in this WSL environment.

**Tech Stack:** Python 3.12, Poetry 1.8, Django 5.2, Django REST Framework, Celery, Redis, PostgreSQL, Gunicorn, pytest, pytest-django, Ruff, GitHub Actions.

## Global Constraints

- Keep this slice limited to backend runtime shell, health endpoints, Dockerfile, Compose, and CI.
- Do not add hook ingest, API keys, tenancy/RBAC, memory, retrieval, provider secret, model policy, audit, or outbox tables.
- Do not reintroduce local memory workers, local SQLite/Chroma authority, or local summarization services.
- Backend commands are rooted at `apps/backend`.
- Use Poetry for Python dependencies.
- Use Python 3.12.
- Use single quotes in Python code.
- Keep existing `.gitignore` local modification unstaged.
- Commit messages must use an allowed prefix from local AGENTS rules.
- Docker is not available locally; record live Compose verification as not run unless the environment changes.

---

## Reference Gates Copied

- From the outbox reference: HTTP healthcheck in Compose, Postgres `pg_isready`,
  `docker compose up -d --wait` acceptance shape, pytest/Ruff CI lanes.
- From the private backend reference: backend-root Django structure, Poetry
  dependency management, `settings.settings` plus `settings.test_settings`,
  `*_tests.py` pytest discovery, and `/-/healthz|readyz|startup/` endpoint
  shape.

## File Structure

- Create `apps/backend/manage.py`: Django management entrypoint.
- Create `apps/backend/pyproject.toml`: Poetry dependencies and tool config.
- Create `apps/backend/pytest.ini`: pytest-django config.
- Create `apps/backend/settings/__init__.py`: settings package marker.
- Create `apps/backend/settings/settings.py`: runtime settings.
- Create `apps/backend/settings/test_settings.py`: test overrides.
- Create `apps/backend/settings/urls.py`: root URL routing.
- Create `apps/backend/settings/wsgi.py`: WSGI entrypoint.
- Create `apps/backend/settings/asgi.py`: ASGI entrypoint.
- Create `apps/backend/engram/__init__.py`: app package marker.
- Create `apps/backend/engram/celery_app.py`: Celery app.
- Create `apps/backend/engram/health/__init__.py`: health package marker.
- Create `apps/backend/engram/health/apps.py`: app config.
- Create `apps/backend/engram/health/views.py`: health views.
- Create `apps/backend/engram/health/urls.py`: health routes.
- Create `apps/backend/engram/health/health_tests.py`: health tests.
- Create `apps/backend/Dockerfile`: backend image.
- Create `deploy/compose/docker-compose.yml`: local runtime services.
- Create `deploy/compose/.env.example`: local non-secret env values.
- Create `tests/repository/test_backend_runtime_contract.py`: Docker/Compose/static contract tests.
- Create `tests/repository/test_backend_workflow.py`: backend CI workflow tests.
- Create `.github/workflows/backend.yml`: backend CI.
- Modify `scripts/repository_layout.py`: require backend runtime files.
- Modify `docs/verification-matrix.md`: record checkpoint commands.

## Task 1: Backend Tooling Contract

**Files:**

- Create: `tests/repository/test_backend_runtime_contract.py`
- Modify: `scripts/repository_layout.py`

**Interfaces:**

- Consumes: repository root.
- Produces: repository layout requirements for backend runtime files.

- [ ] **Step 1: Write failing repository layout test**

```python
from pathlib import Path
import unittest

from scripts.repository_layout import REQUIRED_PATHS, missing_paths


ROOT = Path(__file__).resolve().parents[2]


class BackendRuntimeLayoutTests(unittest.TestCase):
    expected = {
        'apps/backend/manage.py',
        'apps/backend/pyproject.toml',
        'apps/backend/pytest.ini',
        'apps/backend/settings/settings.py',
        'apps/backend/settings/test_settings.py',
        'apps/backend/settings/urls.py',
        'apps/backend/engram/health/views.py',
        'apps/backend/Dockerfile',
        'deploy/compose/docker-compose.yml',
        'deploy/compose/.env.example',
    }

    def test_backend_runtime_paths_are_layout_requirements(self) -> None:
        self.assertTrue(self.expected.issubset(set(REQUIRED_PATHS)))

    def test_backend_runtime_paths_exist(self) -> None:
        missing = set(missing_paths(ROOT))

        self.assertFalse(self.expected & missing)
```

- [ ] **Step 2: Run and verify red**

Run: `python3 -m unittest tests.repository.test_backend_runtime_contract -v`

Expected: fail because backend runtime files are not registered layout requirements yet.

- [ ] **Step 3: Extend `scripts/repository_layout.py`**

Add the expected backend paths to `REQUIRED_PATHS`.

- [ ] **Step 4: Run and keep red for missing files**

Run: `python3 -m unittest tests.repository.test_backend_runtime_contract -v`

Expected: fail because files are still missing, not because `REQUIRED_PATHS` is incomplete.

## Task 2: Django Health Endpoints

**Files:**

- Create: `apps/backend/manage.py`
- Create: `apps/backend/pyproject.toml`
- Create: `apps/backend/pytest.ini`
- Create: `apps/backend/settings/__init__.py`
- Create: `apps/backend/settings/settings.py`
- Create: `apps/backend/settings/test_settings.py`
- Create: `apps/backend/settings/urls.py`
- Create: `apps/backend/settings/wsgi.py`
- Create: `apps/backend/settings/asgi.py`
- Create: `apps/backend/engram/__init__.py`
- Create: `apps/backend/engram/health/__init__.py`
- Create: `apps/backend/engram/health/apps.py`
- Create: `apps/backend/engram/health/views.py`
- Create: `apps/backend/engram/health/urls.py`
- Create: `apps/backend/engram/health/health_tests.py`

**Interfaces:**

- Produces: `GET /-/healthz/ -> 200`.
- Produces: `GET /-/readyz/ -> 200 when database is reachable`.
- Produces: `GET /-/startup/ -> 200 when database is reachable`.

- [ ] **Step 1: Create backend project config files without health implementation**

Create `pyproject.toml`, `pytest.ini`, settings, URL package markers, and
`manage.py`. Do not add `engram.health` yet.

- [ ] **Step 2: Run backend install**

Run from `apps/backend`: `poetry install --no-interaction`

Expected: dependencies install and `poetry.lock` is created.

- [ ] **Step 3: Write failing health tests**

```python
import pytest
from django.test import Client


def test_healthz_returns_process_status(client: Client) -> None:
    response = client.get('/-/healthz/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'process': 'ok'},
    }


@pytest.mark.django_db
def test_readyz_checks_database(client: Client) -> None:
    response = client.get('/-/readyz/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'database': 'ok'},
    }


@pytest.mark.django_db
def test_startup_checks_database(client: Client) -> None:
    response = client.get('/-/startup/')

    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'checks': {'database': 'ok'},
    }
```

- [ ] **Step 4: Run and verify red**

Run from `apps/backend`: `poetry run pytest engram/health/health_tests.py -v`

Expected: fail with route or app import errors because health implementation is missing.

- [ ] **Step 5: Implement health app and routes**

Add `engram.health` app config, views, URLs, and include the health URLs under
`/-/` in `settings/urls.py`.

- [ ] **Step 6: Run backend tests and quality checks**

Run from `apps/backend`:

```bash
poetry run pytest -v
poetry run ruff check .
poetry run ruff format --check .
```

Expected: all exit `0`.

- [ ] **Step 7: Commit backend health**

```bash
git add apps/backend
git commit -m "feat: add backend health endpoints"
```

## Task 3: Docker And Compose Contracts

**Files:**

- Create: `apps/backend/Dockerfile`
- Create: `deploy/compose/docker-compose.yml`
- Create: `deploy/compose/.env.example`
- Modify: `tests/repository/test_backend_runtime_contract.py`

**Interfaces:**

- Produces: backend image build definition.
- Produces: Compose services `api`, `worker`, `postgres`, `redis`.
- Produces: API healthcheck against `/-/readyz/`.

- [ ] **Step 1: Add failing static Compose tests**

Add tests that read `deploy/compose/docker-compose.yml` and assert service names,
depends-on health conditions, `/-/readyz/`, Postgres, Redis, and the Celery
worker command are present.

- [ ] **Step 2: Run and verify red**

Run: `python3 -m unittest tests.repository.test_backend_runtime_contract -v`

Expected: fail because Dockerfile and Compose files are missing.

- [ ] **Step 3: Add Dockerfile and Compose files**

Use a single backend image for API and worker. API command runs migrations then
Gunicorn. Worker command runs Celery using `engram.celery_app`.

- [ ] **Step 4: Run static checks**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract -v
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
```

Expected: all exit `0`.

- [ ] **Step 5: Record Docker availability**

Run: `docker compose version`

Expected in this WSL environment: command fails because Docker is unavailable.
Record that first decisive failure in `docs/verification-matrix.md`.

- [ ] **Step 6: Commit Docker and Compose contracts**

```bash
git add apps/backend/Dockerfile deploy/compose tests/repository/test_backend_runtime_contract.py scripts/repository_layout.py
git commit -m "chore: add backend compose runtime"
```

## Task 4: Backend CI Workflow

**Files:**

- Create: `tests/repository/test_backend_workflow.py`
- Create: `.github/workflows/backend.yml`

**Interfaces:**

- Produces: GitHub workflow that runs backend Poetry install, Ruff, pytest, and root repository checks.

- [ ] **Step 1: Write failing backend workflow tests**

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BackendWorkflowTests(unittest.TestCase):
    def test_backend_workflow_runs_required_commands(self) -> None:
        workflow = (ROOT / '.github/workflows/backend.yml').read_text(encoding='utf-8')

        self.assertIn('working-directory: apps/backend', workflow)
        self.assertIn('poetry install --no-interaction', workflow)
        self.assertIn('poetry run ruff check .', workflow)
        self.assertIn('poetry run ruff format --check .', workflow)
        self.assertIn('poetry run pytest -v', workflow)
        self.assertIn('python3 scripts/repository_layout.py', workflow)
        self.assertIn('python3 scripts/repository_quality.py', workflow)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run and verify red**

Run: `python3 -m unittest tests.repository.test_backend_workflow -v`

Expected: fail because workflow file is missing.

- [ ] **Step 3: Add backend workflow**

Create `.github/workflows/backend.yml` with checkout, Python 3.12 setup, Poetry
install, backend Ruff checks, backend pytest, and root repository checks.

- [ ] **Step 4: Run workflow tests and root tests**

Run:

```bash
python3 -m unittest tests.repository.test_backend_workflow -v
python3 -m unittest discover -s tests -v
```

Expected: all exit `0`.

- [ ] **Step 5: Commit workflow**

```bash
git add .github/workflows/backend.yml tests/repository/test_backend_workflow.py
git commit -m "chore: add backend ci workflow"
```

## Task 5: Verification Matrix

**Files:**

- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: command outcomes from Tasks 1-4.
- Produces: auditable record for this backend checkpoint.

- [ ] **Step 1: Run final verification**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry check
docker compose version
git diff --check HEAD
```

Expected: all Python/Poetry checks exit `0`; Docker command fails locally if
Docker remains unavailable.

- [ ] **Step 2: Add checkpoint entry**

Record branch, scope, commands, exit codes, and the Docker limitation.

- [ ] **Step 3: Commit verification record**

```bash
git add docs/verification-matrix.md
git commit -m "chore: record backend health verification"
```

## Self-Review

- Spec coverage: tasks cover Django backend shell, health endpoints, Poetry,
  Dockerfile, Compose, backend CI, and verification evidence.
- Type consistency: paths and command names match across tasks.
- Scope: hook ingest, RBAC, outbox, memory, retrieval, provider secrets, and
  context bundles are excluded from this checkpoint.
