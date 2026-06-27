# Phase C — Plug-and-Play + Onboarding (Design)

Date: 2026-06-27
Status: Design (autonomous, decisions delegated by owner)

## Context

Audit (task #33) found Engram is NOT plug-and-play after `docker compose up`:
- `bootstrap_golden_path` creates a service-account `Identity` (not a Django `User`), requires an externally-supplied `--api-key`, and is not invoked by compose.
- No Django admin/login user is created → the frontend `/login` has nobody to authenticate.
- `engram connect` requires 3 flags (`--server`, `--api-key`, `--project`), no auto-detect, no key creation, does not edit agent config.
- No `engram mcp install`; plugins are fixtures, not published.

Goal: after `docker compose up`, an operator can (1) log into the admin UI, and (2) connect an agent with one interactive command.

## Goal

1. **Auto-bootstrap** — compose brings up a ready cluster: first Django admin user + default org/project/team + an API key, idempotent, on first run.
2. **`engram connect` wizard** — interactive: detect server, log in (username/password), pick org/project, issue an API key via `/v1/admin/api-keys`, write `~/.engram` config + hook manifests. Non-interactive flag mode preserved.
3. **`engram mcp install`** — register the MCP server in Claude Code / Desktop config from stored credentials.
4. **Plugin packaging notes** — document how claude-plugin/codex-plugin are published (deferred automation).

## Non-Goals (Phase C)

- OAuth / phone login.
- A frontend onboarding wizard page (the admin UI already exists; Phase C is CLI/bootstrap).
- Auto-publishing plugins to a marketplace (documented only).
- Replacing `engram_bootstrap_golden_path` (kept for E2E).

## Architecture Decisions

### AD-1: New `engram_bootstrap_admin` management command
Idempotent. Creates (via `get_or_create`):
- `Organization` (default slug from `ENGRAM_BOOTSTRAP_ORG_SLUG` or `default`).
- `Team`, `Project` (default slugs).
- A Django `User` (superuser/staff) from `ENGRAM_BOOTSTRAP_ADMIN_USERNAME` + `ENGRAM_BOOTSTRAP_ADMIN_PASSWORD` (generated and printed to stdout if unset).
- A user-type `Identity` linked to that User (`external_id = external_id_for_user(user)`).
- `OrganizationMembership` with `organization_owner` role.
- An `ApiKey` (owner = the identity) with admin capabilities; raw key printed once to stdout (or stored in a known file `/data/bootstrap_api_key`).

Prints a clear "bootstrap complete" block with the login URL, username, and (if generated) password + api key. Safe to re-run (no duplicates).

### AD-2: Compose runs bootstrap after migrate
The `api` service command becomes `migrate && engram_bootstrap_admin && gunicorn`. Bootstrap is idempotent and fast, so running it every container start is fine (guarded by "already bootstrapped" check). No new service.

### AD-3: `engram connect` interactive wizard
New flow when run with no/missing flags:
1. Prompt server URL (default `http://localhost:8000`, auto-detect by probing `/-/healthz/`).
2. Prompt username/password → `POST /v1/auth/login` → DRF token.
3. `GET /v1/admin/organizations/` → pick org.
4. `GET /v1/admin/projects/` (with org header) → pick project.
5. Prompt key name → `POST /v1/admin/api-keys/` → store raw key in `~/.engram/credentials.json` (chmod 0600).
6. Write `~/.engram/config.json` (server, org, project) + hook manifests (existing).
`--non-interactive` / existing flags bypass prompts (CI mode).

### AD-4: `engram mcp install`
Writes an MCP server entry to the Claude config:
- Claude Code: `~/.claude.json` `mcpServers` (or `~/.config/claude/...` — detect) with `command: python -m engram_mcp`, `env: {ENGRAM_SERVER_URL, ENGRAM_API_KEY, ENGRAM_PROJECT_ID}` from `~/.engram`.
- Claude Desktop: `claude_desktop_config.json` if present.
Idempotent merge by server name `engram`. `--agent claude_code|claude_desktop|both`.

### AD-5: Plugin packaging (docs only)
Add README sections for building/publishing `packages/claude-plugin` and `packages/codex-plugin` (plugin marketplace manifest). No CI publish automation in Phase C.

## File Structure

- `apps/backend/engram/core/management/commands/engram_bootstrap_admin.py` (new) + `engram_bootstrap_admin_tests.py`.
- `deploy/compose/docker-compose.yml` (api command adds bootstrap).
- `deploy/compose/.env.example` (bootstrap env vars).
- `packages/cli/engram_cli/commands.py` (connect wizard + `mcp install`), `main.py` (subcommand), `http.py` (healthz probe), `config.py`.
- `packages/cli/engram_cli/cli_lifecycle_tests.py` (wizard + mcp install tests).
- `packages/claude-plugin/README.md`, `packages/codex-plugin/README.md` (publish docs).

## Testing

- Backend: `engram_bootstrap_admin_tests.py` — idempotency, creates User+Identity+membership+owner role+ApiKey, re-run no duplicates, password generation when unset. Run in Docker.
- CLI: extend `cli_lifecycle_tests.py` — wizard uses a stubbed HTTP client (prompts answered via stubbed input), asserts config + credentials written; `mcp install` writes the config entry idempotently. Stubs over mocks for the HTTP layer; mocks for filesystem where needed.

## Open decisions (accepted)

- Bootstrap prints generated secrets to stdout (dev/local). Production deployments should set the env vars. Documented.
- MCP install targets Claude Code `~/.claude.json` + Desktop config; other clients documented manually.
- Plugin publish = docs only (no automation).

## Next step

`writing-plans` → tasks C0–C3, subagent-driven execution.
