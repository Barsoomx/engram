# Auth Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add backend-only identities, role/capability grants, API keys, and an
effective authorization scope resolver for the next parity gate.

**Architecture:** Create one `engram.access` Django app that depends on
`engram.core`. Store explicit identities, roles, capabilities, grants, API-key
hashes, and key capability restrictions. Add one domain service,
`ResolveApiKeyScope`, that authenticates a raw key, intersects owner/key
capabilities, enforces project/team binding, returns scope filters, and writes
audit events.

**Tech Stack:** Django 5.2, pytest-django, sqlite test database, PostgreSQL
target, Poetry, Ruff.

## Global Constraints

- Work on branch `feat/parity-05-auth-scope`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use pytest function tests named `*_tests.py`.
- Use TDD: write failing tests before production model/service code.
- Do not add hook endpoints, DRF serializers, API auth classes, CLI behavior,
  frontend files, provider-secret code, retrieval ranking, or worker handlers.
- Never persist raw API keys in models, audit metadata, logs, or tests.
- Docker Compose live checks are recorded as blocked while Docker is unavailable
  in this WSL distro.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-auth-scope-design.md`
- Create: `docs/superpowers/plans/2026-06-25-auth-scope.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/rbac-and-scopes.md`,
  `docs/backend-contracts.md`, `docs/agent-integrations.md`,
  `docs/client-installation.md`.
- Produces: committed design and implementation plan for the access slice.

- [ ] **Step 1: Write the spec and implementation plan**

Record model boundaries, resolver behavior, explicit deferrals, tests, and
verification commands.

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
git add docs/superpowers/specs/2026-06-25-auth-scope-design.md docs/superpowers/plans/2026-06-25-auth-scope.md
git commit -m "chore: add auth scope plan"
```

### Task 2: Failing Access Contract Tests

**Files:**

- Create: `apps/backend/engram/access/access_scope_tests.py`

**Interfaces:**

- Consumes: existing core tenant/project/team/audit models.
- Produces: failing tests for the intended access app, models, seed data, and
  resolver service.

- [ ] **Step 1: Add focused tests**

Tests must cover:

- seeded default capabilities and roles;
- API key raw-token hashing and no raw storage;
- duplicate API key hashes;
- owner/key capability intersection;
- project-scoped allow;
- same-organization cross-project deny;
- cross-organization requested project deny;
- inactive/revoked/expired/owner-inactive deny;
- success and denial audit events without raw key leakage;
- cross-scope FK rejection through normal `objects.create()` paths.

- [ ] **Step 2: Run focused tests and verify the first failure**

Run:

```bash
cd apps/backend && poetry run pytest engram/access/access_scope_tests.py -v
```

Expected before implementation: fail with missing `engram.access` import or
missing model/service.

### Task 3: Access Models And Seed Migrations

**Files:**

- Create: `apps/backend/engram/access/__init__.py`
- Create: `apps/backend/engram/access/apps.py`
- Create: `apps/backend/engram/access/models.py`
- Create: `apps/backend/engram/access/migrations/__init__.py`
- Create: `apps/backend/engram/access/migrations/0001_initial.py`
- Create: `apps/backend/engram/access/migrations/0002_seed_default_roles.py`
- Modify: `apps/backend/settings/settings.py`

**Interfaces:**

- Consumes: tests from Task 2.
- Produces: installed access app and migration-backed RBAC/API-key tables.

- [ ] **Step 1: Add the access app shell**

Create `AccessConfig` and add `'engram.access'` to `INSTALLED_APPS`.

- [ ] **Step 2: Implement access models**

Implement the models listed in the spec with scoped uniqueness constraints and
save-time cross-scope validation.

- [ ] **Step 3: Generate the initial migration**

Run:

```bash
cd apps/backend && poetry run python manage.py makemigrations access
```

Expected: creates `0001_initial.py`.

- [ ] **Step 4: Add seed migration**

Create an empty migration and add reversible seed data for V1 capabilities and
default role capability links.

- [ ] **Step 5: Run focused tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/access/access_scope_tests.py -v
```

Expected: model/seed-data tests pass; resolver tests still fail until Task 4.

### Task 4: Effective Scope Resolver

**Files:**

- Create: `apps/backend/engram/access/services.py`
- Modify: `apps/backend/engram/access/access_scope_tests.py`

**Interfaces:**

- Consumes: access models and core `AuditEvent`.
- Produces: `ResolveApiKeyScope.execute()` and DTO/error types for future hook
  ingest and retrieval services.

- [ ] **Step 1: Implement hashing helpers and DTOs**

Expose helpers for prefix, HMAC-SHA256 hash, fingerprint, and safe API-key
creation in tests/services.

- [ ] **Step 2: Implement `ResolveApiKeyScope.execute()`**

Resolve the key, owner grants, API-key restrictions, project/team filters, and
required capability. Audit existing-key success and denial decisions.

- [ ] **Step 3: Run focused tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/access/access_scope_tests.py -v
```

Expected: all focused access tests pass.

### Task 5: Repository Gates And Verification Matrix

**Files:**

- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: access app, migrations, service, tests.
- Produces: repository-level gates requiring access files and recorded command
  evidence.

- [ ] **Step 1: Add repository layout requirements**

Require access app model/service/test and both migrations.

- [ ] **Step 2: Run repository tests**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract -v
```

Expected: pass.

- [ ] **Step 3: Update verification matrix**

Add the `2026-06-25: Auth Scope And API Keys` checkpoint with branch, scope,
commands, exit codes, and first decisive TDD failures.

### Task 6: Review And Final Verification

**Files:** no new owned files unless review findings require fixes.

**Interfaces:**

- Consumes: completed access implementation.
- Produces: fixed/refuted review findings and a coherent checkpoint commit.

- [ ] **Step 1: Run local simplicity/security review**

Check that the implementation stays capability-and-filter based, never stores
raw keys, and denies cross-project/org requests before future retrieval can run.

- [ ] **Step 2: Run full verification**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
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
git add apps/backend/settings/settings.py apps/backend/engram/access scripts/repository_layout.py tests/repository/test_backend_runtime_contract.py docs/verification-matrix.md
git commit -m "feat: add auth scope resolution"
```
