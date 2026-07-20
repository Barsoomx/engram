# Engram for Codex

Native Codex plugin for Engram's shared engineering memory. It bundles the thin
Engram connector, four lifecycle hooks, eight MCP tools, and three memory skills.

## Install

Connect Engram and install the plugin in one step:

```bash
uvx engram-connect install --agent codex \
  --server URL --api-key KEY --project PROJECT
```

Or install through the Codex marketplace:

```bash
codex plugin marketplace add Barsoomx/engram --json
codex plugin add engram@engram-marketplace --json
```

Codex requires review for changed non-managed hooks. Open `/hooks`, review the
Engram commands, approve them if they match this package, and start a new
thread.

## Lifecycle

| Codex event | Engram command |
| --- | --- |
| `SessionStart` | `hook session-start` |
| `UserPromptSubmit` | `hook user-prompt-submit` |
| `PostToolUse` | `hook post-tool-use` |
| `Stop` | `hook session-end` |

`Stop` is a turn-completion checkpoint. A later Codex turn reactivates the
same Engram session; the plugin does not treat it as a native `SessionEnd`
event. Hook HTTP calls keep an explicit 10-second network timeout inside the
larger Codex handler budgets.

The bundled MCP bridge exposes `engram_search`, `engram_context`,
`engram_memory_link`, `engram_observations`, `engram_memory_version`,
`engram_memory_feedback`, `engram_memory_get`, and `engram_audit` without a
separate MCP installation. `engram_audit` needs a resolved `project_id` and has
no repository-URL fallback; the other seven accept a repository-derived project.

Codex starts bundled MCP processes from its plugin cache. Engram scopes each
tool call from Codex's matching per-turn workspace metadata instead of that
cache directory. Explicit project scope still wins, and missing, mismatched, or
ambiguous metadata fails closed rather than selecting another repository.

## Security and local state

The plugin reads the existing Engram connection from:

- `~/.engram/config.json` - server URL, project, team, runtimes.
- `~/.engram/credentials.json` - API key and credential fingerprint.

It does not store provider secrets, a memory database, a vector index, a local
worker, or a durable event queue. Hook trust remains controlled by Codex, and
every MCP or hook request remains subject to Engram server authorization.
