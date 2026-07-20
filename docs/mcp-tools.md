# MCP Tools

## Goal

Engram ships an MCP server for local agent and team-lead workflows. This MCP
server is not a local memory worker. It is a thin authenticated client that
exposes server-side memory operations to Claude Code, Claude Desktop, Codex,
and local operator sessions. See [guides/mcp.md](guides/mcp.md) for delivery
and configuration.

## Runtime Contract

The MCP server:

- runs locally as a stdio JSON-RPC bridge;
- stores only server URL, an optional project id override, and scoped agent
  credential metadata (resolved from environment variables, `~/.engram`, or a
  git-derived `repository_url` per the precedence ladder in
  [guides/mcp.md](guides/mcp.md#project-precedence-ladder), never persisted by
  the bridge itself);
- calls server APIs for every read and write;
- enforces server-side RBAC on every tool call;
- never stores local memory, embeddings, provider secrets, or curation state.

## Shipped Tool Set (V1)

Six tools ship in `engram mcp serve`
(`packages/cli/engram_cli/mcp_tools.py` + `mcp_server.py`), delivered
automatically with the Claude Code plugin, via `engram mcp install` for Claude
Desktop, or directly over stdio for any other client.

| Tool                      | Maps to conceptual tool                        | Description                                                    |
|----------------------------|--------------------------------------------------|--------------------------------------------------------------------|
| `engram_search`           | `memory.search`                                 | hybrid exact + semantic search over authorized memory             |
| `engram_context`          | `memory.context`                                | session-start context bundle for the resolved project              |
| `engram_memory_link`      | shipped extra, beyond the original catalog      | attach a file/symbol/commit/issue link to an approved memory      |
| `engram_observations`     | shipped extra, beyond the original catalog      | list recent observations for the resolved project                |
| `engram_memory_version`   | shipped extra, beyond the original catalog      | update an approved memory body, creating a new reviewed version   |
| `engram_memory_feedback`  | `memory.feedback` (subset: `stale`/`refuted`/`confirmed`) | mark an injected memory stale/refuted, or confirm it is still accurate, with a reason |

All six are developer-scoped. Any actor whose API key resolves read/write
capability for the target memory can call them, except
`engram_memory_feedback`, which requires the `memories:review` capability (a read/write key alone gets 403);
there is no separate lead/curator tool set yet. All six also accept an optional per-call
`project_id` argument and fall back to a repository-derived project when
neither it nor `ENGRAM_PROJECT_ID`/config resolve one - see
[guides/mcp.md](guides/mcp.md#project-precedence-ladder) for the ladder.

`engram_search` renders each result line as
`[<citation>] <title> (memory_id=<id>) [<kind>, conf <confidence>]`. The
trailing ` [kind, conf X]` suffix is omitted when a field is absent (for
example a memory with no recorded confidence renders ` [gotcha]` only).

These tools should feel seamless. Developers should not need to understand the
curation pipeline to benefit from memory.

## Deferred

Not shipped in V1. The conceptual description is kept for future planning,
with the reason each is deferred:

- `memory.observe` - submit an explicit observation. Hooks already submit
  observations automatically on every tool call; no MCP-specific gap has been
  identified yet.
- `memory.propose` - propose a memory update for review, distinct from the
  direct write `engram_memory_version` performs today. Superseded by
  `engram_memory_version` for V1; a review-gated propose flow can follow if
  direct writes prove too permissive in practice.
- `memory.explain` - explain why a memory was returned or excluded from a
  bundle. No ranking-explanation endpoint exists server-side yet.
- `team.digest.latest` - show the latest team/project digest. Lead/curator
  tool, requires Admin/Owner/curator capability gating that has not been
  designed for MCP yet.
- `team.digest.range` - list digest summaries for a date range. Same as
  `team.digest.latest`.
- `memory.contradictions` - list contradictory/refuted memories needing
  action. Lead/curator tool, deferred with the rest of the curator set.
- `memory.escalations` - list curator escalations awaiting human decision.
  Lead/curator tool, deferred with the rest of the curator set.
- `memory.resolve` - approve, reject, archive, narrow, or supersede an
  escalated memory. Lead/curator tool, deferred with the rest of the curator
  set.
- `memory.audit` - inspect memory reads/writes and curator decisions by
  request id, actor, team, project, or date range. Lead/curator tool,
  deferred with the rest of the curator set.
- `memory.simulate_retrieval` - replay retrieval for an actor/query/scope.
  Lead/curator tool, deferred with the rest of the curator set.
- `hooks.doctor` - verify hook health for a team/project. `engram doctor`
  already covers this from the CLI; no MCP-specific gap has been identified
  yet.

The lead/curator tools should default to generated summaries and recommended
actions, not raw observation streams, whenever they ship.

## Authorization

MCP tools use the same effective access algorithm as the API and admin UI:

- API key or agent token identifies actor and project binding.
- Tool arguments cannot expand organization/team/project scope.
- Server filters before returning memory, digest, audit, or contradiction
  data.
- Mutating tools require capability checks and an audit reason.

If a lead asks for cross-team data, the server returns only teams and projects
the actor can access. Denials include the missing capability and request id.

Repository-URL-derived calls carry the same guarantee: the shared resolver
resolves the project inside the key's own organization only, then checks it
against the key's binding before any query runs. A `repository_url` outside
the key's binding is denied `403 project_scope_denied` (a DENIED audit event
records the resolved project id); a `repository_url` matching no project in
the organization returns `404 project_not_found`. See
[guides/mcp.md](guides/mcp.md#errors-from-repository-url-resolution) for the
guidance text each tool renders for these.
