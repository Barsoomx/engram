# MCP Guide

Engram provides an MCP (Model Context Protocol) bridge so Claude Code and other
MCP-aware clients can call memory operations as tools. The bridge is a thin
authenticated client of the server, not a local memory store: every tool call
goes to the server and is authorized by the same RBAC checks as the HTTP API.

> **Implementation status (Phase C):** the CLI subcommand `engram mcp install`
> and the `engram_mcp` server module are specified in
> [../client-installation.md](../client-installation.md) and
> [../mcp-tools.md](../mcp-tools.md) but are **not yet implemented** in this
> checkout. This guide documents the intended behavior so you can plan around
> it. When the bridge lands, the commands and env vars below are the contract.

## Runtime contract

The MCP server:

- runs locally as a lightweight stdio (or HTTP) bridge;
- stores only server URL, project id, and a scoped agent credential;
- calls server APIs for every read and write;
- enforces server-side RBAC on every tool call;
- never stores local memory, embeddings, provider secrets, or curation state.

## Launching the server manually

When implemented, the bridge is launched as a Python module:

```bash
python -m engram_mcp
```

Required environment:

| Variable              | Required | Description                                       |
|-----------------------|----------|---------------------------------------------------|
| `ENGRAM_SERVER_URL`   | yes      | Server base URL, e.g. `http://localhost:8000`     |
| `ENGRAM_API_KEY`      | yes      | Scoped Engram API key (prefix `sk-engram_`)       |
| `ENGRAM_PROJECT_ID`   | yes      | Project id the bridge is bound to                 |
| `ENGRAM_TEAM_ID`      | no       | Team id (defaults to the key's bound team)        |
| `ENGRAM_AGENT_RUNTIME`| no       | `codex` or `claude_code` for telemetry            |

The server speaks MCP over stdio by default and exits cleanly on stdin close.

## Registering the bridge with `engram mcp install`

The intended one-command registration writes the MCP client config into your
agent profile using the same credentials written by `engram connect`:

```bash
engram connect --server http://localhost:8000 --api-key sk-engram_... --project <id>
engram mcp install
```

`engram mcp install` reads `~/.engram/config.json` and `~/.engram/credentials.json`
and registers a stdio server entry pointing at `python -m engram_mcp` with the
required environment populated. It does not create a local database or worker.

> Until `engram mcp install` ships, register the bridge manually in your MCP
> client config (for example Claude Code's `~/.config/claude-code/mcp.json`)
> with the command `python -m engram_mcp` and the env vars above.

## Tool set

These are the V1 developer-facing tools (see
[../mcp-tools.md](../mcp-tools.md) for the full lead/curator set):

| Tool               | Server path                    | Capability gate       | Description                          |
|--------------------|--------------------------------|-----------------------|--------------------------------------|
| `memory.search`    | `POST /v1/search/`             | `memories:read`       | Hybrid semantic + full-text search   |
| `memory.context`   | `POST /v1/context`             | `memories:read`       | Build a task context bundle          |
| `memory.observe`   | `POST /v1/hooks/post-tool-use` | `observations:write`  | Submit an explicit observation       |
| `memory.propose`   | `POST /v1/memories/{id}/version` | `memories:propose`  | Propose a memory update              |
| `memory.feedback`  | `POST /v1/memories/{id}/feedback` | (any memory reader) | Mark injected memory useful/stale/wrong |

Lead/curator tools (`team.digest.latest`, `memory.contradictions`,
`memory.escalations`, `memory.resolve`, `memory.audit`,
`memory.simulate_retrieval`, `hooks.doctor`) require Admin/Owner/curator
capabilities and are filtered server-side by the key's scope.

## Authorization

- The API key identifies the actor and project/team binding.
- Tool arguments cannot expand organization/team/project scope.
- The server filters results before returning memory, digest, audit, or
  contradiction data.
- Mutating tools require capability checks and an audit reason.

If a lead requests cross-team data, the server returns only teams and projects
the actor can access. Denials include the missing capability and a request id.

## See also

- [../mcp-tools.md](../mcp-tools.md) - full tool catalog and authorization model
- [../client-installation.md](../client-installation.md) - installer design
- [cli.md](cli.md) - `engram connect` and credential files
- [api-keys.md](api-keys.md) - issuing a scoped key for the bridge
