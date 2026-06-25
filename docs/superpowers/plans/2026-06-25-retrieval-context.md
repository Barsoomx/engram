# Retrieval Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authorization-filtered exact retrieval and context bundle APIs
that return cited approved memory for future agent sessions.

**Architecture:** Create an `engram.context` Django app with serializers,
views, and service boundaries. The services index approved memory versions into
`RetrievalDocument` rows, resolve API-key scope with `memories:read`, filter
documents before ranking, persist `ContextBundle`/`ContextBundleItem`/audit
records, and return compact rendered context.

**Tech Stack:** Django 5.2, Django REST Framework, PostgreSQL target with
sqlite test database, Poetry, pytest-django, Ruff.

## Global Constraints

- Work on branch `feat/parity-08-retrieval-context`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use pytest function tests named `*_tests.py`.
- Use TDD: write failing tests before production code.
- Do not add semantic/vector retrieval, provider calls, provider secrets,
  embeddings, memory candidate promotion, CLI behavior, frontend files, MCP
  tools, or Docker golden-path fixtures.
- Authorization must run before retrieval, ranking, packing, or response
  construction.
- Context responses, audit metadata, and bundle/item metadata must not contain
  raw API keys or bearer tokens.
- Docker Compose live checks are recorded as blocked while Docker is unavailable
  in this WSL distro.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-retrieval-context-design.md`
- Create: `docs/superpowers/plans/2026-06-25-retrieval-context.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/north-star.md`, `docs/search-and-retrieval.md`,
  `docs/backend-contracts.md`, `docs/agent-integrations.md`,
  `docs/rbac-and-scopes.md`, `docs/architecture.md`,
  `docs/parity/claude-mem-parity-map.md`, and existing `engram.core`,
  `engram.access`, `engram.hooks`, and `engram.memory` code.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write the design and plan**

Document request/response contracts, authorization filters, exact ranking,
retrieval document indexing, context bundle persistence, audit behavior,
explicit deferrals, tests, and verification.

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
git add docs/superpowers/specs/2026-06-25-retrieval-context-design.md docs/superpowers/plans/2026-06-25-retrieval-context.md
git commit -m "chore: add retrieval context plan"
```

### Task 2: Failing Context API And Indexing Tests

**Files:**

- Create: `apps/backend/engram/context/context_api_tests.py`

**Interfaces:**

- Consumes: existing core/access models, `ResolveApiKeyScope`, and the planned
  service interfaces:
  - `IndexMemoryVersionInput(memory_version_id: uuid.UUID)`
  - `IndexMemoryVersion.execute(data: IndexMemoryVersionInput)`
  - `BuildContextBundleInput(...)`
  - `BuildContextBundle.execute(data: BuildContextBundleInput)`
- Produces: failing tests for context endpoints and retrieval-document indexing.

- [ ] **Step 1: Add test helpers**

Create helpers in `context_api_tests.py` that:

- create organization, team, project, `ProjectTeam`, service-account identity,
  developer grants, and an API key;
- allow API-key capability overrides, especially `('memories:read',)` and
  `('observations:write',)`;
- create approved memory, memory version, and retrieval document rows;
- provide `auth_headers(raw_key: str = RAW_KEY) -> dict[str, str]`;
- provide `valid_context_payload(project, team, **overrides)`.

- [ ] **Step 2: Add session-start success test**

Add:

```python
def test_session_start_returns_cited_exact_context_and_persists_bundle() -> None:
```

Arrange one approved project memory with a retrieval document containing:

```python
file_paths=['apps/backend/engram/context/services.py']
symbols=['BuildContextBundle']
exact_terms=['context bundle', 'authorization before ranking']
full_text='Authorization before ranking protects context bundles.'
```

Call `POST /v1/context/session-start` with a query and file path that match the
document. Assert:

- HTTP 200;
- `status == 'created'`;
- `purpose == 'session_start'`;
- one item with citation `M1`;
- `rendered_context` contains `M1`, memory title, and memory body;
- `hook_specific_output.hookEventName == 'SessionStart'`;
- one `ContextBundle`, one `ContextBundleItem`, and one `AuditEvent` with
  `event_type == 'MemoryRetrieved'` exist;
