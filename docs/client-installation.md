# Client Installation And Hook Bootstrap

## Problem

The inherited `npx claude-mem install` flow does too much for the target
product. It installs local runtime dependencies, prepares a worker, writes local
settings, and registers hooks. That is the opposite of Engram's server-only
direction.

For this fork, server deployment and agent connection are separate jobs:

- server deployment belongs to Docker Compose, Helm, or a managed SaaS control
  plane;
- client installation only connects Claude Code or Codex to an already-running
  server.

## Target Command Shape

The client command should be renamed and narrowed around the `engram` binary.
Target command shape:

- `engram connect`
- `engram install`
- `engram mcp install`

The V1 golden path is non-interactive and scriptable:

```bash
uvx engram-connect install --agent both \
  --server URL --api-key KEY --project PROJECT
```

Use `--agent claude-code`, `--agent codex`, or `--agent both`. `connect` remains
the credential-and-hook-config primitive; `install` runs it, installs the
selected native marketplace plugin, and runs `doctor`.

The interactive wizard wraps a subset of the same fields:

1. Enter server URL.
2. Log in with a username and password against the server's token endpoint.
3. Choose organization and project from the fetched lists.
4. Provide an API key name; the wizard issues a scoped API key.
5. Install thin hook config for the agent runtime(s) passed via `--agent`.
6. Print a connection summary; it does not run a live dry-run identity/scope
   check the way the non-interactive flags path does.

The command must not install Bun, local vector databases, local SQLite stores,
provider SDK workers, or background services.

The dashboard's **Connect agent** modal generates the same command. It exposes
Claude Code, Codex, and Both as explicit choices, defaults to Claude Code for
backward compatibility, and shows the matching native trust/completion steps.
It does not retain the displayed key or implement a second installer.

## Local Credential

V1 stores one hook credential artifact:

- a `credentials.json` file containing the API key, written with owner-only
  file permissions (`chmod 0o600`); there is no OS keychain integration;
- local config stores server URL, project id, team id, agent runtimes, agent
  version, credential fingerprint, connect timestamp, and resolved
  actor/scope.

The raw organization/team provider keys never reach the client. `doctor` verifies
token validity, expiry, project scope, hook file state, and server
reachability.

## Managed Installation

Enterprise admins should be able to avoid per-developer wizard work:

- Codex managed hooks can distribute trusted hook configuration.
- Claude Code hook configuration can be templated by the company onboarding
  script or managed device setup.
- API keys should be scoped to team/project/service account and rotated from the
  server UI.

In managed mode, the local command becomes a verifier:

```bash
engram doctor
```

It checks hook files, trust state, server reachability, identity resolution, and
whether the API key is scoped as expected.

## Local Files

Allowed local state:

- server URL;
- selected organization/team/project ids;
- short-lived device-flow token cache when device flow is added later;
- scoped agent token or managed-hook injected credential;
- redacted credential fingerprint;
- hook installation metadata;
- bounded metadata-only retry envelopes if admin policy allows offline
  buffering.

Forbidden local state:

- provider secrets;
- long-term memory database;
- embeddings;
- summarization worker;
- persistent queue;
- cached memory bundles;
- unredacted prompt/tool-output bodies in retry storage;
- local admin UI as the operational source of truth.

## Hook Output Contract

Installed hooks should execute a tiny adapter command that:

1. reads hook JSON from stdin;
2. adds local workspace metadata;
3. signs or authenticates the request;
4. posts the event to the server;
5. prints only the hook response fields supported by the target agent.

Agent-specific compatibility matters. For example, Codex hook responses must not
emit unsupported fields. The adapter should have contract tests for each event
type and agent family.

Codex's native lifecycle maps `Stop` to the existing `session-end` adapter as a
turn checkpoint. Codex does not expose native `Error`, `Decision`, or
`SessionEnd` hooks; tool failures are carried by `PostToolUse`.

## Server Deploy Is Separate

Server setup should live in deployment docs and automation:

- Docker Compose for development and small on-premise trials;
- Helm for production on-premise;
- SaaS provisioning later.

The client installer may link to server setup instructions, but it must not
attempt to deploy the server implicitly.

## First Implementation Decision

Build the smallest useful client package:

- `connect`: write thin hooks and verify server scope;
- `install`: connect, install the selected Claude Code/Codex marketplace
  plugin, and verify the resulting Engram connection;
- `doctor`: validate installed hooks and server reachability;
- `mcp install` / `mcp serve`: register or run the local MCP bridge without
  creating a local memory store. The Claude Code plugin bundles `mcp serve`
  and registers it automatically; `mcp install` targets Claude Desktop and
  other clients that need a config entry written for them. See
  [guides/mcp.md](guides/mcp.md).
- `disconnect`: remove local hook entries created by the package.

Everything else stays server-side.
