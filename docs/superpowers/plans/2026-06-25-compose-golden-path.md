# Compose Golden Path Implementation Plan

Supersession note (2026-06-25): this historical plan described a manual Engram
outbox-processing command. The live contract now uses
`django-celery-outbox package transport`; hook ingest queues
`engram.memory.process_observation_recorded` with the observation id through
the Celery task `.delay(...)` call. The Compose `relay` service is the
package-owned `python manage.py celery_outbox_relay`, not Engram domain outbox
processing.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the first Docker Compose golden path from CLI connect and hook
observation to future session context injection.

**Architecture:** Add minimal backend management commands for E2E bootstrap,
outbox processing, and explicit candidate promotion; add thin CLI hook commands
that call existing server APIs; add a Python E2E orchestrator and GitHub
Actions workflow.

**Tech Stack:** Python 3.12, Django management commands, Django REST Framework
APIs already in place, stdlib CLI, Docker Compose, GitHub Actions.

## Global Constraints

- Work on branch `feat/parity-12-golden-path-e2e`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use TDD: write failing tests before production code.
- Keep the CLI thin: no local workers, local memory DB, embeddings, cached
  context bundles, provider secrets, or durable event queue.
- The E2E fixture must use exact retrieval only and must not add semantic
  retrieval, provider calls, frontend, MCP, native hook installation, or package
  publishing.
- The golden path must include an explicit memory approval/promotion step before
  context injection.
- Raw API keys and bearer tokens must not appear in normal output, error output,
  public config, hook manifests, management command JSON, context responses, or
  logs produced by tests.
- Docker Compose live checks are recorded as blocked locally while Docker is
  unavailable in this WSL distro; the GitHub Actions Compose E2E workflow owns
  live Docker proof.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-compose-golden-path-design.md`
- Create: `docs/superpowers/plans/2026-06-25-compose-golden-path.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/north-star.md`, `docs/v1-scope.md`,
  `docs/client-installation.md`, `docs/agent-integrations.md`,
  `docs/backend-contracts.md`, `docs/parity/claude-mem-parity-map.md`,
  existing CLI lifecycle code, hook ingest API, memory worker service, and
  context service.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write design and plan**

Document the golden fixture, commands, CLI hook behavior, E2E script, workflow,
explicit deferrals, and verification.

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
git add docs/superpowers/specs/2026-06-25-compose-golden-path-design.md docs/superpowers/plans/2026-06-25-compose-golden-path.md
git commit -m "chore: add compose golden path plan"
```

### Task 2: CLI Hook Adapter Tests And Implementation

**Files:**

- Modify: `packages/cli/engram_cli/cli_lifecycle_tests.py`
- Modify: `packages/cli/engram_cli/main.py`
- Modify: `packages/cli/engram_cli/commands.py`
- Modify: `packages/cli/engram_cli/http.py`
- Modify: `packages/cli/README.md`

**Interfaces:**

- Consumes: existing CLI local config and credential helpers.
- Produces:
  - `engram hook post-tool-use`;
  - `engram hook session-start`.

- [ ] **Step 1: Add failing CLI hook tests**

Add tests that:

- create connected local state through `connect`;
- call `main.main(['hook', 'post-tool-use', '--config-dir', path], stdin=StringIO(...))`;
- assert the transport receives `POST /v1/hooks/post-tool-use` with bearer auth,
  project/team/runtime from config, `event_type='post_tool_use'`,
  `payload_schema_version='v1'`, generated `content_hash`, and no raw key in
  stdout/stderr;
- call `main.main(['hook', 'session-start', '--config-dir', path], stdin=StringIO(...))`;
- assert the transport receives `POST /v1/context/session-start` with query,
  file paths, project/team/runtime from config, and no raw key in output;
- assert invalid JSON exits `1` with `invalid_response`;
- assert missing config exits `1` with `missing_config`.

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

Expected before implementation: failures for unsupported `stdin` argument or
missing hook command.

- [ ] **Step 2: Implement stdin-aware CLI dispatch**

Add optional `stdin: TextIO | None = None` to `main.main(...)` and pass it to
hook command handlers. Preserve existing command behavior.

- [ ] **Step 3: Implement hook HTTP helpers**

Add `post_json(...)` helper or endpoint-specific wrappers that call:

- `${server_url}/v1/hooks/post-tool-use`;
- `${server_url}/v1/context/session-start`.

Reuse existing transport and JSON parsing behavior.

- [ ] **Step 4: Implement hook command runner**

Implement `run_hook(args, stdin, stdout, stderr, transport)` that:

- loads config and credentials;
- validates selected runtime is configured;
- parses stdin JSON object;
- builds request payload with generated ids and content hash when omitted;
- calls the selected endpoint;
- prints response JSON;
- redacts active API key in errors.

- [ ] **Step 5: Update README and run tests**

Document:

```bash
python -m engram_cli hook post-tool-use < hook.json
python -m engram_cli hook session-start < context.json
```

Run CLI tests until they pass, then commit:

```bash
git add packages/cli
git commit -m "feat: add cli hook adapters"
```

### Task 3: Backend Golden Path Commands

**Files:**