- `RAW_KEY` is absent from the response, bundle metadata, item metadata, and
  audit metadata.

- [ ] **Step 3: Add access denial tests**

Add tests proving:

- missing bearer key returns HTTP 401 and `code == 'missing_api_key'`;
- an API key without `memories:read` returns HTTP 403 and
  `code == 'missing_capability'`;
- a project-scoped API key requesting another project returns HTTP 403 and
  creates no `ContextBundle`.

- [ ] **Step 4: Add authorization-before-ranking test**

Add:

```python
def test_session_start_filters_other_team_memory_before_ranking() -> None:
```

Arrange two team-scoped retrieval documents in the same project with the same
exact match, one for the key-bound team and one for another team. Assert only
the key-bound team's memory appears in the response and only one bundle item is
created.

- [ ] **Step 5: Add filter-only and duplicate tests**

Add tests proving:

- empty `query`, `file_paths`, and `symbols` still returns newest authorized
  approved project memory up to the requested limit;
- replaying the same `request_id` returns the existing bundle and does not
  create duplicate items or another `MemoryRetrieved` audit event.

- [ ] **Step 6: Add task endpoint test**

Add:

```python
def test_task_context_endpoint_uses_task_purpose() -> None:
```

Call `POST /v1/context` with the same payload shape and assert `purpose ==
'task'` in the response and persisted bundle.

- [ ] **Step 7: Add indexing service tests**

Add tests proving:

- `IndexMemoryVersion().execute(...)` creates a `RetrievalDocument` for an
  approved memory version using memory metadata exact terms, symbols, file
  paths, source observation file references, and version body;
- indexing a non-approved memory raises `ContextIndexError` and creates no
  retrieval document.

- [ ] **Step 8: Run focused tests and verify first failure**

Run:

```bash
cd apps/backend && poetry run pytest engram/context/context_api_tests.py -v
```

Expected before implementation: collection fails with missing `engram.context`
module or service import.

### Task 3: Context App, Serializers, And Routes

**Files:**

- Create: `apps/backend/engram/context/__init__.py`
- Create: `apps/backend/engram/context/apps.py`
- Create: `apps/backend/engram/context/serializers.py`
- Create: `apps/backend/engram/context/urls.py`
- Create: `apps/backend/engram/context/views.py`
- Modify: `apps/backend/settings/settings.py`
- Modify: `apps/backend/settings/urls.py`

**Interfaces:**

- Consumes: tests from Task 2 and existing access error behavior.
- Produces:
  - `ContextConfig`
  - `ContextRequestSerializer`
  - `SessionStartContextView`
  - `TaskContextView`
  - URL routes for `/v1/context/session-start` and `/v1/context`.

- [ ] **Step 1: Add app registration**

Create `ContextConfig`, install `engram.context` in `INSTALLED_APPS`, and add
context URLs before hook URLs:

```python
path('v1/context/', include('engram.context.urls')),
```

- [ ] **Step 2: Add serializer**

Implement `ContextRequestSerializer` with required fields:

```python
project_id = serializers.UUIDField()
agent_runtime = serializers.CharField(max_length=40)
session_id = serializers.CharField(max_length=255)
request_id = serializers.CharField(max_length=255)
```

and optional fields:

```python
team_id = serializers.UUIDField(required=False, allow_null=True)
agent_version = serializers.CharField(required=False, allow_blank=True)
agent_external_id = serializers.CharField(required=False, allow_blank=True)
correlation_id = serializers.CharField(required=False, allow_blank=True)
trace_id = serializers.CharField(required=False, allow_blank=True)
repository_url = serializers.CharField(required=False, allow_blank=True)
repository_root = serializers.CharField(required=False, allow_blank=True)
branch = serializers.CharField(required=False, allow_blank=True)
cwd = serializers.CharField(required=False, allow_blank=True)
query = serializers.CharField(required=False, allow_blank=True)
file_paths = serializers.ListField(child=serializers.CharField(), required=False)
symbols = serializers.ListField(child=serializers.CharField(), required=False)
limit = serializers.IntegerField(required=False, min_value=1, max_value=10)
token_budget = serializers.IntegerField(required=False, min_value=1)
```

