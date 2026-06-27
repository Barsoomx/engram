# Claude Code Client Security Review

Date: 2026-06-25

Branch: `feat/parity-15-claude-code-client`

Result: SECURITY APPROVED after focused local verification and independent
read-only review.

## Scope Reviewed

- `packages/claude-plugin/.claude-plugin/plugin.json`
- `packages/claude-plugin/hooks/hooks.json`
- `packages/claude-plugin/claude_plugin_contract_tests.py`
- `packages/claude-plugin/README.md`
- `packages/cli/engram_cli/main.py`
- `packages/cli/engram_cli/commands.py`
- `packages/cli/engram_cli/cli_lifecycle_tests.py`
- `scripts/repository_layout.py`
- `docs/parity/claude-mem-parity-map.md`

The focused review covers native Claude Code hook-package trust boundaries,
generated hook commands, response-format separation between Claude Code and
Codex, local manifest secret hygiene, and unimplemented hook-event claims.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| TDD red CLI test run | Exit 1 before implementation. `claude-code` was not an accepted response format and connect manifests lacked response-format flags. |
| Focused CLI regression tests | Exit 0. The three new focused CLI tests passed after implementation. |
| Full CLI tests | Exit 0. `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` reported 30 tests OK. |
| Claude plugin contract tests | Exit 0. `python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v` reported 2 tests OK. |
| Codex plugin contract tests | Exit 0. `python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v` reported 2 tests OK. |
| Repository tests | Exit 0. `python3 -m unittest discover -s tests -v` reported 30 tests OK. |
| Repository layout | Exit 0. `python3 scripts/repository_layout.py` produced no output. |
| Repository text quality | Exit 0. `python3 scripts/repository_quality.py` produced no output. |
| Whitespace | Exit 0. `git diff --check HEAD` produced no output. |
| Independent read-only security review agent | SECURITY APPROVED. CRITICAL none, IMPORTANT none, MINOR none. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

None.

### MINOR

None.

## Fixes Applied

- `engram connect` now writes response-format flags into generated hook
  commands: Codex uses `--response-format codex`, Claude Code uses
  `--response-format claude-code`.
- Claude Code session-start output omits Codex-only top-level `continue` and
  emits `hookSpecificOutput.additionalContext` plus `systemMessage`.
- Claude Code non-session acknowledgements emit an empty JSON object instead of
  Codex-style top-level `continue`.
- `packages/claude-plugin` now has package-local contract tests that reject
  upstream product names in manifests and commands.

## Regression Tests Added

- CLI test coverage for runtime-specific connect hook commands.
- CLI test coverage for Claude Code session-start response shape.
- CLI test coverage for Claude Code non-session acknowledgement shape.
- Claude plugin contract tests for package files, required events, command
  shape, response format, and product-name hygiene.

## Accepted Risk

`UserPromptSubmit`, `PreToolUse`, and `Stop` remain intentionally deferred until
Engram has matching server contracts and worker behavior. The Claude Code
package in this checkpoint covers only `SessionStart`, `PostToolUse`, `Error`,
and `Decision`.
