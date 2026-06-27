# CLI Guide

The `engram` CLI is a thin Python client that connects Claude Code and/or Codex
to a running Engram server. It writes local config, registers hook manifests,
and exposes a few operator commands. It never runs a local memory worker,
local database, vector store, or provider SDK.

This guide documents the commands shipped in Phase C. For the underlying
design, see [../client-installation.md](../client-installation.md).

## Install

```bash
cd packages/cli
pip install -e .
engram --help
```

## Local state

All local state lives under `~/.engram/` (override with `--config-dir`):

| Path                              | Mode   | Contents                                              |
|-----------------------------------|--------|-------------------------------------------------------|
| `config.json`                     | 0644   | Server URL, project id, team id, agent runtimes, resolved scope |
| `credentials.json`                | 0600   | The raw API key and its fingerprint                   |
| `hooks/codex.json`                | 0644   | Codex hook manifest                                   |
| `hooks/claude_code.json`          | 0644   | Claude Code hook manifest                             |

Forbidden on the client (by design): provider secrets, embeddings, memory
bundles, prompt/tool-output bodies, persistent queues.

## `engram connect`

Writes config + credentials + hook manifests, then calls `POST /v1/hooks/dry-run`
to verify the key resolves to the expected actor and scope.

```bash
engram connect \
  --server http://localhost:8000 \
  --api-key sk-engram_... \
  --project <project_id> \
  --agent both
```

| Flag              | Required | Default | Values / meaning                                     |
|-------------------|----------|---------|------------------------------------------------------|
| `--server`        | yes      | -       | `http(s)://host[:port]`                              |
| `--api-key`       | yes      | -       | Scoped Engram key (a `sk-engram_` prefix is recommended) |
| `--project`       | yes      | -       | Project id                                           |
| `--team`          | no       | key's bound team | Team id                                      |
| `--agent`         | no       | `both`  | `codex`, `claude-code` (alias `claude_code`), `both` |
| `--agent-version` | no       | empty   | Free-form version tag                                |
| `--config-dir`    | no       | `~/.engram` | Override config root                              |

Output on success:

```
connected Engram CLI to http://localhost:8000
project: <project_id>
runtimes: codex, claude_code
credential: <fingerprint>
organization: <organization_id>
capabilities: memories:read, observations:write
```

The dry-run must return `status: ok` or connect fails with a remediation hint.
Capabilities and organization are echoed from the server's resolved scope.

## `engram doctor`

Verifies that local state is consistent and the server is reachable, then
re-runs a dry-run for each configured runtime.

```bash
engram doctor
```

Checks performed (in order, each printed as `ok`/`fail`):

1. `config` - `~/.engram/config.json` loads.
2. `credential` - `~/.engram/credentials.json` loads and contains an API key.
3. `hook_config` - manifest exists for each configured runtime.
4. `server_health` - `GET /-/healthz/` returns `{"status":"ok"}`.
5. `dry_run` - `POST /v1/hooks/dry-run` succeeds for each runtime.

A clean run ends with `All required checks passed.` and exit code 0. Any check
that fails prints its error code (for example `server_unavailable`,
`missing_capability`) and a one-line remediation.

## `engram disconnect`

Removes local state created by `connect`:

```bash
engram disconnect
```

Deletes `config.json`, `credentials.json`, and both hook manifests, then
removes `hooks/` if empty. Prints `disconnected Engram local state.` if anything
was removed, otherwise `nothing connected.` This does not revoke the server-side
API key.

## `engram hook`

The thin hook adapter. Reads a JSON object from stdin, attaches project/team
and runtime metadata, computes an idempotency key, posts to the server, and
prints a response shaped for the target agent.

Subcommands (one per supported event):

- `session-start` - posts to `/v1/hooks/session-start` (ingest) and then
  `POST /v1/context/session-start` to fetch the rendered context bundle.
- `post-tool-use` - posts to `/v1/hooks/post-tool-use`.
- `error` - posts to `/v1/hooks/error`.
- `decision` - posts to `/v1/hooks/decision`.

```bash
echo '{"session_id":"s1","payload":{"tool":"Edit"}}' | \
  engram hook post-tool-use --agent claude-code --response-format claude-code
```

| Flag               | Values                                | Default   |
|--------------------|---------------------------------------|-----------|
| `--agent`          | `codex`, `claude-code`, `claude_code` | first configured runtime |
| `--config-dir`     | path                                  | `~/.engram` |
| `--response-format`| `server`, `codex`, `claude-code`      | `server`  |

`--response-format` controls the shape of stdout:

- `server` - the raw server response body.
- `claude-code` - for `session-start`, a `systemMessage` plus a
  `hookSpecificOutput.additionalContext` block; for other events, an empty
  object (Claude Code ignores the body).
- `codex` - `{"continue": true, ...}` plus, for `session-start`, the
  `systemMessage` and `additionalContext`.

Idempotency: the adapter derives `event_id`, `idempotency_key`, `content_hash`,
and `request_id` from a stable hash of the event material when they are not
supplied, so replaying the same stdin is safe.

## `engram search`

Semantic + full-text memory search within the connected project's scope.

```bash
engram search --query "auth token rotation" --limit 5
engram search --query "auth" --file-path src/auth.py --symbol rotate_token --json
```

| Flag          | Default | Description                                   |
|---------------|---------|-----------------------------------------------|
| `--query`     | empty   | Free-text query                               |
| `--file-path` | -       | Repeatable path filter                        |
| `--symbol`    | -       | Repeatable symbol filter                      |
| `--limit`     | 5       | Max items                                     |
| `--json`      | false   | Emit the raw server response as JSON          |

Plain output lists each item as `<citation>: <title>` followed by its body.

## `engram observations`

Lists observations visible to the connected scope.

```bash
engram observations --limit 20
```

## `engram memory`

Memory mutations and links (all within the connected project scope):

```bash
engram memory version <memory_id> --body "Updated body" --reason "fix"
engram memory link <memory_id> --link-type file --target src/auth.py --label "rotate"
engram memory links <memory_id>
```

| Subcommand | Path                                   | Notes                              |
|------------|----------------------------------------|------------------------------------|
| `version`  | `POST /v1/memories/{id}/version`       | Create/retrieve a memory version   |
| `link`     | `POST /v1/memories/{id}/links`         | `--link-type` in `file`, `symbol`, `commit`, `issue` |
| `links`    | `GET /v1/memories/{id}/links`          | List links                         |

## Error model

Every command emits errors to stderr as:

```
<code>: <detail with secrets redacted>
remediation: <one-line hint>
```

Common codes: `missing_server_url`, `missing_api_key`, `missing_project`,
`missing_config`, `missing_credential`, `missing_hook_config`,
`server_unavailable`, `http_error`, `invalid_response`, `invalid_key`,
`expired_key`, `missing_capability`, `project_scope_denied`,
`team_scope_denied`. The raw API key is always redacted from error output.

## See also

- [../quickstart.md](../quickstart.md)
- [mcp.md](mcp.md)
- [plugins.md](plugins.md)
