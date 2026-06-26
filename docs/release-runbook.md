# Release Runbook

Step-by-step procedure for cutting an Engram release. Follow these gates in
order. Record the exact command, working directory, OS, Docker version, exit
code, and first decisive failure for each step in the release status report.

References:

- `docs/operations-and-deployment.md` for deployment profiles and runtime
  components.
- `docs/verification-matrix.md` for the per-slice verification evidence and
  commands used during the development cycle.
- `docs/client-installation.md` for client install and hook bootstrap flow.

## 0. Prerequisites

- A fresh clone in a new directory with no reused virtualenv, `node_modules`,
  database volume, build cache, or generated config.
- Docker with Compose v2 enabled.
- `python3`, `pnpm` (9.x via `corepack enable`), and `poetry` available on the
  host for repository-quality and frontend gates.
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

Expected: backend API, worker, relay, PostgreSQL, RabbitMQ broker, Redis result
backend, and the frontend service reach healthy state.

### 1.2 Apply migrations and verify freshness

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"
```

Expected: migrations apply cleanly and `No changes detected`.

### 1.3 Backend tests, lint, and format

```bash
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v && ruff check . && ruff format --check ."
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

```bash
PYTHONPATH=packages/mcp python3 -m unittest discover -s packages/mcp -p '*_tests.py' -v
python3 -m compileall packages/cli/engram_cli packages/mcp/engram_mcp
```

Expected: contract tests pass and both packages compile without syntax errors.

### 1.7 Claude Code and Codex plugin contract tests

```bash
python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v
python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v
```

Expected: both plugin contract suites pass.

### 1.8 Repository quality gates

```bash
python3 -m unittest discover -s tests -v
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
git diff --check HEAD
```

Expected: repository tests pass, layout and quality scripts exit with no output,
whitespace clean.

### 1.9 E2E golden path

```bash
python3 scripts/e2e_golden_path.py
```

Expected: Compose starts, host CLI connects, hook observation is submitted,
worker-created retrieval document is observed, future session context bundle is
returned with citations, and Compose stops cleanly.

### 1.10 Frontend build

```bash
cd apps/frontend && pnpm install --frozen-lockfile && pnpm build
```

Expected: clean `pnpm build` with no type or lint errors. `pnpm lint`
(`next lint`) is also available.

### 1.11 Stop Compose runtime

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

Expected: all services stopped, no output from `docker compose ... ps --format json`.

## 2. Update CHANGELOG

- Move `[Unreleased]` entries in `CHANGELOG.md` into a new
  `## [<VERSION>] - YYYY-MM-DD` section.
- Reset `[Unreleased]` to empty `Added`/`Changed`/`Fixed` sections.
- Verify every completed slice from `docs/verification-matrix.md` is represented.

## 3. Tag Version

- Choose a semantic version. V1 is `1.0.0`.
- Create an annotated tag and push it (git owner only):

```bash
git tag -a v<VERSION> -m "Engram <VERSION>"
git push origin v<VERSION>
```

## 4. Build Images

Build backend and frontend images from the tagged SHA with explicit tags:

```bash
docker compose -f deploy/compose/docker-compose.yml build api worker relay frontend
```

Tag and push the images to the operator registry per
`docs/operations-and-deployment.md`. Record the image digests in the release
status report.

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
plugin packages. Specifically verify:

- local Docker Compose startup from the published images;
- migrations apply against a clean PostgreSQL volume;
- CLI install/connect/doctor/disconnect from the published plugin packages;
- worker and package-outbox relay process an observation end to end;
- hook ingest accepts session-start, post-tool-use, error, and decision events;
- authorized context-bundle retrieval returns citations and audit evidence;
- search, memory version, memory links, observations, and inspection endpoints
  respond under scoped API keys;
- frontend admin UI loads health and memories pages;
- Prometheus `/metrics` endpoint responds.

Record the release-candidate SHA, OS, Docker version, exact commands, exit
codes, and first decisive failure (if any) in the release status report. Do not
announce the release until every gate is green.
