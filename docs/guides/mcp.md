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
| `ENGRAM_PROJECT_ID`    | Project id the bridge is bound to                           |
| `ENGRAM_TEAM_ID`       | Team id (defaults to the key's bound team)                  |
| `ENGRAM_AGENT_RUNTIME` | Runtime tag reported to the server (default `codex`)         |

If the server URL or API key resolve from neither source, every tool returns a
plain-text message telling the caller to run `engram connect` or set
`ENGRAM_SERVER_URL`/`ENGRAM_API_KEY`.

If `ENGRAM_PROJECT_ID` is unset and there is no `project_id` in
`~/.engram/config.json`, `engram_search` and `engram_context` fall back to the
`repository_url` of the git repository in the server process's working
directory. `engram_memory_link`, `engram_observations`,
`engram_memory_version`, and `engram_memory_feedback` require an explicit
connected project - there is no repository fallback for those - and return a
plain-text message asking for `engram connect --project ...` or
`ENGRAM_PROJECT_ID` when one isn't resolved.

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
- Tool arguments cannot expand organization/team/project scope.
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
