# Engram for Codex

Native Codex plugin package for Engram hook capture. The plugin registers Codex
hook events and forwards them to the Engram server through the thin `engram`
CLI. It ships no local worker, no local database, and no provider secret storage
of its own.

## What this plugin does

Engram is shared engineering memory for AI coding agents. This plugin wires
Codex into that memory:

- Captures coding-session activity (tool calls, decisions, errors, session
  lifecycle) by emitting hook events.
- Forwards each event to the Engram server via `engram hook <event>`.
- Lets future Codex sessions recall relevant, server-backed context.

The plugin is a thin adapter. All persistence, retrieval, and secret handling
live in the Engram server and the `engram` CLI.

## Prerequisites

1. The `engram` CLI must be installed and on `PATH`. Verify with
   `engram --version`.
2. Run `engram connect` once before installing or using the plugin. The wizard
   writes credentials and per-runtime hook manifests under `~/.engram`
   (`config.json`, `credentials.json`, and one `hooks.<runtime>.json` per
   selected runtime). The plugin commands fail with the `missing_hook_config`
   error until `engram connect` has succeeded.
3. (Optional) Register the Engram MCP server with
   `engram mcp install --runtime codex` so Codex can query Engram memory
   directly during a session.

## Install

Install the plugin from a Codex plugin marketplace (see
`plugin-repository/README.md` for the marketplace manifest format):

```bash
codex plugin install engram@<marketplace-name>
```

Or, from a local checkout of this repository:

```bash
codex plugin install /path/to/engram/packages/codex-plugin
```

The plugin manifest lives at `.codex-plugin/plugin.json` and points its
`hooks` field at `../plugin/hooks/codex-hooks.json`.

## Configuration

The plugin reads no plugin-local configuration. All runtime configuration is
owned by `engram connect` and stored under `~/.engram`:

- `~/.engram/config.json` - server URL, project, team, runtimes.
- `~/.engram/credentials.json` - API key and credential fingerprint.
- `~/.engram/hooks.<runtime>.json` - per-runtime hook command manifest.

To reconfigure (new server, new project, rotated API key), rerun
`engram connect`. To revoke a credential, rotate or revoke the API key in the
Engram admin and rerun `engram connect`.

## Hook events

The plugin registers the following Codex hook events. Each event calls
`engram hook <event> --agent codex --response-format codex`.

| Event         | Matcher            | Command                                                                  | Timeout (s) |
| ------------- | ------------------ | ------------------------------------------------------------------------ | ----------- |
| `SessionStart` | `startup\|resume`  | `engram hook session-start --agent codex --response-format codex`        | 60          |
| `PostToolUse`  | `.*`               | `engram hook post-tool-use --agent codex --response-format codex`        | 120         |
| `Error`        | `.*`               | `engram hook error --agent codex --response-format codex`                | 60          |
| `Decision`     | `.*`               | `engram hook decision --agent codex --response-format codex`             | 60          |

### How hooks call the CLI

Every hook entry is a `command`-type hook that invokes the `engram` CLI as a
thin client. The CLI:

1. Loads `~/.engram/config.json` and `~/.engram/credentials.json`.
2. Reads the corresponding `~/.engram/hooks.codex.json` manifest produced by
   `engram connect`.
3. POSTs the hook payload to the Engram server
   (`/v1/hooks/<event>`, and `/v1/context/session-start` for `SessionStart`).
4. Prints the server response in the `codex` response format on stdout, so
   Codex can inject context or instructions.

The plugin never stores provider secrets and never opens a database connection.

## Versioning

The plugin version is declared in `.codex-plugin/plugin.json` (`version`
field). Bump that field in lockstep with the marketplace entry in
`plugin-repository/.claude-plugin/marketplace.json` when publishing a new
release. See `plugin-repository/README.md` for the release and versioning
contract.
