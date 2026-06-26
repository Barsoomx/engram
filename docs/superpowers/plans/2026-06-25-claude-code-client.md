# Claude Code Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or an equivalent documented
> subagent-driven flow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the native Claude Code plugin package and response formatter for
the implemented Engram hook events.

**Architecture:** Keep plugin packaging under `packages/claude-plugin`, keep CLI
formatting under `packages/cli`, and update parity/security docs after tests.

**Tech Stack:** Python CLI, unittest, JSON hook manifests, Markdown docs.

## Global Constraints

- Do not touch backend worker behavior in this slice.
- Do not claim `UserPromptSubmit`, `PreToolUse`, or `Stop` are implemented.
- Do not reintroduce upstream product names in commands or manifests.
- Keep one git owner for branch operations and commit.

---

### Task 1: Claude Plugin Package Contract

**Files:**
- Create: `packages/claude-plugin/.claude-plugin/plugin.json`
- Create: `packages/claude-plugin/hooks/hooks.json`
- Create: `packages/claude-plugin/claude_plugin_contract_tests.py`
- Modify: `packages/claude-plugin/README.md`

**Interfaces:**
- Produces a native Claude Code package contract for `SessionStart`,
  `PostToolUse`, `Error`, and `Decision`.

- [ ] Write failing contract tests for package files, events, commands, and
      product-name hygiene.
- [ ] Add Claude package metadata and hook manifest.
- [ ] Run the focused package contract tests.

### Task 2: CLI Claude Response Format

**Files:**
- Modify: `packages/cli/engram_cli/main.py`
- Modify: `packages/cli/engram_cli/commands.py`
- Modify: `packages/cli/engram_cli/cli_lifecycle_tests.py`

**Interfaces:**
- Adds `--response-format claude-code`.
- Updates `engram connect` hook manifests to emit runtime-specific response
  formats.

- [ ] Write failing CLI tests for `claude-code` session response shape,
      non-session acknowledgement shape, and connect manifests.
- [ ] Implement response-format parsing and formatter behavior.
- [ ] Run focused CLI tests.

### Task 3: Docs, Security, And Verification

**Files:**
- Modify: `docs/parity/claude-mem-parity-map.md`
- Modify: `docs/verification-matrix.md`
- Create: `docs/security/reviews/2026-06-25-claude-code-client.md`

**Interfaces:**
- Records the current parity state and verification evidence.

- [ ] Update parity map without rewriting the historical first-gate report.
- [ ] Run focused plugin and CLI tests.
- [ ] Run repository verification that covers docs/package layout.
- [ ] Run a focused security review and record findings.
- [ ] Commit with `feat: add claude code client package`.
