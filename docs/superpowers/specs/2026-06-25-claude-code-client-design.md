# Claude Code Client Design

## Goal

Add the native Claude Code client package contract for the Engram hook events
that are already implemented end to end: `SessionStart`, `PostToolUse`,
`Error`, and `Decision`.

## Source Rule

The implementation intentionally mirrors the upstream Claude Code package shape
where it is useful, but replaces upstream local worker scripts with the Engram
CLI thin client. The package must not contain upstream private product names.

## Architecture

`packages/claude-plugin` becomes an active package with Claude plugin metadata,
a `hooks/hooks.json` manifest, and contract tests. Hook commands call
`engram hook ...` with `--agent claude_code` and
`--response-format claude-code`.

The CLI gains a Claude Code response formatter distinct from the Codex formatter.
For `SessionStart`, Claude Code receives `hookSpecificOutput.additionalContext`
and optional `systemMessage`. Codex keeps its top-level `continue` response
contract. Non-session Claude Code hook acknowledgements return an empty JSON
object so Engram does not emit Codex-only fields into Claude Code.

`engram connect` writes runtime-specific hook commands into local state. Codex
commands use `--response-format codex`; Claude Code commands use
`--response-format claude-code`.

## Explicit Non-Goals

This slice does not implement `UserPromptSubmit`, `PreToolUse`, or `Stop`
runtime hooks. Those remain deferred until Engram has matching server contracts
and worker behavior.

## Verification

Focused tests cover the new package contract, the CLI response formatter, and
runtime-specific connect manifests. A focused security review checks that hook
commands do not embed secrets, preserve agent/runtime separation, and do not
reintroduce upstream product naming.