- [ ] **Step 3: Add views**

Implement two API views with no DRF auth/permission classes. Each view:

- validates `ContextRequestSerializer`;
- reads `Authorization: Bearer <key>`;
- calls `BuildContextBundle` with purpose `session_start` or `task`;
- maps `AccessDeniedError` to existing code/status responses;
- returns `result.to_response()`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/context/context_api_tests.py -v
```

Expected: tests still fail because service classes are not implemented.

### Task 4: Retrieval Document Indexing Service

**Files:**

- Create/Modify: `apps/backend/engram/context/services.py`

**Interfaces:**

- Consumes: `Memory`, `MemoryStatus`, `MemoryVersion`, `Observation`, and
  `RetrievalDocument`.
- Produces:
  - `ContextIndexError`
  - `IndexMemoryVersionInput`
  - `IndexMemoryVersionResult`
  - `IndexMemoryVersion.execute(data: IndexMemoryVersionInput)`
  - term normalization helpers reused by context retrieval.

- [ ] **Step 1: Add DTOs and error**

Define frozen dataclasses:

```python
@dataclass(frozen=True)
class IndexMemoryVersionInput:
    memory_version_id: uuid.UUID


@dataclass(frozen=True)
class IndexMemoryVersionResult:
    retrieval_document: RetrievalDocument
    created: bool


class ContextIndexError(Exception):
    pass
```

- [ ] **Step 2: Add normalization helpers**

Implement:

```python
def normalize_lookup_value(value: object) -> str:
    return str(value).strip().casefold()


def normalize_lookup_values(values: object) -> tuple[str, ...]:
    ...
```

The helper must accept strings, lists, tuples, and sets, drop empty values, trim
whitespace, casefold values, and dedupe while preserving order.

- [ ] **Step 3: Implement `IndexMemoryVersion.execute()`**

Load `MemoryVersion` with `memory` and `source_observation`. Reject when
`memory.status != MemoryStatus.APPROVED`.

Build defaults:

```python
full_text = f'{memory.title}\n\n{version.body}'.strip()
file_paths = normalized metadata file paths plus source observation files
symbols = normalized memory metadata symbols
exact_terms = normalized memory metadata exact terms plus memory title terms
```

Use `RetrievalDocument.objects.update_or_create(memory_version=version, ...)`.
Copy organization, project, team, memory, visibility scope, stale, and refuted
from the memory.

- [ ] **Step 4: Run indexing tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/context/context_api_tests.py::test_index_memory_version_creates_retrieval_document_for_approved_memory -v
cd apps/backend && poetry run pytest engram/context/context_api_tests.py::test_index_memory_version_rejects_non_approved_memory -v
```

Expected: both pass.

### Task 5: Context Bundle Service And Exact Ranking

**Files:**

- Modify: `apps/backend/engram/context/services.py`

**Interfaces:**

- Consumes: `ResolveApiKeyScope`, context serializer inputs, and indexed
  `RetrievalDocument` rows.
- Produces:
  - `ContextBundleInput`
  - `ContextItemResult`
  - `ContextBundleResult`
  - `BuildContextBundle.execute(data: ContextBundleInput)`
  - deterministic exact retrieval and rendering helpers.

- [ ] **Step 1: Add context DTOs**

Define frozen dataclasses for:

```python
ContextBundleInput(
    raw_key: str,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    agent_runtime: str,
    agent_version: str,
    agent_external_id: str,
    session_id: str,
    request_id: str,
    correlation_id: str,
    trace_id: str,
    repository_url: str,
    repository_root: str,
    branch: str,
    cwd: str,
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    limit: int,
    token_budget: int | None,
    purpose: str,
)
```

Define result DTOs with `to_response()` returning only JSON-serializable values.

- [ ] **Step 2: Resolve scope and durable session**

In `BuildContextBundle.execute()`:

- call `ResolveApiKeyScope().execute(required_capability='memories:read')`;
- load organization/project;
- resolve team from request or single key-bound team;
- get or create `Agent` and `AgentSession`;
- check duplicate `ContextBundle` by organization/project/request id and return
  existing items when found.

