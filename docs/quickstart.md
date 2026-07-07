# Quickstart

This guide takes you from a clean checkout to a working Engram deployment with
ingest and context retrieval verified. It targets the Phase C bootstrap and the
`engram connect` wizard flow.

Expect 10-15 minutes, most of it Docker image builds.

## Prerequisites

- Docker with the Compose v2 plugin.
- `git`.
- A Python 3.12+ environment if you want to run the `engram` CLI on your host
  (the CLI is a thin client; it does not run a local worker).
- The Compose stack defaults to a fake provider gateway (`ENGRAM_PROVIDER_MODE=fake`
  in `docker-compose.yml`), so ingest, retrieval, and context generation all work
  without network egress or a real provider key. Set `ENGRAM_PROVIDER_MODE=real`
  and configure a real provider secret to use live embeddings/generation.

## 1. Clone and configure

```bash
git clone <your-engram-repo-url> engram
cd engram
cd deploy/compose
cp .env.example .env
```

Open `.env` and set at minimum:

```dotenv
ENGRAM_SECRET_KEY=<generate-a-long-random-string>
ENGRAM_DEBUG=false
ENGRAM_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0
ENGRAM_LOG_LEVEL=INFO
ENGRAM_CELERY_BROKER_URL=amqp://engram:engram@rabbitmq:5672/engram
ENGRAM_CELERY_RESULT_BACKEND=redis://redis:6379/1
```

`ENGRAM_SECRET_KEY` must be changed from the example value before any real use.
`ENGRAM_DATABASE_URL` and `ENGRAM_REDIS_URL` are hardcoded in
`docker-compose.yml` to the bundled `postgres`/`redis` services and are not
read from `.env`. `ENGRAM_CELERY_BROKER_URL` and `ENGRAM_CELERY_RESULT_BACKEND`
are also hardcoded in `docker-compose.yml`, so the values set in `.env` in this
step have no effect on the containers started by this Compose file.

## 2. Start the stack

From `deploy/compose/:

```bash
docker compose up --build -d
docker compose ps
```

Services that come up:

| Service              | Port  | Role                                              |
|----------------------|-------|---------------------------------------------------|
| `api`                | 8000  | Django + DRF app (runs migrations, then granian)  |
| `frontend`           | 3000  | Next.js admin UI                                  |
| `worker-realtime`    | -     | Celery worker, `engram-realtime` queue            |
| `worker-near-realtime` | -   | Celery worker, `engram-near-realtime` queue       |
| `worker-batch`       | -     | Celery worker, `engram-batch` queue               |
| `worker-highmemory`  | -     | Celery worker, `engram-highmemory` queue          |
| `worker-domain-events` | -   | Celery worker, `engram-domain-events` queue       |
| `beat`               | -     | Celery beat scheduler                             |
| `relay`              | -     | Durable outbox relay                              |
| `postgres`           | -     | PostgreSQL 18 with pgvector                       |
| `redis`              | -     | Cache + result backend                            |
| `rabbitmq`           | -     | Celery broker                                     |

The `api` service runs `python manage.py migrate --noinput` on every start, so
the schema is current. Wait for `api` to become healthy before continuing:

```bash
docker compose exec api python manage.py check
```

A `200` from `http://localhost:8000/-/healthz/` means the API is up.

## 3. Bootstrap the golden-path project

Engram ships a deterministic bootstrap command that creates a ready-to-use
organization, team, project, service-account identity, a scoped API key, a
provider secret envelope, and project + organization model policies for
generation, embedding, digest, and curation. It is idempotent: re-running it
updates the existing rows.

Choose a raw API key value (this is the secret your agents will use). The server
does not enforce a specific prefix, but the `egk_` prefix used by
server-issued keys is strongly recommended for readability and audit. Use a
long, random string. For example:

```bash
export ENGRAM_GOLDEN_KEY='egk_local_quickstart_00112233445566778899'
```

Run the bootstrap inside the `api` container:

```bash
docker compose exec api python manage.py engram_bootstrap_golden_path \
  --api-key "$ENGRAM_GOLDEN_KEY"
```

Plain-text output:

```
organization_id=<uuid>
project_id=<uuid>
team_id=<uuid>
repository_url=<url>
api_key_fingerprint=<short-fingerprint>
```

For scripting, add `--json` to get the same fields as a single JSON object,
plus `identity_id`, `api_key_id`, `capabilities`, `provider_secret_id`,
`generation_policy_id`, `embedding_policy_id`,
`organization_generation_policy_id`, `organization_embedding_policy_id`.

What this command creates (all under organization slug `engram-e2e`):

- Organization `engram-e2e` ("Engram E2E").
- Team `platform`.
- Project `backend` bound to the team.
- Service-account identity `golden-path-agent` with the `developer` role.
- API key with capabilities `memories:read` and `observations:write`, scoped to
  the team and project.
- An OpenAI provider secret envelope and six model policies: project-scoped
  `generation` and `embedding` on project `backend`, plus organization-scoped
  `generation`, `embedding`, `digest`, and `curation`. Generation/digest/curation
  use `gpt-4.1-mini`; embedding uses `text-embedding-3-small`.

Record `$ENGRAM_GOLDEN_KEY` somewhere safe now. It is the only time the raw key
is materialized on your side; the server stores only its hash and fingerprint.

## 4. Install the CLI

The `engram` CLI is a thin Python client. Install it from the package source:

```bash
cd ../../packages/cli
pip install -e .
cd -
```

Verify:

```bash
engram --help
```

