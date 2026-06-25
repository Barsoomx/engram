# MCP Tools

## Goal

`claudex-teams` needs an MCP server for local agent and team-lead workflows.
This MCP server is not a local memory worker. It is a thin authenticated client
that exposes server-side memory operations to Claude Code, Codex, and local
operator sessions.

Developers should get seamless memory search and update tools. Leads and
responsible owners should get tools for the AI-generated memory workflow: inspect
contradictions, review escalations, resolve important conflicts, and query team
digests when their RBAC scope allows it.

## Runtime Contract

The MCP server:

- runs locally as a lightweight stdio or HTTP bridge;
- stores only server URL, project id, and scoped agent credential metadata;
- calls server APIs for every read and write;
- enforces server-side RBAC on every tool call;
- never stores local memory, embeddings, provider secrets, or curation state;
- reports request id, trace id, actor, team, project, and effective scopes in
  debug output when asked.

## Developer Tools

Developer-scoped tools:

- `memory.search`: hybrid exact + semantic search over authorized memory.
- `memory.context`: get the current session/project context bundle.
- `memory.observe`: submit an explicit observation.
- `memory.propose`: propose a memory update under the current project scope.
- `memory.feedback`: mark injected memory useful, stale, wrong, or irrelevant.
- `memory.explain`: explain why a memory was returned or excluded.

These tools should feel seamless. Developers should not need to understand the
curation pipeline to benefit from memory.

## Lead And Curator Tools

Lead-scoped tools require Admin, Owner, or explicit Auditor/curator capability:

- `team.digest.latest`: show the latest team/project digest.
- `team.digest.range`: list digest summaries for a date range.
- `memory.contradictions`: list contradictory/refuted memories needing action.
- `memory.escalations`: list curator escalations awaiting human decision.
- `memory.resolve`: approve, reject, archive, narrow, or supersede an escalated
  memory.
- `memory.audit`: inspect memory reads/writes and curator decisions by request
  id, actor, team, project, or date range.
- `memory.simulate_retrieval`: replay retrieval for an actor/query/scope.
- `hooks.doctor`: verify hook health for a team/project.

The tools should default to generated summaries and recommended actions, not raw
observation streams.

## Authorization

MCP tools use the same effective access algorithm as the API and admin UI:

- API key or agent token identifies actor and project binding.
- Tool arguments cannot expand organization/team/project scope.
- Server filters before returning memory, digest, audit, or contradiction data.
- Mutating tools require capability checks and audit reason.

If a lead asks for cross-team data, the server returns only teams and projects
the actor can access. Denials include the missing capability and request id.

## V1 Tool Set

V1 must include:

- `memory.search`;
- `memory.context`;
- `memory.feedback`;
- `team.digest.latest`;
- `memory.contradictions`;
- `memory.escalations`;
- `memory.resolve`;
- `hooks.doctor`.

Later tools:

- bulk policy-pack operations;
- legal hold and export;
- billing/chargeback;
- custom role administration.
