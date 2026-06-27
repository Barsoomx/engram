# Plugins Guide

Engram ships two thin plugin packages that wire hook events into Claude Code and
Codex. Both call the `engram` CLI adapter, which posts events to the server.
Neither plugin runs a local worker, local database, vector store, or provider
SDK.

## Prerequisites

Before installing either plugin:

1. The Engram server is running (see [../quickstart.md](../quickstart.md)).
2. You ran `engram connect` so `~/.engram/credentials.json` and
   `~/.engram/hooks/<runtime>.json` exist (see [cli.md](cli.md)).

The plugins read no secrets of their own; they delegate to the `engram hook`
adapter, which authenticates using the credential written by `engram connect`.

## Hook events

Both plugins expose the same four events:

| Event         | CLI adapter                       | Server endpoint            |
|---------------|-----------------------------------|----------------------------|
| `SessionStart`| `engram hook session-start`       | `POST /v1/hooks/session-start` + `POST /v1/context/session-start` |
| `PostToolUse` | `engram hook post-tool-use`       | `POST /v1/hooks/post-tool-use` |
| `Error`       | `engram hook error`               | `POST /v1/hooks/error`     |
| `Decision`    | `engram hook decision`            | `POST /v1/hooks/decision`  |

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
For `SessionStart` the adapter returns a `systemMessage` plus a
`hookSpecificOutput.additionalContext` block; for the other events it returns an
empty object, which Claude Code ignores.

### Install

Reference the package directory from your Claude Code profile/plugin list, or
copy the hook manifest entries into your Claude Code settings. The manifest
points at `engram hook` for each event, so once `engram connect` has run, no
further configuration is needed.

See `packages/claude-plugin/README.md` for the package contract.

## Codex plugin

Package: `packages/codex-plugin/`. The fixture lives at
`packages/codex-plugin/.codex-plugin/plugin.json`.

The hooks call the same thin Python CLI with `--agent codex` (or
`--response-format codex`). Codex hook responses use the `{"continue": true, ...}`
shape and must not emit fields Codex does not support.

> **Status:** as of Phase C this package is a contract fixture. It is not
> published and does not install itself into a user profile. It documents the
> intended Codex hook wiring so a later checkpoint can publish it.

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
