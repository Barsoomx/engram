# Agent Integrations

## Integration Goal

Claude Code and Codex should connect through hooks that call the server. Hooks
capture observations, request context bundles, and return guidance. They must
not start local memory workers or depend on local databases.

## Supported Agent Families

- Claude Code: lifecycle hooks, tool-use hooks, session hooks, and optional MCP
  tools for explicit memory operations.
- Codex: hook events and managed/trusted hook configuration for session, prompt,
  and tool-use integration.

The architecture is LLM-agnostic. Gemini CLI, Cursor, OpenAI Agents, and future
agent runtimes should be added by implementing thin adapters over the same
server memory and context APIs, not by creating separate memory stores.

The hook adapters should be separate thin packages that share the same server
API schema. Agent-specific differences belong at the adapter boundary.

The local MCP bridge is installed alongside hooks when requested. It exposes
developer and lead tools, but every tool call goes back to the server and uses
the same RBAC checks as HTTP APIs.

## Installation Model

The target installer is a client connector, not a worker bootstrapper. The V1
golden path is explicit:

```bash
engram connect --server URL --api-key KEY --project PROJECT
```

It writes hook configuration for Claude Code and/or Codex, then calls the dry-run
endpoint.

Server deployment is handled separately by Compose, Helm, or SaaS provisioning.
The installer must not install local databases, vector stores, provider workers,
or background services.

## Hook Protocol Matrix

| Event | Endpoint | Sync behavior | Timeout budget | Response |
| --- | --- | --- | --- | --- |
| session start | `/v1/context/session-start` | synchronous retrieval | 2s | memory bundle, citations, warnings |
| prompt submit | `/v1/context` | optional synchronous retrieval | 1s | focused guidance |
| pre tool use | `/v1/hooks/pre-tool-use` | warning/audit in V1 | 1s | allowed warning fields only |
| post tool use | `/v1/hooks/post-tool-use` | synchronous durable ingest, async distillation | 2s | ack, request id |
| stop/session end | `/v1/hooks/session-end` | synchronous durable ingest, async digest/curation | 2s | ack, scheduled job ids |
| dry run | `/v1/hooks/dry-run` | synchronous verification | 2s | resolved actor, scopes, server health |

Every request includes agent family, agent version, event id, session id,
repository metadata, cwd, idempotency key, timestamp, and auth credential.
Responses are adapter-specific and must not emit fields unsupported by the
target agent.

## Hook Responsibilities

Session start:

- identify agent family and version;
- resolve local workspace, repository, branch, and project;
- call `/v1/context/session-start`;
- inject the context bundle and citations into the agent context.

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

Adapters may cache request ids, hook trust state, credential metadata, and
short-lived retry buffers.
They must not run persistent local summarizers, vector indexes, SQLite stores, or
background workers. If the server is unavailable, hooks should degrade by
recording a bounded local retry envelope or returning no memory, depending on
admin policy.

Retry envelopes are metadata-only by default. They have a strict size limit, TTL,
redaction pass, optional encryption, and never contain provider secrets, memory
bundles, embeddings, or unredacted prompt/tool-output bodies.

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
