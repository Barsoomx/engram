# Design: Activate the SessionEnd hook (agent-loop closure)

> Branch `feat/session-end-hook-distillation`, off current master. Roadmap Слой 3
> "Закрыть agent-loop: вайрить SessionEnd (бэк есть, плагин не зовёт!)". Scout-verified.

## Problem
The backend session-distillation pipeline is wired end-to-end but **never fires in real
usage**: no client emits a `session_end` hook event.

- Backend (DONE, do not change): `hooks/urls.py` registers `session-end`; `IngestHookEvent`
  (`hooks/services.py:117-167`) on `event_type=='session_end'` sets `session.status=ENDED`,
  writes the raw event/observation, and queues `distill_session.delay(session.id)`;
  `memory/tasks.py:50` + `memory/distillation.py` implement synthesis→candidate→gate→
  curator→auto-promote. Event-level dedup (`_find_duplicate`) makes replays idempotent.
- Client (THE GAP): the CLI hook subparser only knows 4 commands
  (`packages/cli/engram_cli/main.py:104`), `run_hook` only dispatches those 4
  (`commands.py:598-650`), `write_local_state` only writes 4 manifest commands
  (`commands.py:1138-1155`), and both distributed plugin manifests
  (`packages/claude-plugin/hooks/hooks.json`, `packages/codex-plugin/plugin/hooks/codex-hooks.json`)
  register only `SessionStart/PostToolUse/Error/Decision`. Both contract tests hard-assert
  exactly those 4 (`REQUIRED_HOOK_EVENTS`).

Result: `distill_session.delay()` has zero production trigger path — the entire Layer-3
distillation/curator/confidence/auto-promote campaign is dead code in deployment.

## Target
Emit `session_end` from the CLI and both plugins so a finished coding session triggers
session distillation, with the contract guard actually enforced in CI. **One coherent
client-wiring slice. No backend behaviour change.**

## Changes (exact)

### 1. CLI — `packages/cli/engram_cli/main.py`
Line 104: add `"session-end"` to the hook subparser loop tuple:
`for command in ("post-tool-use", "session-start", "error", "decision", "session-end"):`
(no new args — `session-end` takes the same `--agent/--config-dir/--response-format`).

### 2. CLI — `packages/cli/engram_cli/commands.py`
- `run_hook` (after the `decision` branch, before the `else`): add
  ```python
  elif args.hook_command == "session-end":
      status, body = send_hook_event(
          active_transport,
          server_url=server_url,
          api_key=api_key,
          config=config,
          runtime=runtime,
          input_payload=input_payload,
          path="/v1/hooks/session-end",
          event_type="session_end",
      )
  ```
  (byte-for-byte mirror of the `error`/`decision` branches; `send_hook_event` already
  routes any non-`session_start` event through `build_generic_hook_payload`).
- `write_local_state` commands dict (`commands.py:1138-1155`): add a `"SessionEnd"` entry
  mirroring `"Error"`/`"Decision"`:
  ```python
  "SessionEnd": (
      f"engram hook session-end --agent {runtime} "
      f"--response-format {response_format_for_runtime(runtime)}"
  ),
  ```

### 3. Claude plugin manifest — `packages/claude-plugin/hooks/hooks.json`
Add a `"SessionEnd"` event (mirror `Error`, matcher `"*"`, timeout 60):
command `"engram hook session-end --agent claude_code --response-format claude-code"`.

### 4. Codex plugin manifest — `packages/codex-plugin/plugin/hooks/codex-hooks.json`
Add a `"SessionEnd"` event (mirror `Error`, matcher `".*"`, timeout 60):
command `"engram hook session-end --agent codex --response-format codex"`.

### 5. Contract tests (keep the guard in lockstep)
`packages/claude-plugin/claude_plugin_contract_tests.py:17` and
`packages/codex-plugin/codex_plugin_contract_tests.py:11`:
`REQUIRED_HOOK_EVENTS = ("SessionStart", "PostToolUse", "Error", "Decision", "SessionEnd")`.
The existing `assertEqual(len(REQUIRED_HOOK_EVENTS), len(commands))` then enforces the
manifest stays at 5 events in lockstep.

### 6. CI — `.github/workflows/backend.yml`
The plugin contract tests are NOT run in CI today (only `packages/cli` + `packages/mcp`
are discovered, lines 77/80). Add two steps so the SessionEnd contract is enforced:
```yaml
      - name: Run Claude plugin contract tests
        run: PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v
      - name: Run Codex plugin contract tests
        run: PYTHONPATH=packages/codex-plugin python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v
```
If discovery surfaces a PRE-EXISTING unrelated failure, STOP and report (do not fix
out-of-scope breakage in this slice).

### 7. Docs
Record the hook lifecycle ordering (`SessionStart → PostToolUse* → Decision/Error →
SessionEnd`) and a one-line note that existing installs need a plugin re-install /
version bump before SessionEnd fires. Put it where the other hook events are documented
(grep `PostToolUse` under `docs/`); if no such doc exists, add a short note to the
codex/claude plugin `README.md`.

## TDD / tests
- **CLI lifecycle test** (`packages/cli/engram_cli/cli_lifecycle_tests.py`, mirror the
  existing `error`/`decision` lifecycle tests + `FakeTransport`): with a connected config
  + credentials in a tempdir, `main.main(["hook","session-end","--agent","claude_code",
  "--config-dir",...], stdin=<json with session_id>, transport=FakeTransport([(200,{...})]))`
  asserts the POST URL ends `/v1/hooks/session-end` and the payload `event_type` is
  `session_end`; a non-2xx response propagates exit code 1. If an existing CLI test asserts
  the connect-written manifest has exactly 4 commands, update it to include `SessionEnd`.
- **Plugin contract tests**: updated `REQUIRED_HOOK_EVENTS` must pass for both plugins.
- **Backend** (add ONLY if not already covered — grep `session_end` /`distill_session` in
  `apps/backend/engram/hooks/*_tests.py`): one test asserting ingesting a `session_end`
  hook event sets `session.status=ENDED` and enqueues `distill_session` exactly once, and
  a duplicate `session_end` event (same idempotency key) does NOT re-enqueue.

## Out of scope (roadmap follow-ups; keep one behavior slice)
`PreToolUse`, `UserPromptSubmit`, and the Cursor adapter.

## Conventions / gate
- **Match each file's existing style**: the CLI package uses DOUBLE quotes + `from __future__
  import annotations` (NOT the backend single-quote rule); manifests are JSON; backend (if a
  test is added) uses single quotes. No `Co-Authored-By`.
- **Full gate (all must pass):**
  1. backend `ruff check .` + `ruff format --check .` (in `apps/backend`)
  2. backend `migrate` + `makemigrations --check --dry-run` → No changes detected (no model change)
  3. backend `pytest -v` (full suite green; run via the pg+pgvector container)
  4. `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v`
  5. `PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v`
  6. `PYTHONPATH=packages/codex-plugin python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v`
