# Upstream Migration Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal idempotent `claude-mem` migration importer with a
sanitized fixture, unsupported-record report, command, tests, and security
artifact.

**Architecture:** Add a focused `engram.imports` Django app. The importer reads
upstream SQLite from a source root, maps useful rows into existing Engram
models, builds retrieval documents through current services, reports
unsupported artifacts, and exposes dry-run/apply through one management command.

**Tech Stack:** Python 3.12, Django management commands, sqlite3 stdlib,
existing Engram core/memory/context models and services, pytest, GitHub
Actions.

## Global Constraints

- Work on branch `feat/parity-13-upstream-migration-import`.
- Do not implement frontend, MCP, transcript replay, Chroma/vector import,
  corpora import, provider generation, or broad memory curation.
- Do not add database migrations unless existing constraints cannot prove
  idempotency; stop and report before schema changes.
- Use existing Engram models and `IndexMemoryVersion` for retrieval documents.
- Default command mode is dry-run; apply requires `--apply`.
- Every report must include unsupported artifacts with source ids and reasons.
- Raw API keys, provider secrets, bearer tokens, and token-shaped prompt/tool
  content must be redacted before persisted rows and command output.
- Add a focused security review artifact under `docs/security/reviews/`.
- Keep Python style: absolute imports, built-in generics, single quotes, and
  pytest function tests.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-upstream-migration-import-design.md`
- Create: `docs/superpowers/plans/2026-06-25-upstream-migration-import.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/parity/claude-mem-parity-map.md`, upstream
  artifact audit evidence, current Engram models/services.
- Produces: committed design and implementation plan for importer workers.

- [ ] **Step 1: Write the design spec**

  Capture source artifacts, target mapping, report JSON, fixture shape, command
  contract, security review requirements, verification commands, and deferrals.

- [ ] **Step 2: Write this implementation plan**

  Break implementation into fixture/layout, importer service/command,
  security/evidence, and review tasks.

- [ ] **Step 3: Run docs sanity checks**

  Run:

  ```bash
  python3 scripts/repository_quality.py
  git diff --check HEAD
  ```

  Expected: both commands exit 0.

- [ ] **Step 4: Commit**

  ```bash
  git add docs/superpowers/specs/2026-06-25-upstream-migration-import-design.md docs/superpowers/plans/2026-06-25-upstream-migration-import.md
  git commit -m "chore: add upstream migration import plan"
  ```

### Task 2: Import App, Fixture, And Repository Gates

**Files:**

- Create: `apps/backend/engram/imports/__init__.py`
- Create: `apps/backend/engram/imports/apps.py`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/manifest.json`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/claude_mem_minimal.sql`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/settings.json`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/transcript-watch.json`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/transcript-watch-state.json`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/corpora/deferred.corpus.json`
- Create: `apps/backend/engram/imports/fixtures/claude_mem_minimal/vector-db/.keep`
- Modify: `apps/backend/settings/settings.py`
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Modify: `tests/repository/test_repository_layout.py` if needed.

**Interfaces:**

- Consumes: Django app registry.
- Produces: `engram.imports` app and sanitized upstream fixture path.

- [ ] **Step 1: Add failing repository layout tests**

  Extend `BackendRuntimeLayoutTests.expected` with:

  ```python
  'apps/backend/engram/imports/apps.py',
  'apps/backend/engram/imports/services.py',
  'apps/backend/engram/imports/upstream_import_tests.py',
  'apps/backend/engram/imports/fixtures/claude_mem_minimal/manifest.json',
  'apps/backend/engram/imports/fixtures/claude_mem_minimal/claude_mem_minimal.sql',
  'apps/backend/engram/imports/management/commands/engram_import_claude_mem.py',
  ```

  Run:

  ```bash
  python3 -m unittest tests.repository.test_backend_runtime_contract -v
  ```

  Expected before implementation: failure naming missing importer paths.

- [ ] **Step 2: Create import app skeleton**

  Add `ImportConfig`:

  ```python
  from django.apps import AppConfig


  class ImportConfig(AppConfig):
      default_auto_field = 'django.db.models.BigAutoField'
      name = 'engram.imports'
  ```

  Add `'engram.imports'` to `INSTALLED_APPS`.

- [ ] **Step 3: Add sanitized fixture**

  Create `claude_mem_minimal.sql` with tables:

  - `schema_versions`;
  - `sdk_sessions`;
  - `user_prompts`;
  - `observations`;
  - `session_summaries`;
  - `pending_messages`;
  - `observation_feedback`.

  Include one fake row per required source type. Use fake paths such as
  `/workspace/example-repo`, fake model/runtime names, and one fake token-shaped
  value such as `sk-test_fake_import_token_1234567890` only where redaction is
  asserted.

- [ ] **Step 4: Add fixture manifest**

  `manifest.json` must include:

  ```json
  {
    "source_store_id": "fixture-store",
    "expected": {
      "sdk_sessions": 1,
      "user_prompts": 1,
      "observations": 1,
      "session_summaries": 1,
      "pending_messages": 1,
      "observation_feedback": 1
    }
  }
  ```

- [ ] **Step 5: Update layout registry and verify**

  Add importer paths to `scripts/repository_layout.py`, then run:

  ```bash
  python3 scripts/repository_layout.py
  python3 -m unittest tests.repository.test_backend_runtime_contract -v
  ```

  Expected: both exit 0.

- [ ] **Step 6: Commit**

  ```bash
  git add apps/backend/engram/imports apps/backend/settings/settings.py scripts/repository_layout.py tests/repository/test_backend_runtime_contract.py tests/repository/test_repository_layout.py
  git commit -m "chore: add upstream import fixture gates"
  ```

### Task 3: Import Report And Dry-Run Service

**Files:**

- Create: `apps/backend/engram/imports/services.py`
- Create: `apps/backend/engram/imports/upstream_import_tests.py`

**Interfaces:**

- Consumes: fixture paths from Task 2 and current core models.
- Produces:
  - `ClaudeMemImportInput`;
  - `ClaudeMemImporter`;
  - JSON-serializable `ImportReport`.

- [ ] **Step 1: Add failing dry-run tests**

  Add pytest fixtures that create organization, project, team, and fixture
  source root. Add:

  ```python
  def test_claude_mem_importer_dry_run_reports_counts_without_writes(f_import_scope, f_claude_mem_fixture):
      report = ClaudeMemImporter().execute(
          ClaudeMemImportInput(
              source_root=f_claude_mem_fixture,
              organization_id=f_import_scope.organization.id,
              project_id=f_import_scope.project.id,
              team_id=f_import_scope.team.id,
              source_store_id='fixture-store',
              apply=False,
          )
      )

      assert report['mode'] == 'dry_run'
      assert report['counts']['sdk_sessions']['seen'] == 1
      assert report['counts']['observations']['importable_memories'] == 1
      assert report['counts']['session_summaries']['importable_memories'] == 1
      assert report['counts']['pending_messages']['unsupported'] == 1
      assert AgentSession.objects.count() == 0
      assert RawEventEnvelope.objects.count() == 0
      assert Observation.objects.count() == 0
      assert Memory.objects.count() == 0
  ```

  Run:

  ```bash
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py::test_claude_mem_importer_dry_run_reports_counts_without_writes -v
  ```

  Expected before implementation: import error for missing service.

- [ ] **Step 2: Implement SQLite source reader**

  Use `sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)` and load table
  names from `sqlite_master`. Read only expected tables when present. Missing
  optional tables count as zero and produce warnings only for missing
  `claude-mem.db`.

- [ ] **Step 3: Implement report counting**

  Return a plain `dict[str, object]` with `mode`, `source`, `target`, `counts`,
  `created`, `duplicates`, `unsupported`, `warnings`, and `redactions`. Populate
  unsupported entries for known unsupported files/directories and unsupported
  tables.

- [ ] **Step 4: Verify dry-run**

  Run:

  ```bash
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py::test_claude_mem_importer_dry_run_reports_counts_without_writes -v
  ```

  Expected: pass.

- [ ] **Step 5: Commit**

  ```bash
  git add apps/backend/engram/imports/services.py apps/backend/engram/imports/upstream_import_tests.py
  git commit -m "feat: add claude mem import dry run"
  ```

### Task 4: Apply Import And Idempotency

**Files:**

- Modify: `apps/backend/engram/imports/services.py`
- Modify: `apps/backend/engram/imports/upstream_import_tests.py`

**Interfaces:**

- Consumes: `ClaudeMemImporter.execute(...)` from Task 3.
- Produces: apply mode that creates sessions, raw events, observations,
  memories, versions, retrieval documents, duplicates report, and unsupported
  report.

- [ ] **Step 1: Add failing apply/idempotency tests**

  Add tests named:

  - `test_claude_mem_importer_imports_observations_and_summaries_as_approved_memory_documents`;
  - `test_claude_mem_importer_is_idempotent_for_rerun`;
  - `test_claude_mem_importer_preserves_prompt_rows_as_raw_events_without_promoting_them`;
  - `test_claude_mem_importer_reports_unsupported_records_with_source_ids_and_reasons`;
  - `test_claude_mem_importer_redacts_token_shaped_values_before_persisting_or_reporting`;
  - `test_claude_mem_importer_rejects_cross_scope_team_before_writes`.

  Run:

  ```bash
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v
  ```

  Expected before implementation: apply tests fail with zero created rows.

- [ ] **Step 2: Implement scoped target validation**

  Load `Organization`, `Project`, and optional `Team`. Reject when project or
  team does not belong to organization before opening a write transaction.

- [ ] **Step 3: Implement imported session creation**

  Use `Agent.objects.get_or_create` with runtime from upstream when available or
  `Runtime.UNKNOWN`. Use stable external session id as
  `AgentSession.external_session_id`. Preserve content/memory session ids,
  repository root, cwd, branch, started/ended timestamps, and upstream metadata.

- [ ] **Step 4: Implement prompt raw-event import**

  Create one `RawEventEnvelope` for prompt rows. Use source id as
  `client_event_id` and `idempotency_key`, redacted prompt payload, event type
  `claude_mem.user_prompt`, and no promoted memory.

- [ ] **Step 5: Implement observation and summary import**

  For each importable observation/summary, create or reuse:

  - `RawEventEnvelope`;
  - `Observation`;
  - `ObservationSource`;
  - `MemoryCandidate`;
  - promoted `Memory`;
  - `MemoryVersion`;
  - `RetrievalDocument` via `IndexMemoryVersion`.

  Use deterministic content hashes based on source ids and redacted body.

- [ ] **Step 6: Implement duplicate accounting**

  On rerun, do not create new rows. Increment `duplicates` for existing
  sessions, raw events, observations, and memories.

- [ ] **Step 7: Verify focused importer tests**

  Run:

  ```bash
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v
  ```

  Expected: all importer tests pass.

- [ ] **Step 8: Commit**

  ```bash
  git add apps/backend/engram/imports/services.py apps/backend/engram/imports/upstream_import_tests.py
  git commit -m "feat: import claude mem fixture records"
  ```

### Task 5: Management Command, CI Gate, And Security Review

**Files:**

- Create: `apps/backend/engram/imports/management/__init__.py`
- Create: `apps/backend/engram/imports/management/commands/__init__.py`
- Create: `apps/backend/engram/imports/management/commands/engram_import_claude_mem.py`
- Modify: `apps/backend/engram/imports/upstream_import_tests.py`
- Modify: `.github/workflows/backend.yml`
- Modify: `tests/repository/test_backend_workflow.py`
- Create: `docs/security/reviews/2026-06-25-upstream-migration-import.md`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: `ClaudeMemImporter` from Tasks 3 and 4.
- Produces: management command, CI focused test gate, security review artifact,
  and verification matrix entry.

- [ ] **Step 1: Add failing command test**

  Add:

  ```python
  def test_claude_mem_import_command_emits_sanitized_json_report(f_import_scope, f_claude_mem_fixture):
      out = io.StringIO()
      call_command(
          'engram_import_claude_mem',
          str(f_claude_mem_fixture),
          organization_id=str(f_import_scope.organization.id),
          project_id=str(f_import_scope.project.id),
          team_id=str(f_import_scope.team.id),
          source_store_id='fixture-store',
          dry_run=True,
          as_json=True,
          stdout=out,
      )
      payload = json.loads(out.getvalue())
      assert payload['mode'] == 'dry_run'
      assert 'sk-test_fake_import_token' not in out.getvalue()
  ```

  Run:

  ```bash
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py::test_claude_mem_import_command_emits_sanitized_json_report -v
  ```

  Expected before implementation: unknown command.

- [ ] **Step 2: Implement command**

  Add arguments:

  - positional `source_root`;
  - `--organization-id`;
  - `--project-id`;
  - `--team-id`;
  - `--source-store-id`;
  - `--dry-run`;
  - `--apply`;
  - `--json` with `dest='as_json'`.

  Reject commands that pass both `--dry-run` and `--apply`. Default to dry-run
  when neither is supplied.

- [ ] **Step 3: Add Backend workflow focused gate**

  Add before full backend tests:

  ```yaml
  - name: Run upstream import tests
    working-directory: apps/backend
    run: poetry run pytest engram/imports/upstream_import_tests.py -v
  ```

  Update `tests/repository/test_backend_workflow.py` to assert the command is
  present.

- [ ] **Step 4: Add focused security review artifact**

  Create `docs/security/reviews/2026-06-25-upstream-migration-import.md` with:

  - scope reviewed;
  - commands run;
  - findings by severity;
  - fixed/refuted findings;
  - accepted risks.

  The initial artifact may be produced by an independent read-only security
  reviewer after implementation and must not claim review before the reviewer
  runs.

- [ ] **Step 5: Update verification matrix**

  Append a `2026-06-25: Upstream Migration Import` entry with exact commands,
  exit codes, and first decisive failures.

- [ ] **Step 6: Verify task**

  Run:

  ```bash
  python3 -m unittest tests.repository.test_backend_workflow -v
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v
  ```

  Expected: both exit 0.

- [ ] **Step 7: Commit**

  ```bash
  git add apps/backend/engram/imports/management .github/workflows/backend.yml tests/repository/test_backend_workflow.py docs/security/reviews/2026-06-25-upstream-migration-import.md docs/verification-matrix.md apps/backend/engram/imports/upstream_import_tests.py
  git commit -m "feat: add claude mem import command"
  ```

### Task 6: Final Verification And Review

**Files:**

- No new files unless verification reveals defects.

**Interfaces:**

- Consumes: all prior tasks.
- Produces: reviewed, verified checkpoint branch ready for PR.

- [ ] **Step 1: Run full local verification**

  ```bash
  python3 scripts/repository_layout.py
  python3 scripts/repository_quality.py
  python3 -m unittest discover -s tests -v
  cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v
  cd apps/backend && poetry run pytest -v
  cd apps/backend && poetry run ruff check .
  cd apps/backend && poetry run ruff format --check .
  cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
  cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
  cd apps/backend && poetry check --lock
  git diff --check HEAD
  ```

  Expected: all commands exit 0.

- [ ] **Step 2: Dispatch independent reviews**

  Request:

  - importer correctness review;
  - security review focused on import/migration risks;
  - final whole-diff review.

- [ ] **Step 3: Fix or document findings**

  Critical and important findings must be fixed and re-reviewed. Accepted risks
  must be recorded in the security review artifact with owner/date.

- [ ] **Step 4: Publish checkpoint PR**

  Push the branch, open a draft PR, record local verification and reviewer
  evidence, wait for CI, then promote/merge only when checks are green.

## Plan Self-Review

- The plan covers dry-run, apply, idempotency, unsupported records, command
  output, security review, repository gates, CI, and verification.
- The plan does not require new schema or platform breadth.
- The task split lets workers own fixture/layout, service behavior, command/CI,
  and verification without overlapping write scopes.