- Create: `apps/backend/engram/core/management/__init__.py`
- Create: `apps/backend/engram/core/management/commands/__init__.py`
- Create: `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- Create: `apps/backend/engram/core/golden_path_tests.py`
- Create: `apps/backend/engram/memory/management/__init__.py`
- Create: `apps/backend/engram/memory/management/commands/__init__.py`
- Create: `apps/backend/engram/memory/management/commands/engram_process_observation_outbox.py`
- Create: `apps/backend/engram/memory/management/commands/engram_promote_memory_candidate.py`
- Modify: `apps/backend/engram/memory/services.py`
- Modify: `apps/backend/engram/memory/memory_worker_tests.py`

**Interfaces:**

- Consumes: access models, core models, `ProcessObservationRecorded`,
  `IndexMemoryVersion`.
- Produces:
  - idempotent bootstrap command;
  - outbox processing command;
  - `PromoteMemoryCandidate` service and command.

- [ ] **Step 1: Add failing backend command tests**

Add pytest coverage proving:

- bootstrap command creates organization/team/project/link/identity/API key
  scope and does not print the raw API key;
- running bootstrap twice with the same raw key is idempotent;
- outbox command processes a pending `ObservationRecorded` row and reports
  processed count;
- promotion service creates approved memory, version, retrieval document, marks
  candidate promoted, and is idempotent;
- promotion command can promote the latest proposed candidate for a project.

Run:

```bash
cd apps/backend && poetry run pytest engram/core/golden_path_tests.py engram/memory/memory_worker_tests.py -v
```

Expected before implementation: failures for missing commands/services.

- [ ] **Step 2: Implement bootstrap command**

Use existing `api_key_prefix`, `hash_api_key`, and `api_key_fingerprint`.
Create or reuse the deterministic E2E records and capabilities. Output JSON
with ids and key fingerprint only.

- [ ] **Step 3: Implement promotion service**

Add dataclasses:

```python
class PromoteMemoryCandidateInput:
    candidate_id: uuid.UUID
```

and:

```python
class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> PromoteMemoryCandidateResult:
```

The service creates/reuses approved `Memory`, version `1`, updates candidate
state, and indexes the version.

- [ ] **Step 4: Implement outbox and promotion commands**

`engram_process_observation_outbox --limit N --json` processes pending
`ObservationRecorded` rows through `ProcessObservationRecorded`.

`engram_promote_memory_candidate --candidate-id ID --json` or
`--project-id ID --latest --json` promotes one candidate.

- [ ] **Step 5: Run backend focused tests and commit**

Run:

```bash
cd apps/backend && poetry run pytest engram/core/golden_path_tests.py engram/memory/memory_worker_tests.py -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
```

Commit:

```bash
git add apps/backend/engram/core apps/backend/engram/memory
git commit -m "feat: add golden path backend commands"
```

### Task 4: Compose E2E Script And CI Workflow

**Files:**

- Create: `scripts/e2e_golden_path.py`
- Create: `.github/workflows/compose-e2e.yml`
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: CLI hook commands and backend golden path commands.
- Produces: a live Compose E2E workflow.

- [ ] **Step 1: Add failing repository gate tests**

Require:

- `scripts/e2e_golden_path.py`;
- `.github/workflows/compose-e2e.yml`;
- backend management command files;
- workflow text containing `python3 scripts/e2e_golden_path.py`.

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected before layout/workflow updates: failures for missing required paths or
workflow text.

- [ ] **Step 2: Implement E2E script**

The script must:

- copy `.env.example` to `.env` when `.env` is missing;
- run `docker compose up -d --build --wait`;
- run bootstrap in the API container;
- run host CLI `connect`;
- run host CLI `hook post-tool-use`;
- run outbox processing in the worker container;
- run candidate promotion in the worker container;
- run host CLI `hook session-start`;
- assert response contains the promoted memory title and citation;
- run `docker compose down -v` in `finally`.

- [ ] **Step 3: Add Compose E2E workflow**

Add `.github/workflows/compose-e2e.yml` with:

```yaml
name: Compose E2E
on:
  pull_request:
    branches: [master]
  push:
    branches: [master]
jobs:
  compose-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python3 scripts/e2e_golden_path.py
```

- [ ] **Step 4: Update verification matrix**

Append the Compose golden path section with local command statuses and CI
workflow evidence. Locally, `docker compose version` remains blocked.

- [ ] **Step 5: Run non-Docker local verification and commit**

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
git diff --check HEAD
docker compose version
```

Expected: non-Docker commands exit 0. Docker exits 1 in this WSL distro.

Commit:

```bash
git add .github/workflows/compose-e2e.yml scripts/e2e_golden_path.py scripts/repository_layout.py tests docs/verification-matrix.md
git commit -m "test: add compose golden path e2e"
```

### Task 5: Final Verification And PR

**Files:**

- Verify only.

**Interfaces:**

- Consumes all prior tasks.
- Produces PR and CI evidence.

- [ ] **Step 1: Run full local verification**

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
python3 -m compileall packages/cli/engram_cli
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

- [ ] **Step 2: Review**

Run an independent read-only review focused on:

- raw API key exposure;
- whether the E2E really exercises API, database, worker service, promotion,
  retrieval, and context response;
- idempotency of bootstrap and promotion;
- cleanup of Compose resources.

- [ ] **Step 3: Open PR**

Push the branch and open a PR. The PR body must record local verification,
Docker local block, and the fact that live Compose proof is owned by the
Compose E2E workflow.

- [ ] **Step 4: Merge only after all CI is green**

Required checks for this checkpoint:

- Backend;
- Repository Quality;
- Compose E2E.
