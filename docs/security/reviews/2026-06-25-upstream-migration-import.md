# Upstream Migration Import Security Review

Date: 2026-06-25

Branch: `feat/parity-13-upstream-migration-import`

Start SHA: `e49ebf034ee0fdb2aefa058b500e54dde3a4ae98`

Result: SECURITY APPROVED after fixes.

## Scope Reviewed

- `apps/backend/engram/imports/services.py`
- `apps/backend/engram/imports/management/commands/engram_import_claude_mem.py`
- `apps/backend/engram/imports/upstream_import_tests.py`
- `apps/backend/engram/core/redaction.py`
- `.github/workflows/backend.yml`
- `tests/repository/test_backend_workflow.py`
- Task 5 working-tree diff and command/security-fix evidence reports.

The focused review covered the import/export/migration risks required for this
slice: target organization/project/team validation before writes, mixed-source
project rejection, unsupported secret-bearing artifacts, `.env` and
`settings.json` handling, prompt/tool redaction before persistence and command
output, idempotency and duplicate prevention, no vector or Chroma import as
authority, and no upstream server-owned credential import.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| Command/CI review over Task 5 working tree | SPEC APPROVED / QUALITY APPROVED; no blocking findings. |
| Initial focused security review | SECURITY CHANGES_REQUIRED. Findings: mixed upstream projects were not rejected, provider-token redaction was too narrow, and `settings.json` was not reported as an unsupported secret-bearing artifact. |
| Security re-review after fixes | SECURITY APPROVED. CRITICAL none, IMPORTANT none, MINOR none. |
| `python3 -m unittest tests.repository.test_backend_workflow -v` | Exit 0. Ran 4 tests OK. |
| `git diff --check` | Exit 0. |
| `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry install --with dev --no-interaction && pytest engram/imports/upstream_import_tests.py -v && ruff check engram/imports engram/core/redaction.py && ruff format --check engram/imports engram/core/redaction.py"` | Exit 4 before fix verification was rebuilt. First decisive failure: `ERROR: file or directory not found: engram/imports/upstream_import_tests.py`. |
| `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --with dev --no-interaction && pytest engram/imports/upstream_import_tests.py -v && ruff check engram/imports engram/core/redaction.py && ruff format --check engram/imports engram/core/redaction.py"` | Exit 0. Importer pytest reported 15 passed; ruff check and format check were clean. |

## Findings By Severity

### CRITICAL

None open after re-review.

### IMPORTANT

Resolved: mixed upstream source projects could be accepted into one Engram target.
The importer now rejects multiple distinct upstream `project` values before
writing Engram rows.

Resolved: provider-token redaction was too narrow for importer persistence and
command JSON output. Shared redaction now covers `sk`, `egk`, `Bearer`, Gemini
`AIza`, Telegram bot-token, and Slack `xox` token shapes.

Resolved: `settings.json` was missing from unsupported secret-bearing artifact
reporting. The importer now reports `settings_secret_file_not_read` without
reading or returning secret values.

None open after re-review.

### MINOR

None open after re-review.

## Fixes Applied

- Mixed upstream `project` values across importable tables now raise
  `ClaudeMemImportError('source contains multiple projects')` before Engram
  writes occur.
- Redaction now covers OpenAI-style `sk`, Engram-style `egk`, bearer tokens,
  Gemini `AIza` keys, Telegram bot tokens, and Slack `xox` tokens before values
  are persisted or returned through command output.
- `settings.json` is treated as an unsupported secret-bearing artifact and is
  reported as `settings_secret_file_not_read` without reading values.

## Regression Tests Added

- `test_claude_mem_importer_rejects_mixed_upstream_projects_before_writes`
  verifies mixed upstream projects fail before sessions, raw events,
  observations, or memories are written.
- `test_claude_mem_import_command_redacts_provider_token_shapes_before_persisting_or_reporting`
  verifies Gemini, Telegram, and Slack token shapes do not appear in command
  output or persisted importer records.
- `test_claude_mem_importer_reports_settings_json_without_reading_secret_values`
  verifies `settings.json` is reported as unsupported without leaking the fake
  secret value.

## Accepted Risk

No remaining code security fix is required for Task 5 after re-review. Broad
repository/backend suites and final release-gate aggregation remain assigned to
Task 6 rather than this focused security artifact.
