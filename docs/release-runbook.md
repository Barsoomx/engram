# Release Runbook

Step-by-step procedure for cutting an Engram release. Follow these gates in
order. Record the exact command, working directory, OS, Docker version, exit
code, and first decisive failure for each step in the release status report.

References:

- `docs/operations-and-deployment.md` for deployment profiles and runtime
  components.
- `docs/client-installation.md` for client install and hook bootstrap flow.

## 0. Prerequisites

- A fresh clone in a new directory with no reused virtualenv, `node_modules`,
  database volume, build cache, or generated config.
- Docker with Compose v2 enabled.
- `python3`, `pnpm` (11.9.0 via `corepack prepare pnpm@11.9.0 --activate`), and
  `poetry` available on the host for backend and frontend gates.
- No real secrets in the tree. Confirm:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/master
```

## 1. Fresh-Clone Gate

Run every command from the repository root unless noted. All commands must exit
0 before promoting the release.

### 1.1 Start Compose runtime

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build --wait
```

Expected: api, frontend, PostgreSQL, RabbitMQ broker, and Redis reach healthy
state (each defines a healthcheck); worker-realtime, worker-near-realtime,
worker-batch, worker-highmemory, worker-domain-events, beat, and relay have no
healthcheck and are expected to be running.

### 1.2 Apply migrations and verify freshness

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"
```

Expected: migrations apply cleanly and `No changes detected`.

### 1.3 Backend tests, lint, and format

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm \
  -v "$PWD/deploy/compose/docker-compose.yml:/contract/docker-compose.yml:ro" \
  -e ENGRAM_COMPOSE_CONTRACT_PATH=/contract/docker-compose.yml \
  api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v && ruff check . && ruff format --check ."
```

Expected: pytest passes, ruff reports `All checks passed!`, format reports all
files already formatted.

### 1.4 System check

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py check"
```

Expected: no issues reported.

### 1.5 CLI tests

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

Expected: full CLI suite passes, including `connect`, `doctor`, `disconnect`,
`search`, `memory version`, `memory link`, `memory links`, and `observations`.

### 1.6 MCP bridge contract tests

The MCP bridge ships inside `engram-connect` (`engram_cli/mcp_server.py`,
`engram_cli/mcp_tools.py`), so its contract tests run as part of the CLI suite
in 1.5.

```bash
python3 -m compileall packages/cli/engram_cli
```

Expected: the package compiles without syntax errors.

### 1.7 Claude Code and Codex plugin contract tests

```bash
python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v
python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v
```

Expected: both plugin contract suites pass.

### 1.8 E2E golden path

```bash
docker compose -f deploy/compose/docker-compose.yml down
COMPOSE_PROJECT_NAME=engram-release-golden-path python3 scripts/e2e_golden_path.py
```

Expected: Compose starts, host CLI connects, hook observation is submitted,
worker-created retrieval document is observed, future session context bundle is
returned with citations, and Compose stops cleanly. The first command stops the
base release project without deleting its named volumes. The isolated E2E
project alone deletes its disposable volumes during cleanup.

### 1.9 Frontend build

```bash
cd apps/frontend && pnpm install --frozen-lockfile && pnpm build
```

Expected: clean `pnpm build` with no type or lint errors. `pnpm lint`
(`eslint .`) is also available.

### 1.10 Stop Compose runtime

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

Expected: all services stopped, no output from `docker compose ... ps --format json`.
Named release volumes are preserved. The `restart: unless-stopped` policy
recovers unexpected exits but does not override this deliberate `down`.

## 2. Update CHANGELOG

- Move `[Unreleased]` entries in `CHANGELOG.md` into a new
  `## [<VERSION>] - YYYY-MM-DD` section.
- Reset `[Unreleased]` to empty `Added`/`Changed`/`Fixed` sections.
- Verify every slice merged since the last release is represented (cross-check
  against the merged PRs / `git log`).

## 3. Tag Version

- Choose a semantic version. V1 is `1.0.0`.
- Create an annotated tag and push it (git owner only):

```bash
git tag -a v<VERSION> -m "Engram <VERSION>"
git push origin v<VERSION>
```

## 4. Build Images

Pushing the `v<VERSION>` tag in step 3 triggers
`.github/workflows/publish-images.yml`, which builds the backend and frontend
images and pushes them to `ghcr.io` with signed SLSA build-provenance
attestations. To build the images locally instead:

```bash
docker compose -f deploy/compose/docker-compose.yml build api worker-realtime worker-near-realtime worker-batch worker-highmemory worker-domain-events beat relay frontend
```

Confirm the `publish-images.yml` run for the tag succeeded and record the
published image digests in the release status report.

## 5. Publish Plugin Repository

Publish installable Claude Code and Codex plugin packages from
`plugin-repository/` and `packages/claude-plugin`, `packages/codex-plugin`.
Per `goal.md`, plugin repository releases must include:

- signed manifests or verifiable checksums;
- version pinning and agent-runtime compatibility ranges;
- update channels and revoked-version handling;
- CI and security scan evidence for each published package.

Confirm `engram connect` + `engram doctor` work against the published packages
before announcing the release.

## 6. Post-Release Verification

Re-run the fresh-clone gate (step 1) against the tagged image and the published
plugin packages, including the read-only Compose-contract mount and
`ENGRAM_COMPOSE_CONTRACT_PATH` from step 1.3. Specifically verify:

- local Docker Compose startup from the published images;
- migrations apply against a clean PostgreSQL volume;
- CLI install/connect/doctor/disconnect from the published plugin packages;
- worker and package-outbox relay process an observation end to end;
- hook ingest accepts session-start, post-tool-use, error, and decision events;
- authorized context-bundle retrieval returns citations and audit evidence;
- search, memory version, memory links, observations, and inspection endpoints
  respond under scoped API keys;
- frontend admin UI loads health and memories pages;
- Prometheus `/-/metrics` endpoint responds.

Record the release-candidate SHA, OS, Docker version, exact commands, exit
codes, and first decisive failure (if any) in the release status report. Do not
announce the release until every gate is green.
