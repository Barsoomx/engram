# Core Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first Django model and migration surface for Engram's parity-loop persistence.

**Architecture:** Create one `engram.core` Django app with explicit UUID-based models for tenant scope, agent sessions, raw events, observations, observation source links, memory candidates, memories, retrieval documents, context bundle audit, audit events, and durable outbox entries. Keep API, auth, worker execution, retrieval ranking, and provider calls out of this checkpoint.

**Tech Stack:** Django 5.2, Django REST Framework, PostgreSQL target with sqlite test database, Poetry, pytest-django, Ruff.

## Global Constraints

- Work on branch `feat/parity-04-core-models`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use pytest function tests named `*_tests.py`.
- Use TDD: write failing tests before production model code.
- Do not add API endpoints, serializers, API keys, worker handlers, CLI behavior, frontend files, provider adapters, or semantic retrieval.
- Every tenant-owned model must carry `organization`; project-owned records must carry `project`.
- Agent runtime names are metadata only, not product naming or memory ownership.
- Docker Compose live checks are recorded as blocked while Docker is unavailable in this WSL distro.

---

### Task 1: Core App Contract Tests

**Files:**
- Create: `apps/backend/engram/core/core_models_tests.py`
- Modify: `apps/backend/settings/settings.py`

**Interfaces:**
- Consumes: existing Django project and pytest-django settings.
- Produces: failing tests that define the model names, scoped uniqueness rules, and outbox idempotency contract.

- [ ] **Step 1: Write failing tests for core model contracts**

Add tests that import the intended models and assert:

```python
import pytest
from django.db import IntegrityError

from engram.core.models import (
    Agent,
    AgentSession,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    OutboxEvent,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Team,
)


@pytest.mark.django_db
def test_core_scope_allows_same_external_ids_in_different_projects() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    first_project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    second_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-1')

    first_session = AgentSession.objects.create(
        organization=organization,
        project=first_project,
        team=team,
        agent=agent,
        external_session_id='session-1',
        runtime='codex',
    )
    second_session = AgentSession.objects.create(
        organization=organization,
        project=second_project,
        team=team,
        agent=agent,
        external_session_id='session-1',
        runtime='codex',
    )

    assert first_session.external_session_id == second_session.external_session_id
    assert first_session.project_id != second_session.project_id
```

Add duplicate tests for raw events, observations, retrieval documents, context bundle items, and outbox idempotency.

- [ ] **Step 2: Run tests and verify expected failure**

Run:

```bash
cd apps/backend && poetry run pytest engram/core/core_models_tests.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'engram.core'` or equivalent missing model/app error.

### Task 2: Core Models And Migrations

**Files:**
- Create: `apps/backend/engram/core/__init__.py`
- Create: `apps/backend/engram/core/apps.py`
- Create: `apps/backend/engram/core/models.py`
- Create: `apps/backend/engram/core/migrations/__init__.py`
- Create: `apps/backend/engram/core/migrations/0001_initial.py`
- Modify: `apps/backend/settings/settings.py`

**Interfaces:**
- Consumes: tests from Task 1.
- Produces: Django model classes listed in the design, an initial migration, and installed app `engram.core`.

- [ ] **Step 1: Add the core app shell**

Create `CoreConfig`:

```python
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engram.core'
```

Add `'engram.core'` to `INSTALLED_APPS`.

- [ ] **Step 2: Implement model enums and base timestamp fields**

In `models.py`, add UUID primary keys and timestamp helpers. Use Django
`TextChoices` for `Runtime`, `SessionStatus`, `VisibilityScope`,
`MemoryStatus`, `CandidateStatus`, `ContextBundleStatus`,
`AuditResult`, and `OutboxStatus`.

- [ ] **Step 3: Implement tenant, project, agent, and session models**

Implement `Organization`, `Team`, `Project`, `ProjectTeam`, `Agent`, and
`AgentSession` with scoped uniqueness constraints:

- organization slug unique;
- team slug unique per organization;
- project slug unique per organization;
- project/team pair unique;
- agent runtime/external id unique per organization;
- session external id unique per organization/project.

