# Hook Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated hook dry-run and durable observation ingest
endpoints for the Engram parity loop.

**Architecture:** Create one `engram.hooks` Django app with DRF serializers,
views, URLs, and domain services. Views extract the bearer API key and delegate
all authorization and write behavior to services. Ingest writes raw event,
observation, source, and outbox records transactionally and handles duplicate
client submissions idempotently.

**Tech Stack:** Django 5.2, Django REST Framework, PostgreSQL target with sqlite
test database, Poetry, pytest-django, Ruff.

## Global Constraints

- Work on branch `feat/parity-06-hook-ingest`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use pytest function tests named `*_tests.py`.
- Use TDD: write failing tests before production code.
- Do not add CLI behavior, frontend files, plugin packages, provider calls,
  retrieval, context bundle APIs, worker handlers, or semantic search.
- Every accepted hook event writes raw event, normalized observation, source,
  and outbox rows in one database transaction.
- Duplicate hook submissions return existing durable ids and must not create
  duplicate observations or outbox events.
- Raw API keys must not be persisted in models, audit metadata, outbox payloads,
  logs, or responses.
- Hook payload and observation content must redact obvious secret-bearing keys
  and token-shaped values before persistence.
- Request signatures and managed hook trust signing are deferred for a later
  hook/CLI trust checkpoint; this slice is bearer-token authenticated only.
- Docker Compose live checks are recorded as blocked while Docker is unavailable
  in this WSL distro.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-hook-ingest-design.md`
- Create: `docs/superpowers/plans/2026-06-25-hook-ingest.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/agent-integrations.md`,
  `docs/backend-contracts.md`, `docs/client-installation.md`, and
  `docs/parity/claude-mem-parity-map.md`.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write the design and plan**

Document endpoints, service boundaries, request/response contracts,
idempotency, authorization, explicit deferrals, tests, and verification.

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
git add docs/superpowers/specs/2026-06-25-hook-ingest-design.md docs/superpowers/plans/2026-06-25-hook-ingest.md
git commit -m "chore: add hook ingest plan"
```

### Task 2: Failing Hook API Contract Tests

**Files:**

- Create: `apps/backend/engram/hooks/hook_ingest_tests.py`

**Interfaces:**

- Consumes: existing `engram.access` models/services and `engram.core` durable
  models.
- Produces: failing endpoint tests for dry-run, ingest, replay, denial, and
  validation behavior.

- [ ] **Step 1: Add test helpers**

Create fixtures/helpers in `hook_ingest_tests.py` that:

- create organization, team, project, `ProjectTeam`;
- create a service-account identity;
- grant developer project access;
- create a project-scoped API key with `observations:write`;
- build `Authorization: Bearer <raw-key>` headers;
- build a valid post-tool-use payload.

- [ ] **Step 2: Add dry-run tests**

Add tests that post to `/v1/hooks/dry-run` and assert:

- valid key returns HTTP 200, `status: ok`, resolved actor, scope filters, and
  no raw key in response;
- missing key returns HTTP 401 with `code: missing_api_key`;
- wrong-project key returns HTTP 403 with `code: project_scope_denied`.

- [ ] **Step 3: Add ingest tests**

Add tests that post to `/v1/hooks/post-tool-use` and assert:

- HTTP 202 response with durable ids;
- exactly one `Agent`, `AgentSession`, `RawEventEnvelope`, `Observation`,
  `ObservationSource`, and `OutboxEvent`;
- raw event fields preserve runtime, client event id, idempotency key, payload,
  request id, actor, repository metadata, and content hash;
- observation fields preserve normalized title/body/files;
- thin hook payloads without `observation` create deterministic observation
  title/type server-side;
- secret-shaped payload and observation values are redacted before persistence;
- omitted `team_id` still persists the key-bound team when the API key is
  team-scoped;
- outbox payload references ids only and does not contain the raw API key.

- [ ] **Step 4: Add replay and denial tests**

Add tests for:

- duplicate idempotency returns `duplicate: true` and does not increase durable
  row counts;
- same session/event id replay returns `duplicate: true`;
- a replay insert race that reaches the database constraint returns existing
  rows instead of HTTP 500;
- cross-project request returns HTTP 403 and creates no raw events;
- malformed payload missing `content_hash` returns HTTP 400;
- non-object `payload` returns HTTP 400.

- [ ] **Step 5: Add session-end test**

Add a test that posts to `/v1/hooks/session-end`, gets HTTP 202, marks the
session `ended`, and writes a session-end observation/outbox entry.

