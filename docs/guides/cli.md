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

## Project resolution

`engram search`, `engram observations`, `engram memory version|link|links`,
and hook ingest all resolve which project a call targets with the same
precedence ladder, in order:

1. an explicit override for that call - `--project` on the CLI commands
   below, or the harness-supplied `project_id` field on hook payloads;
2. `ENGRAM_PROJECT_ID`;
3. `project_id` in `~/.engram/config.json` (written by `engram connect
   --project ...` - optional, `connect` works without it);
4. the repository derived from `git remote get-url origin` in the current
   directory, sent as `repository_url` instead of `project_id`. Credentials
   embedded in the remote URL (`https://user:token@host/...`, including the
   password-only form `https://:token@host/...`) are stripped before the
   value ever leaves the machine.

`engram search` never fails client-side on an unresolved project: if nothing
in the ladder resolves, it sends the request with neither `project_id` nor
`repository_url`, and the server answers `400 project_or_repository_required`.
`engram observations` and `engram memory version|link|links` fail fast
client-side instead - `missing_project: Set --project, ENGRAM_PROJECT_ID, or
run inside a git repository` - with no network call.

The server always re-authorizes whichever project a `repository_url`-derived
request resolves to, inside the caller's own organization; see
[backend-contracts.md](../backend-contracts.md#project-routing-contract) for
the resolver contract and error codes.

## `engram connect`

Writes config + credentials + hook manifests, then calls `POST /v1/hooks/dry-run`
to verify the key resolves to the expected actor and scope.

```bash
engram connect \
  --server http://localhost:8000 \
  --api-key egk_... \
  --project <project_id> \
  --agent both
```

| Flag              | Required | Default | Values / meaning                                     |
|-------------------|----------|---------|------------------------------------------------------|
| `--server`        | yes      | -       | `http(s)://host[:port]`                              |
| `--api-key`       | yes      | -       | Scoped Engram key (server-issued keys use the `egk_` prefix) |
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

Project resolution follows the ladder in [Project resolution](#project-resolution):
the stdin payload's own `project_id` field wins, then `ENGRAM_PROJECT_ID`, then
config `project_id`; hooks have no repo-derived fallback beyond the
`repository_url`/`repository_root`/`cwd` the harness already supplies in the
payload.

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

Semantic + full-text memory search within the resolved project's scope (see
[Project resolution](#project-resolution)).

```bash
engram search --query "auth token rotation" --limit 5
engram search --query "auth" --file-path src/auth.py --symbol rotate_token --json
engram search --query "auth" --project <project_id>
```

| Flag          | Default | Description                                   |
|---------------|---------|-----------------------------------------------|
| `--query`     | empty   | Free-text query                               |
| `--file-path` | -       | Repeatable path filter                        |
| `--symbol`    | -       | Repeatable symbol filter                      |
| `--limit`     | 5       | Max items                                     |
| `--project`   | empty   | Project id override (ladder rung 1)           |
| `--json`      | false   | Emit the raw server response as JSON          |

Plain output lists each item as `<citation>: <title>` followed by its body.

## `engram observations`

Lists observations visible to the resolved scope (see
[Project resolution](#project-resolution)).

```bash
engram observations --limit 20
engram observations --project <project_id>
```

| Flag        | Default | Description                                     |
|-------------|---------|--------------------------------------------------|
| `--limit`   | 20      | Max items                                         |
| `--project` | empty   | Project id override (ladder rung 1)               |

## `engram memory`

Memory mutations and links (all within the resolved project scope; every
subcommand accepts `--project` as ladder rung 1):

```bash
engram memory version <memory_id> --body "Updated body" --reason "fix"
engram memory link <memory_id> --link-type file --target src/auth.py --label "rotate"
engram memory links <memory_id>
engram memory version <memory_id> --body "Updated body" --project <project_id>
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

`missing_project` is raised client-side, with no network call, by
`engram observations` and `engram memory version|link|links` when the
[precedence ladder](#project-resolution) resolves neither a project id nor a
repository URL. Two server-side codes can also reach the terminal, printed
with their own code (not wrapped as `http_error`) once a `repository_url` is
in play: `project_or_repository_required` (400 - neither `project_id` nor
`repository_url` reached the server; `engram search` can hit this, since it
does not gate client-side) and `project_not_found` (404 - the resolved
`repository_url` matches no project in the organization).

## See also

- [../quickstart.md](../quickstart.md)
- [mcp.md](mcp.md)
- [plugins.md](plugins.md)
