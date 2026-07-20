# Engram for Claude Code

Active native Claude Code plugin package for Engram hook capture and MCP
memory tools. The plugin registers Claude Code hook events and an MCP server,
both backed by a runtime bundled inside the plugin, and forwards everything to
the Engram server. It ships no local worker, no local database, and no
provider secret storage of its own.

## What this plugin does

Engram is shared engineering memory for AI coding agents. This plugin wires
Claude Code into that memory:

- Captures prompts, tool results, and session lifecycle activity by emitting
  hook events.
- Forwards each event to the Engram server via the bundled hook runtime
  (`hooks/hook.py`, which vendors the thin `engram_cli` client).
- Registers the bundled Engram MCP server (`hooks/mcp.py`) so Claude Code can
  call memory tools (search, context, link, observations, version, feedback,
  propose)
  directly during a session.
- Lets future Claude Code sessions recall relevant, server-backed context.

The plugin is a thin adapter. All persistence, retrieval, and secret handling
live in the Engram server.

## Prerequisites

1. `python3` >= 3.12 must be on `PATH`. The hook and MCP runtime is bundled
   with the plugin (`hooks/hook.py`, `hooks/mcp.py` + `hooks/engram_cli/`), so
   a separate `engram` CLI install is **not** required for hooks or MCP tools
   to work.
2. Local credentials must exist under `~/.engram` (`config.json`,
   `credentials.json`). These are written by `engram install` / `engram connect`
   (or by the dashboard **Connect agent** button, which prints the one-line
   install command). Until they exist, the hooks fail with the
   `missing_config` / `missing_credential` error, and MCP tools return a
   not-configured message.

## Install

Install the plugin from the Engram Claude Code marketplace (the canonical
manifest lives at the repo-root `.claude-plugin/marketplace.json`):

```bash
claude plugin marketplace add Barsoomx/engram
claude plugin install engram@engram-marketplace
```

`engram install` performs both steps and then writes `~/.engram` credentials in
one command. Or, from a local checkout of this repository:

```bash
claude plugin install /path/to/engram/packages/claude-plugin
```

The plugin manifest lives at `.claude-plugin/plugin.json`; Claude Code discovers
the hook manifest from the package's default `hooks/hooks.json` path.

## Configuration

The plugin reads no plugin-local configuration. All runtime configuration is
owned by `engram install` / `engram connect` and stored under `~/.engram`:

- `~/.engram/config.json` - server URL, project, team, runtimes.
- `~/.engram/credentials.json` - API key and credential fingerprint.

To reconfigure (new server, new project, rotated API key), rerun
`engram connect`. To revoke a credential, rotate or revoke the API key in the
Engram admin and rerun `engram connect`.

## Hook events

The plugin registers the following Claude Code hook events. Each event runs the
bundled runtime via
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py" hook <event> --agent claude_code --response-format claude-code`.

| Event              | Matcher           | Hook argument        | Timeout (s) |
| ------------------ | ----------------- | -------------------- | ----------- |
| `SessionStart`     | `startup\|resume\|clear\|compact` | `session-start` | 60 |
| `PostToolUse`      | `*`               | `post-tool-use`      | 120         |
| `SessionEnd`       | `*`               | `session-end`        | 60          |
| `UserPromptSubmit` | `*`               | `user-prompt-submit` | 60          |

Hook lifecycle ordering: `SessionStart → UserPromptSubmit* → PostToolUse* → SessionEnd`.

`UserPromptSubmit` fires on every user prompt: it records the prompt as an observation and injects a fresh per-turn context bundle (`additionalContext`) into the agent.

Claude Code has no registered Engram `Error` or `Decision` hooks. Tool failures
are captured in `PostToolUse`; decisions are learned from prompts and tool/session
observations instead of an invented native event.

### How hooks call the bundled runtime

Every hook entry is a `command`-type hook that runs
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py"`. `hook.py` puts its own
directory on `sys.path` and dispatches to the vendored `engram_cli` client,
which:

1. Loads `~/.engram/config.json` and `~/.engram/credentials.json`.
2. POSTs the hook payload to the Engram server
   (`/v1/hooks/<event>`, and `/v1/context/session-start` for `SessionStart`).
3. Prints the server response in the `claude-code` response format on stdout,
   so Claude Code can inject context or instructions.

The bundled `hooks/engram_cli/` is kept in lockstep with the source at
`packages/cli/engram_cli/` by `scripts/sync_plugin_bundle.py` (guarded by
`bundle_sync_tests.py`). The plugin never stores provider secrets and never
opens a database connection.

## MCP tools

The plugin ships an `.mcp.json` at plugin root that registers the bundled
Engram MCP server with Claude Code automatically - no separate
`engram mcp install` step is needed for Claude Code. The entry runs
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"`, a `sys.path` shim (mirroring
`hooks/hook.py`) that starts the vendored `engram_cli` MCP server over stdio.

Seven tools are exposed: `engram_search`, `engram_context`,
`engram_memory_link`, `engram_observations`, `engram_memory_version`,
`engram_memory_feedback`, and `engram_memory_propose`. Each resolves server URL, API key, and project/team
scope from `~/.engram` the same way the hooks do, and calls the Engram server
under the connected project's RBAC scope. See
[../../docs/guides/mcp.md](../../docs/guides/mcp.md) and
[../../docs/mcp-tools.md](../../docs/mcp-tools.md) for the full contract.

To register the same MCP server for Claude Desktop or another MCP client, run
`engram mcp install --agent claude_desktop`, or point the client at
`engram mcp serve` directly.

## Versioning

The plugin version is declared in `.claude-plugin/plugin.json` (`version`
field). Bump that field in lockstep with the marketplace entry in the repo-root
`.claude-plugin/marketplace.json` when publishing a new release
(`bundle_sync_tests.py` asserts the two match). See `plugin-repository/README.md`
for the release and versioning contract.
