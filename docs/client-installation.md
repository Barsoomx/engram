# Client Installation And Hook Bootstrap

## Problem

The inherited `npx claude-mem install` flow does too much for the target
product. It installs local runtime dependencies, prepares a worker, writes local
settings, and registers hooks. That is the opposite of the `claudex-teams`
server-only goal.

For this fork, server deployment and agent connection are separate jobs:

- server deployment belongs to Docker Compose, Helm, or a managed SaaS control
  plane;
- client installation only connects Claude Code or Codex to an already-running
  server.

## Target Command Shape

The client command should be renamed and narrowed. Candidate names:

- `npx claudex-teams connect`
- `npx claudex-teams hooks install`
- `npx claudex-teams agent connect`

The V1 golden path is non-interactive and scriptable:

```bash
npx claudex-teams connect --server URL --api-key KEY --project PROJECT
```

The interactive wizard can wrap the same fields later:

1. Choose agent: Claude Code, Codex, or both.
2. Enter server URL.
3. Authenticate by device/browser flow or paste a scoped API key.
4. Choose organization, team, project, and repository binding.
5. Install thin hook config for the selected agent.
6. Optionally install the local MCP bridge for memory tools.
7. Run a dry-run hook call and print the resolved identity/scope.

The command must not install Bun, local vector databases, local SQLite stores,
provider SDK workers, or background services.

## Local Credential

V1 stores one hook credential artifact:

- preferred: OS keychain entry containing a project-scoped agent token minted
  from the supplied API key;
- fallback for headless hosts: file containing the agent token with strict file
  permissions;
- local config stores only server URL, project id, and redacted fingerprint.

The raw organization/team provider keys never reach the client. `doctor` verifies
token validity, expiry, project scope, last rotation time, hook file state, and
server reachability.

## Managed Installation

Enterprise admins should be able to avoid per-developer wizard work:

- Codex managed hooks can distribute trusted hook configuration.
- Claude Code hook configuration can be templated by the company onboarding
  script or managed device setup.
- API keys should be scoped to team/project/service account and rotated from the
  server UI.

In managed mode, the local command becomes a verifier:

```bash
npx claudex-teams doctor
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
- `doctor`: validate installed hooks and server reachability;
- `mcp install`: configure the local MCP bridge without creating a local memory
  store;
- `disconnect`: remove local hook entries created by the package.

Everything else stays server-side.