- [ ] **Step 4: Implement event and observation models**

Implement `RawEventEnvelope`, `Observation`, and `ObservationSource` with:

- resolved organization/project/team/agent/session scope;
- event ids and idempotency keys;
- JSON payload/source fields;
- content hash;
- schema version;
- source/provenance links for citation and migration compatibility;
- scoped duplicate prevention.

- [ ] **Step 5: Implement memory, retrieval, context, audit, and outbox models**

Implement `MemoryCandidate`, `Memory`, `MemoryVersion`, `RetrievalDocument`,
`ContextBundle`, `ContextBundleItem`, `AuditEvent`, and `OutboxEvent` with the
constraints described in the spec.

- [ ] **Step 6: Generate and inspect migration**

Run:

```bash
cd apps/backend && poetry run python manage.py makemigrations core
```

Expected: creates `engram/core/migrations/0001_initial.py`.

- [ ] **Step 7: Run the focused model tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/core/core_models_tests.py -v
```

Expected: all focused model tests pass.

- [ ] **Step 8: Commit**

Commit:

```bash
git add apps/backend/settings/settings.py apps/backend/engram/core
git commit -m "feat: add core parity models"
```

### Task 3: Repository And Migration Gates

**Files:**
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `.github/workflows/backend.yml`
- Modify: `docs/verification-matrix.md`

**Interfaces:**
- Consumes: core app from Task 2.
- Produces: repository-level checks proving the core model files, migration, and migration commands remain part of CI.

- [ ] **Step 1: Write failing repository tests for core model paths and backend workflow**

Extend repository tests to require:

- `apps/backend/engram/core/models.py`;
- `apps/backend/engram/core/migrations/0001_initial.py`;
- backend workflow command `poetry run python manage.py makemigrations --check --dry-run`;
- backend workflow command `poetry run python manage.py migrate --noinput --settings=settings.test_settings`.

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract tests.repository.test_backend_workflow -v
```

Expected before implementation: fail on missing layout/workflow requirements.

- [ ] **Step 2: Add layout and workflow requirements**

Update `scripts/repository_layout.py` and `.github/workflows/backend.yml` to include the core model paths and migration checks.

- [ ] **Step 3: Run repository tests**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract tests.repository.test_backend_workflow -v
```

Expected: pass.

- [ ] **Step 4: Run backend migration checks**

Run:

```bash
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run
cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
```

Expected: both commands exit 0.

- [ ] **Step 5: Update verification matrix**

Add a `2026-06-25: Core Models And Migrations` section with branch, scope,
commands, exit codes, and first decisive TDD failures.

- [ ] **Step 6: Commit**

Commit:

```bash
git add .github/workflows/backend.yml scripts/repository_layout.py tests/repository/test_backend_runtime_contract.py tests/repository/test_backend_workflow.py docs/verification-matrix.md
git commit -m "chore: require core model migration gates"
```

### Task 4: Final Verification And Merge

**Files:**
- No new files unless verification reveals a defect.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: verified checkpoint and fast-forward merge to local `master` if all non-Docker checks pass.

- [ ] **Step 1: Run full local verification**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run
cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
cd apps/backend && poetry check
git diff --check HEAD
docker compose version
```

Expected: all non-Docker commands exit 0. `docker compose version` exits 1 until Docker is available in this WSL distro.

- [ ] **Step 2: Run simplicity/self-review**

Review the diff against:

- LLM-agnostic;
- memory-first;
- context-not-search;
- local-first/server-only runtime;
- agent-native;
- no North Star expansion beyond the parity persistence surface.

- [ ] **Step 3: Merge locally if verified**

If checks pass and only `.gitignore` remains dirty:

```bash
git switch master
git merge --ff-only feat/parity-04-core-models
```

Then re-run the non-Docker verification commands on `master`.

## Self-Review

- Spec coverage: every model required by the design has an implementation task.
- Placeholder scan: no unfinished markers or vague implementation steps remain.
- Type consistency: model names in tests, implementation, and repository gates match, including `ObservationSource`.
- Scope check: auth, API, workers, CLI, frontend, MCP, and provider adapters remain deferred.