You should see subcommands: `connect`, `install`, `doctor`, `disconnect`,
`mcp-install`, `mcp`, `hook`, `search`, `memory`, `observations`.

## 5. Connect an agent

`engram connect` writes local config and hook manifests, then calls the
server's dry-run endpoint to verify the key resolves to the expected scope. It
does not install local databases, workers, or vector stores.

For both Claude Code and Codex (the default):

```bash
engram connect \
  --server http://localhost:8000 \
  --api-key "$ENGRAM_GOLDEN_KEY" \
  --project <project_id-from-step-3>
```

For a single runtime, pass `--agent codex` or `--agent claude-code`.

Flags:

| Flag              | Required | Description                                                     |
|-------------------|----------|-----------------------------------------------------------------|
| `--server`        | yes      | Engram API base URL (`http://` or `https://`).                  |
| `--api-key`       | yes      | Scoped Engram API key (server-issued keys use the `egk_` prefix). |
| `--project`       | yes      | Project id from bootstrap (or from the admin UI).               |
| `--team`          | no       | Team id. Defaults to the key's bound team.                      |
| `--agent`         | no       | `codex`, `claude-code`, or `both` (default `both`).             |
| `--agent-version` | no       | Free-form agent version tag.                                    |
| `--config-dir`    | no       | Override the config root (default `~/.engram`).                 |

Successful output:

```
connected Engram CLI to http://localhost:8000
project: <project_id>
runtimes: codex, claude_code
credential: <fingerprint>
organization: <organization_id>
capabilities: memories:read, observations:write
```

Local files written under `~/.engram/:

- `config.json` - server URL, project id, team id, agent runtimes, resolved
  scope.
- `credentials.json` - the API key (mode `0600`). This is the only place the
  raw key lives on the client.
- `hooks/codex.json` and/or `hooks/claude_code.json` - hook manifests each
  pointing at `engram hook <event> --agent <runtime>`.

## 6. Verify the connection

```bash
engram doctor
```

`doctor` checks, in order: config loads, credentials load, hook manifests
exist, server health is `ok`, and a dry-run succeeds for each configured
runtime. A clean run prints:

```
ok config: loaded
ok credential: loaded
ok hook_config: codex, claude_code
ok server_health: http://localhost:8000
ok dry_run: codex, claude_code
All required checks passed.
```

## 7. Register hooks with your agent

`engram connect` writes hook manifests that the plugin packages or your agent
config reference. The hooks exposed are `SessionStart`, `PostToolUse`, `Error`,
`Decision`, `SessionEnd`, and `UserPromptSubmit`, each invoking the thin
`engram hook <event>` adapter.

For the native plugin packages, see:

- `packages/claude-plugin/README.md`
- `packages/codex-plugin/README.md`

For the MCP bridge (memory tools surfaced as callable tools - registered
automatically for Claude Code via the plugin, via `engram mcp install` for
Claude Desktop, or `engram mcp serve` for any other MCP client), see
[guides/mcp.md](guides/mcp.md).

## 8. Exercise ingest and context

Trigger a session-start hook by hand to confirm context retrieval works end to
end. The hook reads a JSON payload from stdin:

```bash
echo '{"session_id":"qs-001"}' | \
  engram hook session-start --agent claude-code --response-format claude-code
```

A successful response includes a `systemMessage` with the rendered context
bundle (empty on a fresh project, but the call itself validates the full
ingest-retrieve-inject path).

Submit a PostToolUse observation so there is data to retrieve next time:

```bash
echo '{
  "session_id":"qs-001",
  "payload":{"tool":"Edit","summary":"Added quickstart"},
  "observation":{"title":"Quickstart drafted","body":"Wrote docs/quickstart.md"}
}' | engram hook post-tool-use --agent claude-code --response-format server
```

Now search the memory you just ingested:

```bash
engram search --query "quickstart" --json
```

The CLI hits `POST /v1/search/` with the connected project's scope and returns
matching memory items with citations.

## 9. Explore the admin UI

Open `http://localhost:3000/` and sign in as `admin`. This account is created
automatically by `engram_bootstrap_admin`, which the `api` container runs on
every start (separate from the `engram-e2e` golden-path project from step 3;
it creates its own `default` organization, team, and project). The password
is `ENGRAM_BOOTSTRAP_ADMIN_PASSWORD` if you set it before the first start,
otherwise a random password is generated once and printed to the `api`
container's logs (`docker compose logs api`). The capability-gated sidebar
groups Workspace pages (memories, observations, review, search/hook debuggers,
projects, digests, workflow runs) and Administration pages (secrets, model
policies, organizations, teams, members, roles, API keys, audit, health). See
[guides/admin-ui.md](guides/admin-ui.md) and
[guides/api-keys.md](guides/api-keys.md).

## 10. Stop and clean up

```bash
docker compose down           # stop containers, keep volumes
docker compose down -v        # also delete the postgres volume
```

To remove CLI local state:

```bash
engram disconnect
```

This deletes `~/.engram/config.json`, `~/.engram/credentials.json`, and the
hook manifests. It does not revoke the server-side API key; do that from the
admin UI or `POST /v1/admin/api-keys/{id}/revoke/`.

## Next steps

- [CLI guide](guides/cli.md)
- [MCP guide](guides/mcp.md)
- [Plugins guide](guides/plugins.md)
- [Admin UI guide](guides/admin-ui.md)
- [API keys guide](guides/api-keys.md)
- [Auth guide](guides/auth.md)
- [API reference](api-reference.md)