- [ ] **Step 3: Apply authorization filters before scoring**

Build a base queryset over `RetrievalDocument` with `select_related()` and
filters:

```python
organization=organization
project=project
memory__status=MemoryStatus.APPROVED
memory__stale=False
memory__refuted=False
stale=False
refuted=False
```

Keep only project-scope documents and team-scope documents whose team id is in
`scope.team_ids`. Exclude session and organization visibility in this slice.

- [ ] **Step 4: Implement exact scoring**

Score each authorized document in Python:

- `+100` for file path exact or suffix match;
- `+80` for symbol exact match;
- `+60` for exact term match;
- `+40` for full-text phrase/token match;
- include filter-only rows with score `1` when no request terms are present;
- exclude score `0` rows when request terms are present.

Sort by score descending, document update time descending, memory title, and
document id. Limit to `data.limit`.

- [ ] **Step 5: Persist bundle, items, and audit**

Inside one transaction:

- create `ContextBundle`;
- create `ContextBundleItem` rows with citations `M1`, `M2`, etc.;
- render compact markdown;
- update `rendered_text` and `selected_count`;
- create one `AuditEvent(event_type='MemoryRetrieved')` with ids, counts,
  scope filters, and retrieval strategy metadata.

- [ ] **Step 6: Run focused context tests**

Run:

```bash
cd apps/backend && poetry run pytest engram/context/context_api_tests.py -v
```

Expected: all context tests pass.

### Task 6: Verification, Simplicity Review, And Commit

**Files:**

- Review all files changed in Tasks 2-5.

**Interfaces:**

- Consumes: finished implementation and tests.
- Produces: verified implementation commit.

- [ ] **Step 1: Run required local verification**

Run:

```bash
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest engram/context/context_api_tests.py -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
cd apps/backend && poetry check
git diff --check HEAD
docker compose version
```

Expected: all commands exit 0 except `docker compose version`, which exits 1 in
this WSL distro with Docker unavailable.

- [ ] **Step 2: Run Karpathy-style simplicity review**

Check:

- every changed Python line traces to this context slice;
- no provider, semantic, CLI, frontend, MCP, or promotion behavior slipped in;
- duplicated helper code is either tiny or moved only when it reduces real
  complexity;
- exact retrieval remains deterministic and testable.

- [ ] **Step 3: Inspect diff and stage only owned files**

Run:

```bash
git status --short
git diff -- docs/superpowers/specs/2026-06-25-retrieval-context-design.md docs/superpowers/plans/2026-06-25-retrieval-context.md apps/backend/engram/context apps/backend/settings/settings.py apps/backend/settings/urls.py
```

Do not stage `.gitignore`.

- [ ] **Step 4: Commit implementation**

Commit:

```bash
git add apps/backend/engram/context apps/backend/settings/settings.py apps/backend/settings/urls.py
git commit -m "feat: add retrieval context API"
```

### Task 7: Draft PR, CI, And Merge Checkpoint

**Files:**

- No additional source files unless CI finds a scoped issue.

**Interfaces:**

- Consumes: planning and implementation commits.
- Produces: draft PR/MR with evidence, CI status, and merge or stop report.

- [ ] **Step 1: Open draft PR**

Open a draft PR against `master` summarizing:

- exact retrieval/context API behavior;
- commands and exit codes;
- Docker Compose blocked state;
- security note that authorization filters run before ranking.

- [ ] **Step 2: Wait for CI**

Record CI job names, statuses, and first failure if any.

- [ ] **Step 3: Fix or stop**

If CI fails with a scoped implementation issue, reproduce locally, add a failing
test when practical, fix, verify, and push. If the first decisive failure is
unclear or requires changing architecture, data model, security model,
migration strategy, release process, or public API, stop with the report format
required by `AGENTS.md`.

- [ ] **Step 4: Promote and merge**

When local verification and CI pass, mark the PR ready, merge with the
repository's normal non-force flow, update local `master`, and record the final
SHA plus commands in the status report.
