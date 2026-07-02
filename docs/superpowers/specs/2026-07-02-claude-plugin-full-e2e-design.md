# Full-contract Claude Code plugin E2E

Date: 2026-07-02
Status: approved (user-specified 7-step scope)

## Goal

One script that proves the entire plug-and-play contract end to end with a
REAL Claude Code binary and a REAL backend, catching the bug classes that
unit suites and the existing golden path missed:

- empty observation body / null observed_at (CLI never synthesized content);
- plugin manifest/loader failures (invalid hook events, duplicate hooks file);
- payload contract violations (blank payload, oversized tool_input);
- queue not draining / distillation not firing.

## Steps (user spec)

1. Start the real backend (deploy/compose, same images as CI Compose E2E).
2. Bootstrap objects: `engram_bootstrap_golden_path` extended with an
   org-wide agent key (`--agent-key`, capabilities memories:read,
   observations:write, search:query, projects:agent) — mirrors the dashboard
   Connect-agent flow.
3. Ensure `claude` CLI is available (install via npm when E2E_INSTALL_CLAUDE=1).
4. Install the plugin from THIS checkout: isolated HOME, `claude plugin
   marketplace add <repo-root>`, `claude plugin install
   engram@engram-marketplace`. Loader errors fail the run.
5. Mock Anthropic server (stdlib http.server): serves /v1/messages (SSE and
   non-streaming), scripted turns emitting tool_use blocks (Read, Write,
   Bash), logs every request to requests.jsonl (traffic sniffing), accepts
   only the fake API key.
6. Run `claude -p <prompt> --dangerously-skip-permissions` in a temp git repo
   (remote origin set → repo-url routing must auto-create the Project) with
   ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY pointed at the mock.
7. Verify in the backend DB: project auto-created by canonical repo url;
   observations exist with NON-EMPTY body, observed_at set, files_read /
   files_modified populated; raw event payloads non-empty; memory candidates
   produced (queue drained); audit events written; no secret leaked into the
   mock traffic log; mock saw only the fake key.

## Non-goals

- Real LLM providers (server stays in fake provider mode for determinism).
- Codex runtime parity (Claude Code only).
- CI wiring in this slice (script must be CI-able; workflow wiring follows
  once stable locally).

## Files

- `scripts/e2e_claude_plugin.py` — orchestrator (stdlib only).
- `scripts/mock_anthropic_server.py` — mock Anthropic Messages API.
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
  — `--agent-key` option.