- [ ] **Step 6: Run focused tests and verify first failure**

Run:

```bash
cd apps/backend && poetry run pytest engram/hooks/hook_ingest_tests.py -v
```

Expected before implementation: collection fails with missing `engram.hooks`
module or endpoint 404.

### Task 3: Hook App Services And Views

**Files:**

- Create: `apps/backend/engram/hooks/__init__.py`
- Create: `apps/backend/engram/hooks/apps.py`
- Create: `apps/backend/engram/hooks/serializers.py`
- Create: `apps/backend/engram/hooks/services.py`
- Create: `apps/backend/engram/hooks/urls.py`
- Create: `apps/backend/engram/hooks/views.py`
- Modify: `apps/backend/settings/settings.py`
- Modify: `apps/backend/settings/urls.py`

**Interfaces:**

- Consumes: tests from Task 2 and `ResolveApiKeyScope.execute()`.
- Produces: installed app, `/v1/hooks/dry-run`, `/v1/hooks/post-tool-use`, and
  `/v1/hooks/session-end`.

- [ ] **Step 1: Add app and URLs**

Add `HooksConfig`, install `engram.hooks`, and include `engram.hooks.urls` at
`path('v1/hooks/', ...)`.

- [ ] **Step 2: Add serializers**

Implement serializers for dry-run and hook event request fields. Require:
`project_id`, `agent_runtime`, `session_id`, `event_id`, `idempotency_key`,
`event_type`, `payload_schema_version`, `content_hash`, and `payload` for
ingest. Validate `payload` as a JSON object. Allow optional `observation`,
`team_id`, `sequence_number`, `occurred_at`, repository metadata, branch, cwd,
and request id.

- [ ] **Step 3: Add credential extraction and error mapping**

Views accept only `Authorization: Bearer <token>`. Missing credentials return
HTTP 401 `missing_api_key`. `AccessDeniedError` maps `invalid_key` to HTTP 401
and all other access codes to HTTP 403.

- [ ] **Step 4: Implement dry-run service**

`VerifyHookDryRun.execute()` calls `ResolveApiKeyScope` with
`observations:write`, requested project/team ids, target type `hook_dry_run`,
and returns scope data.

- [ ] **Step 5: Implement ingest service**

`IngestHookEvent.execute()` resolves API-key scope, creates or loads agent and
session, writes raw event, observation, source, and outbox inside
`transaction.atomic()`, creates a deterministic observation shell when the hook
payload has no normalized `observation`, redacts secret-shaped persisted
content, derives omitted key-bound team scope, and returns `HookIngestResult`.

- [ ] **Step 6: Implement replay handling**

Before inserting new rows, look up existing `RawEventEnvelope` by
organization/project/idempotency key or organization/project/session/event id.
If found, return existing raw event, first linked observation, first matching
outbox, and `duplicate=True`. If a concurrent duplicate insert raises
`IntegrityError`, reload the matching duplicate and return it; re-raise only if
the conflict cannot be tied to an existing duplicate hook event.

- [ ] **Step 7: Run focused tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/hooks/hook_ingest_tests.py -v
```

Expected: all focused hook tests pass.

### Task 4: Repository Gates And Verification Matrix

**Files:**

- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: hook app files and passing focused tests.
- Produces: repository-level gates requiring hook API files and recorded
  command evidence.

- [ ] **Step 1: Add repository layout requirements**

Require hook app serializers, services, views, URLs, tests, and app config.

- [ ] **Step 2: Run repository tests**

Run:

```bash
python3 -m unittest tests.repository.test_backend_runtime_contract -v
```

Expected: pass.

- [ ] **Step 3: Update verification matrix**

Add the `2026-06-25: Hook Dry-Run And Observation Ingest` checkpoint with
branch, scope, commands, exit codes, review findings, and first decisive TDD
failures.

### Task 5: Review And Final Verification

**Files:** no new owned files unless review findings require fixes.

**Interfaces:**

- Consumes: completed hook API implementation.
- Produces: fixed/refuted review findings and a coherent checkpoint commit.

- [ ] **Step 1: Run focused security review**

Check credential redaction, authorization before writes, duplicate replay,
outbox payload safety, non-object payload denial, replay race handling, scoped
team persistence, and cross-project/team denial.

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
git add apps/backend/settings/settings.py apps/backend/engram/hooks scripts/repository_layout.py tests/repository/test_backend_runtime_contract.py docs/verification-matrix.md
git commit -m "feat: add hook observation ingest"
```
