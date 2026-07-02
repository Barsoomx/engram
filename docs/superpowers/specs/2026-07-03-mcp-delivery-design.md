# MCP Delivery And Consolidation Design

Date: 2026-07-03
Status: proposed (design presented in-session; user approval pending at spec
review gate)
Owner: engram core

## Problem

The MCP bridge exists (`packages/mcp/engram_mcp`: stdio JSON-RPC 2.0 server,
5 tools, 12 contract tests) but is undeliverable and invisible:

- G1 (blocker): `engram mcp-install` registers `python -m engram_mcp`, but
  `engram_mcp` is not installable anywhere: not a dependency of the published
  `engram-connect` dist, not published to PyPI, not bundled into the Claude
  plugin (`scripts/sync_plugin_bundle.py` syncs only `engram_cli`). Fresh
  machines get `ModuleNotFoundError`. Entry also uses bare `python`.
- G2: the plug-and-play flow (`engram install`, `uvx engram-connect install`
  one-liner from the dashboard Connect modal) never registers MCP; the only
  path is a second, undocumented manual command.
- G3: zero e2e coverage: no test launches the MCP server as a process or
  drives its stdio protocol; `client.py` (the whole HTTP layer) has no tests.
- G4: docs contradict reality: `docs/guides/mcp.md` says "not yet
  implemented"; documents `engram mcp install` (CLI implements `mcp-install`);
  `docs/mcp-tools.md` promises a differently named tool catalog; key prefix
  documented as `sk-engram_` while real keys use `egk_`.
- G5: the MCP client hard-requires `ENGRAM_PROJECT_ID` and cannot use the
  `repository_url` routing the CLI already supports; it also duplicates
  HTTP/config code instead of reusing `engram_cli.http`/`engram_cli.config`.
- G6 (verified): `update_memory_version` reads `version`/`reason` from a
  response that contains `current_version`/`memory_version_id`
  (`apps/backend/engram/memory/services.py:731-739`) and prints
  `version=None`.
- G7: fixed default `request_id` values (`mcp-link-{memory_id}`) collide with
  backend idempotency: a second distinct call silently replays the first
  result instead of applying.
- G8: `tools/call` invokes tool functions unguarded (`server.py:123`); an
  exception kills the stdio loop.
- G9: version skew: `engram-mcp` 0.1.0 never moved, no publish workflow.

## Decision

Approach A: merge the MCP bridge into the CLI package and deliver it through
the channels that already exist.

Rejected alternatives:

- B: separate `engram-mcp` PyPI dist. Second publish chain, duplicated
  HTTP/config/routing code, persistent version skew, second install step for
  users.
- C: rewrite on the official `mcp` SDK. Breaks the deliberate zero-dependency
  CLI (fast `uvx`, minimal supply chain); the hand-rolled ~150-line server is
  tested and sufficient for a 6-tool stdio bridge. Revisit when
  resources/prompts/notifications are needed; record as a deferred decision.

## Architecture

### Code move

- `packages/mcp/engram_mcp/server.py` -> `packages/cli/engram_cli/mcp_server.py`
  (JSON-RPC loop, tool schemas, dispatch).
- `packages/mcp/engram_mcp/client.py` -> `packages/cli/engram_cli/mcp_tools.py`
  (tool handlers), rewritten on top of `engram_cli.http` and
  `engram_cli.config` instead of raw env-only `urllib`.
- Flat top-level modules on purpose: `scripts/sync_plugin_bundle.py` copies
  top-level non-test `.py` files into the plugin bundle, so the plugin picks
  the MCP code up with no sync-script changes.
- `packages/mcp/` is deleted (README replaced by a pointer note in the same
  commit that removes the package).

### Configuration resolution

Per-value precedence, resolved at call time:

1. env vars `ENGRAM_SERVER_URL`, `ENGRAM_API_KEY`, `ENGRAM_PROJECT_ID`,
   `ENGRAM_TEAM_ID`, `ENGRAM_AGENT_RUNTIME`;
2. fallback to `~/.engram/config.json` + `~/.engram/credentials.json` via the
   existing `engram_cli.config` loaders.

Consequences:

- plugin-registered MCP needs no env block at all;
- `engram mcp install` no longer copies the API key into agent config files
  (`~/.claude.json`, Claude Desktop config) — the key stays in
  `~/.engram/credentials.json` (0600), which is a security improvement and
  makes key rotation not break MCP;
- `project_id` becomes optional: when absent, tools fall back to
  `repository_url` derived from git in the server process cwd, matching CLI
  search/hook behavior. If neither resolves, the tool returns the existing
  "not configured" guidance string.

### Tool set (V1 as shipped)

Existing five, unchanged names: `engram_search`, `engram_context`,
`engram_memory_link`, `engram_observations`, `engram_memory_version`.

Added: `engram_memory_feedback` (memory_id, action `stale|refuted`, reason)
calling the existing `POST /v1/memories/{id}/feedback`.

Explicitly deferred (recorded in `docs/mcp-tools.md`): curator/lead tools
(`team.digest.*`, `memory.contradictions`, `memory.escalations`,
`memory.resolve`, `memory.audit`, `memory.simulate_retrieval`),
`hooks.doctor`, `memory.observe`, `memory.propose`, `memory.explain`.

### Behavior fixes

