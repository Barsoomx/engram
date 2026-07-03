# MCP Guide

Engram provides an MCP (Model Context Protocol) bridge so Claude Code, Claude
Desktop, and other MCP-aware clients can call memory operations as tools. The
bridge is a thin authenticated client of the server, not a local memory store:
every tool call goes to the server and is authorized by the same RBAC checks as
the HTTP API.

## Runtime contract

The MCP server:

- runs locally as a stdio JSON-RPC bridge (`engram mcp serve`);
- resolves server URL, API key, project id, and team id at call time (see
  [Configuration](#configuration));
- calls server APIs for every read and write;
- enforces server-side RBAC on every tool call;
- never stores local memory, embeddings, provider secrets, or curation state.

## Delivery

| Client                | How the bridge gets registered                          |
|------------------------|-----------------------------------------------------------|
| Claude Code            | Automatic, via the Claude Code plugin                    |
| Claude Desktop         | `engram mcp install --agent claude_desktop`               |
| Codex                  | Manual `config.toml` snippet (below); native support is deferred |
| Any other MCP client   | Point it at `engram mcp serve`                            |

### Claude Code

The Engram Claude Code plugin bundles the MCP server alongside its hook
runtime and registers it automatically when the plugin loads. `engram install`
installs the plugin and writes `~/.engram` credentials in one command, so
running it is the only step needed - there is no separate `mcp install` call
for Claude Code. See
[../../packages/claude-plugin/README.md](../../packages/claude-plugin/README.md).

### Claude Desktop and other manual setups

```bash
engram connect --server http://localhost:8000 --api-key egk_... --project <id>
engram mcp install --agent claude_desktop
```

`engram mcp install`:

- reads `~/.engram/config.json` and `~/.engram/credentials.json`, written by
  `engram connect` (or `engram install`);
- writes an `mcpServers.engram` entry into the target client's config file.
  `--agent claude_code` writes `~/.claude.json`, `--agent claude_desktop`
  writes the Claude Desktop config, and the default `--agent both` writes
  both;
- points the entry at the `engram` binary resolved from `PATH` when
  available, otherwise `<python> -m engram_cli mcp serve`;
- writes no `env` block and no API key into the client config. The bridge
  resolves credentials from `~/.engram` at call time, so rotating or revoking
  the API key never requires re-running `mcp install`.

`--claude-code-config PATH` and `--claude-desktop-config PATH` override the
target file. `--config-dir DIR` overrides the `~/.engram` root, matching
`engram connect`.

`engram mcp-install` (no space) still works as a deprecated alias for
`engram mcp install` with the same flags.

### Any other MCP client

Point the client's server command directly at:

```bash
engram mcp serve
```

It speaks MCP over stdio and exits cleanly on stdin close.

### Codex

Codex does not support installer-driven MCP registration yet. Add this to
`~/.codex/config.toml` by hand:

```toml
[mcp_servers.engram]
command = "engram"
args = ["mcp", "serve"]
```

## Configuration

`engram mcp serve` resolves each configuration value at call time, in this
order:

1. environment variables;
2. `~/.engram/config.json` and `~/.engram/credentials.json`, written by
   `engram connect` / `engram install`.

| Variable               | Description                                                |
|-------------------------|--------------------------------------------------------------|
| `ENGRAM_SERVER_URL`    | Server base URL, e.g. `http://localhost:8000`               |
| `ENGRAM_API_KEY`       | Scoped Engram API key (prefix `egk_`)                       |
| `ENGRAM_PROJECT_ID`    | Project id override (see the precedence ladder below)       |
| `ENGRAM_TEAM_ID`       | Team id (defaults to the key's bound team)                  |
| `ENGRAM_AGENT_RUNTIME` | Runtime tag reported to the server (default `codex`)         |

If the server URL or API key resolve from neither source, every tool returns a
plain-text message telling the caller to run `engram connect` or set
`ENGRAM_SERVER_URL`/`ENGRAM_API_KEY`.

### Project precedence ladder

All six tools resolve which project a call targets with the same ladder, in
order:

1. the tool's own optional `project_id` argument (a per-call override);
2. `ENGRAM_PROJECT_ID`;
3. `project_id` in `~/.engram/config.json` (written by `engram connect
   --project ...` - optional, `engram connect` works without it);
4. the repository derived from the current workspace (below), sent as
   `repository_url` instead of `project_id`.

Rung 4 is the default in the plug-and-play setup (org-wide agent key,
`engram connect` run without `--project`), and all six tools - including
`engram_memory_link`, `engram_observations`, `engram_memory_version`, and
`engram_memory_feedback` - work in that mode. The server always
re-authorizes whichever project a request resolves to, so no rung can expand
scope beyond the key's own binding. This is the one place the ladder is
documented; [mcp-tools.md](../mcp-tools.md), [cli.md](cli.md), and
[backend-contracts.md](../backend-contracts.md) reference it instead of
restating it.

### Repository derivation (rung 4)

When no project id resolves, the bridge derives `repository_url` from:

1. `CLAUDE_PROJECT_DIR`, if set - the workspace directory Claude Code passes
   to spawned stdio MCP servers (v2.1.139+). This is checked first because the
   MCP server process's own working directory is not guaranteed to be the
   workspace: for a user-scope plugin install it can be the plugin cache - a
   different git checkout whose `origin` would otherwise silently mis-route
   memory to the marketplace repository's project;
2. otherwise the server process's current working directory (correct for
   `engram mcp serve` launched manually, and for any other MCP runtime with no
   `CLAUDE_PROJECT_DIR` equivalent).

### Errors from repository-URL resolution

- **`404 project_not_found`** - no project in the organization matches the
  resolved `repository_url`. The bridge renders this as: "No Engram project
  exists for this repository yet - it is created on the first hook ingest."
  That guidance is accurate for an **org-wide** agent key (hooks ingest
  auto-creates the project on first use). A **project-bound** key gets the
  same 404 but the guidance does not apply to it - hooks ingest only
  auto-creates for unbound keys carrying `projects:agent`; a project-bound key
  needs the project created explicitly (or its own project's repository_url)
  before any tool call against it will succeed.
- **`403 project_scope_denied`** - the resolved project exists but is outside
  the key's binding (for example a project-scoped key sent the
  `repository_url` of a different in-org project). Surfaced through the same
  HTTP status/code/detail rendering the bridge already uses for any denial.

## Tool set

Six tools ship today. See [../mcp-tools.md](../mcp-tools.md) for the full
catalog, the mapping to the original conceptual tool names, and the tools that
are explicitly deferred.

| Tool                      | Server path                          |
|----------------------------|-----------------------------------------|
| `engram_search`           | `POST /v1/search/`                    |
| `engram_context`          | `POST /v1/context/session-start`      |
| `engram_memory_link`      | `POST /v1/memories/{id}/links`        |
| `engram_observations`     | `GET /v1/observations/`               |
| `engram_memory_version`   | `POST /v1/memories/{id}/version`      |
| `engram_memory_feedback`  | `POST /v1/memories/{id}/feedback`     |

## Authorization

- The API key identifies the actor and project/team binding.
- Tool arguments cannot expand organization/team/project scope - a `project_id`
  argument selects which project the call targets, it does not authorize
  access to it. The server's own scope resolution plus the membership guard
  (see [Errors from repository-URL resolution](#errors-from-repository-url-resolution))
  decide.
- The server filters results before returning memory or observation data.
- Mutating tools (`engram_memory_link`, `engram_memory_version`,
  `engram_memory_feedback`) require capability checks and pass a `request_id`
  for idempotency; the bridge generates a new one per call unless the caller
  supplies its own.

If a call is denied, the server returns the missing capability and a request
id; the bridge surfaces the HTTP status, error code, and detail as plain text.

## See also

- [../mcp-tools.md](../mcp-tools.md) - full tool catalog, deferred tools, and
  authorization model
- [../client-installation.md](../client-installation.md) - `engram connect` /
  `engram mcp install` design
- [cli.md](cli.md) - `engram connect` and credential files
- [api-keys.md](api-keys.md) - issuing a scoped key for the bridge
