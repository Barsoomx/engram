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
uvx engram-connect install --agent both \
  --server URL --api-key KEY --project PROJECT
```

It writes hook configuration, installs the selected Claude Code and/or Codex
native plugin, then verifies the connection. `engram connect` performs only the
configuration and dry-run portion for managed installs.

Server deployment is handled separately by Compose, Helm, or SaaS provisioning.
The installer must not install local databases, vector stores, provider workers,
or background services.

## Hook Protocol Matrix

| Event | Endpoint | Sync behavior | Timeout budget | Response |
| --- | --- | --- | --- | --- |
| session start | `/v1/context/session-start` | synchronous retrieval | 60s | memory bundle, citations, warnings |
| prompt submit | `/v1/hooks/user-prompt-submit` then `/v1/context/user-prompt-submit` | optional synchronous retrieval | 60s | focused guidance |
| post tool use | `/v1/hooks/post-tool-use` | synchronous durable ingest, async distillation | 120s | ack, request id |
| stop/session end | `/v1/hooks/session-end` | synchronous durable ingest, async digest/curation | 60s | ack, request id |
| dry run | `/v1/hooks/dry-run` | synchronous verification | 5s | resolved actor, scopes, server health |

Every request includes agent family, agent version, event id, session id,
repository metadata, cwd, idempotency key, timestamp, and auth credential.
Responses are adapter-specific and must not emit fields unsupported by the
target agent.

The table describes handler budgets. The connector's HTTP call timeout remains
10 seconds for hook ingest/context calls, including the two-call retrieval
hooks; the general CLI HTTP timeout is 30 seconds.

Native lifecycle names differ at the adapter boundary:

| Adapter behavior | Claude Code | Codex |
| --- | --- | --- |
| session start | `SessionStart` | `SessionStart` |
| prompt context | `UserPromptSubmit` | `UserPromptSubmit` |
| tool capture | `PostToolUse` | `PostToolUse` |
| completion checkpoint | `SessionEnd` | `Stop` |

Codex has no native `Error`, `Decision`, or `SessionEnd` event. A failed tool is
captured in `PostToolUse.tool_response`, and `Stop` is turn-scoped.

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

Post-tool use:

- send tool result metadata, changed files, commands, failures, and references;
- create normalized observations;
- enqueue distillation and search index updates.

Stop/session end:

- summarize unresolved work and decisions;
- generate candidate observations and memory updates;
- mark stale injected memory when the session proves it wrong.

Explicit tools:

- `engram_search`
- `engram_context`
- `engram_memory_link`
- `engram_observations`
- `engram_memory_version`
- `engram_memory_feedback`

## Server-Only Contract

Adapters may cache request ids, hook trust state, credential metadata, and
short-lived retry buffers.
They must not run persistent local summarizers, vector indexes, SQLite stores, or
background workers. If the server is unavailable, hooks return an error and no
memory bundle is returned; nothing is written to local storage.

## Trust And Managed Hooks

Enterprise deployments should support managed hooks so platform admins can
install and trust the same integration across developer machines. Trust is still
not authorization. The server must verify API keys, scopes, request signatures,
tenant/project binding, and replay protection for every call.

For non-managed Codex installs, the user reviews the Engram commands in
`/hooks` and starts a new thread. The installer must not bypass that trust
boundary.