- `engram_memory_version` output reads `current_version` and
  `memory_version_id`.
- Default `request_id` is `mcp-<uuid4>` per invocation; explicit
  `request_id` argument still wins (callers who want replay semantics can
  pass one).
- `tools/call` wraps handler execution; exceptions become a JSON-RPC error
  response (or `isError` content) instead of killing the loop.
- Registration entries use `python3`/resolvable commands, never bare
  `python`.

### Delivery channels

1. Claude Code (plug-and-play): the plugin ships `.mcp.json` at plugin root:

   ```json
   {
     "mcpServers": {
       "engram": {
         "command": "python3",
         "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"]
       }
     }
   }
   ```

   `hooks/mcp.py` is a sys.path shim mirroring `hooks/hook.py` that starts
   the bundled server. Claude Code starts plugin MCP servers automatically,
   so `engram install` (which installs the plugin) now delivers hooks and
   MCP in one step. Answering the original question: yes, after this change
   the bootstrap one-liner installs MCP.
2. Claude Desktop and manual setups: `engram mcp install` writes
   `mcpServers.engram` with a resolvable command: `engram` from PATH when
   available, else `sys.executable -m engram_cli mcp serve`; no `env` block
   (config-file fallback covers it).
3. Codex: deferred; `docs/guides/mcp.md` gets a manual `config.toml`
   snippet. Rationale: dependency-free TOML writing is a separate slice.

### CLI surface

- New command group: `engram mcp install` and `engram mcp serve` (matches
  the documented shape).
- `engram mcp-install` remains as a hidden deprecated alias dispatching to
  the same handler.
- `engram install` output mentions MCP delivery via the plugin.

## Testing

TDD per slice; test files live next to modules as `<module>_tests.py`,
pytest functions, typed fixtures, stubs over mocks for handler tests.

- `mcp_server_tests.py`: port the 12 JSON-RPC contract tests from unittest
  to pytest; add: unknown tool, handler raising -> JSON-RPC error, loop
  survives bad input line.
- `mcp_tools_tests.py`: handler tests against a stub HTTP transport: payload
  shapes for all six tools, config resolution order (env over file over
  absent), repository_url fallback, response rendering including
  `current_version`/`memory_version_id`, unique `request_id` per call.
- CLI tests: `mcp install`/`mcp serve` parsing, deprecated alias, written
  config entries (adapt the existing 11 `mcp-install` tests), no-API-key-in-
  entry assertion.
- e2e (compose golden path, `scripts/e2e_golden_path.py`): launch
  `engram mcp serve` as a subprocess, drive stdio: `initialize` ->
  `tools/list` -> `tools/call` for all six tools against the live backend;
  assert non-error, content-bearing results, and that version/link calls
  actually apply (no silent replay).
- e2e (claude plugin, `scripts/e2e_claude_plugin.py`): after
  `claude plugin install`, assert the engram MCP server is registered and
  responsive in the Claude CLI environment.
- Bundle guard: `sync_plugin_bundle --check` (existing) covers the new
  modules automatically; plugin contract test asserts `.mcp.json` exists and
  points at a bundled file.

## Documentation

- Rewrite `docs/guides/mcp.md`: implemented status, real commands, real tool
  names, env vars + config fallback, plugin auto-registration, `egk_` key
  prefix, Codex manual snippet.
- Update `docs/mcp-tools.md`: shipped V1 set with actual `engram_*` names
  mapped to the conceptual catalog; deferred list with rationale.
- Touch `docs/client-installation.md` and `docs/quickstart.md` where they
  reference `mcp install`/bridge state.
- `README.md` MCP mention aligned.

## Versioning And Packaging

- `engram-connect` 0.1.4 -> 0.2.0 (new feature surface).
- Claude plugin 0.1.7 -> 0.1.8 + marketplace version sync (guarded by
  existing bundle tests).
- Delete `packages/mcp`; no new publish workflows needed (the existing
  `publish-pypi.yml` for `engram-connect` now ships the MCP bridge).

## Security Notes

- API key removed from agent config files (env block dropped); key remains
  only in `~/.engram/credentials.json` (0600) and server-side.
- MCP tools authenticate to the backend with the same scoped key as the CLI;
  server-side RBAC (`memories:read`, `memories:review`, etc.) is unchanged
  and enforced per call.
- Tool output rendering passes through backend redaction (`redact_value`)
  as today; the bridge stores nothing locally.
- Run the standing security review checklist before merge (tenant isolation,
  key scope, no secret leakage in logs/errors) per repo cadence.

## Acceptance Criteria

1. Fresh-machine flow works: `uvx engram-connect install ...` then opening
   Claude Code yields a working `engram` MCP server (tools listed and
   callable) with no extra steps.
2. `engram mcp install` on a machine with `engram` on PATH writes an entry
   that resolves and starts; the written entry contains no API key.
3. All six tools round-trip against a live backend in compose e2e over real
   stdio.
4. `engram_memory_version` reports the new version number; two consecutive
   distinct `engram_memory_link`/`engram_memory_version` calls both apply
   (no silent replay).
5. A handler exception does not terminate the server process.
6. `packages/mcp` is gone; `sync_plugin_bundle --check` and plugin contract
   tests pass; docs match shipped commands and tool names.
7. Backend test suite, CLI tests, both e2e workflows green in CI.
