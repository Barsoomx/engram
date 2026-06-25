# CLI Lifecycle Design

## Goal

Add the first `engram` command-line client slice: `connect`, `doctor`, and
`disconnect` for an already-running Engram server.

This slice is CLI-only plus CI/docs wiring. It does not add hook adapters,
native Claude Code or Codex plugin manifests, MCP installation, token minting,
server deployment, migration import, provider calls, semantic retrieval, or the
Docker Compose golden path.

## Current Gate

Roadmap item 11 is the CLI lifecycle checkpoint. Previous checkpoints created
server-side hook dry-run, observation ingest, memory candidate generation,
retrieval documents, exact context bundle APIs, and the semantic retrieval
deferral record.

The next missing parity behavior is a thin local command that can connect a
developer machine to the Engram API, verify the resolved server scope, diagnose
the connection later, and remove only Engram-owned local state.

## Approaches Considered

### Python Standard-Library CLI Package

Create `packages/cli` as a Python package with no third-party runtime
dependencies. The command uses `argparse`, `urllib`, and JSON files under
`~/.engram` by default. Tests run with `python3 -m unittest` and inject a fake
HTTP transport.

Tradeoff: this is not a polished distribution package yet, but it keeps the
first client slice small, testable, and aligned with the Python/server rewrite.

### TypeScript CLI Package

Create a Node/TypeScript CLI with `pnpm`, test runner, bundler, and published
package metadata.

Tradeoff: this may be the right packaging target later for agent ecosystems,
but it adds a second toolchain and dependency policy before the first CLI
contract is proven.

### Backend Management Command

Expose `connect`, `doctor`, and `disconnect` as Django management commands.

Tradeoff: this avoids a separate package, but it violates the product boundary:
developer machines should not need backend internals or server dependencies to
connect hooks.

## Decision

Build a dependency-free Python CLI package under `packages/cli/engram_cli`.

The package exposes:

- `python -m engram_cli connect --server URL --api-key KEY --project PROJECT`;
- `python -m engram_cli doctor`;
- `python -m engram_cli disconnect`;
- a package script entry point named `engram` in `packages/cli/pyproject.toml`.

The hidden `--config-dir` option exists only to make tests and managed
installers deterministic. The default local state directory is
`$ENGRAM_HOME` when set, otherwise `~/.engram`.

## Connect Contract

`connect` accepts:

- `--server URL`;
- `--api-key KEY`;
- `--project PROJECT`;
- optional `--team TEAM`;
- optional `--agent codex|claude-code|both`, default `both`;
- optional `--agent-version VERSION`;
- optional `--config-dir PATH`.

The command:

1. normalizes the server URL by trimming a trailing slash;
2. calls `POST /v1/hooks/dry-run` once per selected runtime with the bearer API
   key, project id, optional team id, runtime, agent version, and generated
   request id;
3. fails before writing local state if any dry-run call fails;
4. writes Engram-owned local config and hook manifests only after dry-run
   succeeds;
5. writes the credential to a separate file with mode `0600`;
6. prints resolved organization, project, team, capabilities, selected
   runtimes, server URL, and redacted credential fingerprint.

Because the backend does not yet have a token-minting endpoint, the supplied
project-scoped API key is the temporary hook credential for this checkpoint.
This is acceptable only because it is an Engram API credential, not a model
provider secret, and it is stored in the strict-permission credential file.
The public config file stores only the redacted fingerprint.

## Local State Contract

Allowed files:

- `config.json`: server URL, project id, optional team id, selected runtimes,
  resolved scope, credential fingerprint, and connection timestamp;
- `credentials.json`: temporary hook credential and fingerprint, mode `0600`;
- `hooks/codex.json`: Engram-owned Codex hook manifest when Codex is selected;
- `hooks/claude_code.json`: Engram-owned Claude Code hook manifest when Claude
  Code is selected.

Hook manifests are internal Engram manifests for the first CLI lifecycle slice.
They intentionally do not edit real `~/.claude`, Codex managed-hook, or plugin
repository files. Native hook installation is a later adapter/package slice.

Forbidden local state:

- provider secrets;
- memory databases;
- embeddings;
- cached memory bundles;
- durable local event queues;
- persistent local workers;
- unredacted prompt or tool-output bodies.

## Doctor Contract

`doctor` is read-only. It never writes, repairs, or deletes state.

Required checks:

- config file exists and parses;
- credential file exists, parses, and contains a credential;
- every configured hook manifest exists and parses;
- `GET /-/healthz/` returns a JSON body with `status == "ok"`;
- `POST /v1/hooks/dry-run` succeeds for every configured runtime and project.

Exit code is `0` only when all required checks pass. Any required failure exits
`1` and prints the failing check code plus remediation.

## Disconnect Contract

`disconnect` removes only Engram-owned local state inside the selected config
directory:

- `config.json`;
- `credentials.json`;
- `hooks/codex.json`;
- `hooks/claude_code.json`;
- the empty `hooks/` directory when it becomes empty.

The command is idempotent. If no Engram state exists, it exits `0` and reports
that nothing was connected. It never removes files outside the config directory.

## Failure Taxonomy

The first CLI gate categorizes these failures:

- `missing_server_url`;
- `missing_api_key`;
- `missing_project`;
- `missing_config`;
- `missing_credential`;
- `missing_hook_config`;
- `server_unavailable`;
- `http_error`;
- `invalid_response`;
- access codes returned by the server, including `invalid_key`,
  `expired_key`, `missing_capability`, and `project_scope_denied`.

Each error prints a short remediation. Raw API keys and bearer tokens must never
appear in normal output, error output, config JSON, hook manifests, or test
assertion diffs.

## Test Plan

Use stdlib `unittest` with a fake transport and temporary config directory.
Tests cover:

- `connect` dry-run before writing files;
- config does not contain the raw key;
- credential file has mode `0600`;
- `connect --agent both` writes Codex and Claude Code manifests;
- failed dry-run writes no local state;
- `doctor` exits `0` for healthy config, health, and dry-run;
- `doctor` exits `1` without mutating state when config, credential, hook
  manifest, health, or dry-run checks fail;
- `disconnect` removes only Engram-owned files and is idempotent;
- command argument validation maps missing server, key, and project to the
  expected failure codes.

CI must run the CLI tests explicitly:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

## Boundaries

This slice owns:

- dependency-free Python CLI package skeleton;
- local config, credential, and Engram hook-manifest file handling;
- dry-run and health HTTP client;
- `connect`, `doctor`, and `disconnect` command behavior;
- CLI unit tests;
- CI and verification matrix updates for CLI tests.

This slice defers:

- real native Claude Code settings edits;
- real Codex managed-hook installation;
- hook stdin/stdout adapter command;
- MCP install;
- OS keychain integration;
- server-side token minting;
- offline retry envelopes;
- package publishing, signing, checksums, and update channels;
- Docker Compose E2E golden path.

## Verification

Required local commands:

- `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v`
- `python3 -m compileall packages/cli/engram_cli`
- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings`
- `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`
- `docker compose version`

Docker remains blocked in this WSL distro until Docker Desktop WSL integration
is available.

## Self-Review

- The CLI is a thin client over existing server APIs and introduces no local
  worker, local memory store, provider secret path, or cached context.
- The command surface matches roadmap item 11 and does not start the E2E gate.
- Native hook installation is explicitly deferred instead of faked through
  unknown agent-specific config formats.
