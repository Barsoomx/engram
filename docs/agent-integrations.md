# Agent Integrations

## Integration Goal

Claude Code and Codex should connect through hooks that call the server. Hooks
capture observations, retrieve memory, and return guidance. They must not start
local memory workers or depend on local databases.

## Supported Agent Families

- Claude Code: lifecycle hooks, tool-use hooks, session hooks, and optional MCP
  tools for explicit memory operations.
- Codex: hook events and managed/trusted hook configuration for session, prompt,
  and tool-use integration.

The hook adapters should be separate thin packages that share the same server
API schema. Agent-specific differences belong at the adapter boundary.

## Hook Responsibilities

Session start:

- identify agent family and version;
- resolve local workspace, repository, branch, and project;
- call `/v1/context/session-start`;
- inject memory bundle and citations into the agent context.

Prompt submit:

- detect whether the prompt asks about prior work, conventions, architecture,
  recurring errors, or previous decisions;
- call focused retrieval when useful;
- attach compact memory guidance before the agent plans.

Pre-tool use:

- retrieve narrow context for risky or context-sensitive actions;
- optionally run server-side policy checks;
- return warnings or blocking decisions only when the configured policy layer
  requires it.

Post-tool use:

- send tool result metadata, changed files, commands, failures, and references;
- create normalized observations;
- enqueue distillation and search index updates.

Stop/session end:

- summarize unresolved work and decisions;
- generate candidate observations and memory updates;
- mark stale injected memory when the session proves it wrong.

Explicit tools:

- `memory.search`
- `memory.observe`
- `memory.update`
- `memory.feedback`
- `memory.explain`

## Server-Only Contract

Adapters may cache request ids, hook trust state, and short-lived retry buffers.
They must not run persistent local summarizers, vector indexes, SQLite stores, or
background workers. If the server is unavailable, hooks should degrade by
recording a bounded local retry envelope or returning no memory, depending on
admin policy.

## Trust And Managed Hooks

Enterprise deployments should support managed hooks so platform admins can
install and trust the same integration across developer machines. Trust is still
not authorization. The server must verify API keys, scopes, request signatures,
tenant/project binding, and replay protection for every call.

## Policy Layer

Policy enforcement uses the same hook surfaces as memory retrieval, but it is a
separate domain:

- memory guidance tells the agent what prior context matters;
- policy decisions tell the hook whether an operation is allowed, warned, or
  blocked;
- both decisions are audited with shared correlation ids.

This keeps the memory product honest: memory improves agent behavior, while the
server remains the source of truth for authorization and policy.
