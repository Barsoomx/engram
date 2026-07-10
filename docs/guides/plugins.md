# Plugins Guide

Engram ships two thin plugin packages that wire hook events into Claude Code and
Codex. Both call the `engram` CLI adapter, which posts events to the server.
Neither plugin runs a local worker, local database, vector store, or provider
SDK.

## Prerequisites

Before installing either plugin, make sure the Engram server is running (see
[../quickstart.md](../quickstart.md)). Then use the one-step installer:

```bash
uvx engram-connect install --agent codex \
  --server URL --api-key KEY --project PROJECT
```

`--agent` accepts `claude-code`, `codex`, or `both`. The command writes
`~/.engram/credentials.json` and `~/.engram/hooks/<runtime>.json`, verifies the
connection, and installs the selected native plugin. `engram connect` remains
available when plugin installation is managed separately.

The plugins read no secrets of their own; they delegate to the `engram hook`
adapter, which authenticates using the credential written by `engram connect`.

## Hook events

Event coverage differs between the two plugins:

| Adapter event | Server endpoint | Claude Code hook | Codex hook |
| --- | --- | --- | --- |
| `session-start` | `POST /v1/hooks/session-start` + `POST /v1/context/session-start` | `SessionStart` | `SessionStart` |
| `user-prompt-submit` | `POST /v1/hooks/user-prompt-submit` + `POST /v1/context/user-prompt-submit` | `UserPromptSubmit` | `UserPromptSubmit` |
| `post-tool-use` | `POST /v1/hooks/post-tool-use` | `PostToolUse` | `PostToolUse` |
| `session-end` | `POST /v1/hooks/session-end` | `SessionEnd` | `Stop` |

Codex has no native `Error`, `Decision`, or `SessionEnd` hook. Tool failures
are captured through `PostToolUse.tool_response`, and `Stop` is a turn-scoped
checkpoint rather than proof that the whole Codex session is over.

Each adapter call:

- reads the agent's hook JSON from stdin;
- attaches project id, team id, agent runtime, and version from local config;
- computes `event_id`, `idempotency_key`, `content_hash`, and `request_id` from
  a stable hash of the event material (replays are safe);
- posts the event with the scoped API key;
- prints a response shaped for the target agent.

## Claude Code plugin

Package: `packages/claude-plugin/`. The manifest lives at
`packages/claude-plugin/.claude-plugin/plugin.json`.

The plugin uses `engram hook ... --agent claude_code --response-format claude-code`.
For `SessionStart` and `UserPromptSubmit` the adapter returns a `systemMessage`
plus a `hookSpecificOutput.additionalContext` block; for `PostToolUse` and
`SessionEnd` it returns an empty object, which Claude Code ignores.

### Install

Run `engram install`. It connects Engram, adds the Engram marketplace
(`claude plugin marketplace add <source>`) and installs the plugin
(`claude plugin install engram@engram-marketplace`). The plugin bundles its own
copy of the CLI under `hooks/`, so each hook command runs
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py" hook ...` directly — a separate
`engram` install on `PATH` is not required for hooks or MCP tools to work.

See `packages/claude-plugin/README.md` for the package contract.

## Codex plugin

Package: `packages/codex-plugin/`. The native manifest lives at
`packages/codex-plugin/.codex-plugin/plugin.json`.

The hooks call the same thin Python CLI with `--agent codex` (or
`--response-format codex`). Codex hook responses use the `{"continue": true, ...}`
shape and must not emit fields Codex does not support.

The package uses Codex's native `hooks/hooks.json`, bundles the same connector
and six MCP tools as the Claude Code package, and ships the three shared memory
skills. Install it with the one-step command above or directly:

```bash
codex plugin marketplace add Barsoomx/engram --json
codex plugin add engram@engram-marketplace --json
```

Open `/hooks`, review the Engram commands, approve them if they match the
installed package, and start a new thread. Installation never bypasses Codex's
hook trust decision.

See `packages/codex-plugin/README.md` for the package contract.

## Managed / enterprise installation

For managed fleets, prefer distributing trusted hook configuration rather than
running the wizard per developer:

- Codex managed hooks can ship trusted hook configuration at scale.
- Claude Code hook configuration can be templated by an onboarding script.
- API keys should be scoped to team/project/service account and rotated from
  the admin UI (see [api-keys.md](api-keys.md)).

In managed mode the local command becomes a verifier: run `engram doctor` to
check hook files, server reachability, identity resolution, and whether the key
is scoped as expected.

Trust is not authorization. The server verifies API keys, scopes, request
binding, and replay protection on every call regardless of how hooks were
installed.

## See also

- [../agent-integrations.md](../agent-integrations.md) - hook protocol matrix
- [../client-installation.md](../client-installation.md) - installer design
- [cli.md](cli.md) - `engram connect`, `engram doctor`, `engram hook`
- [mcp.md](mcp.md) - optional MCP bridge for explicit memory tools
