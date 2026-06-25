# Compose Golden Path Design

## Goal

Prove the rewritten `claude-mem` product loop through Docker Compose:
connect a thin client, submit a hook observation, create memory from it, approve
and index that memory, and retrieve it in a future session context bundle.

This slice is the first E2E proof for roadmap item 12. It stays inside the
parity gate and does not add a frontend, MCP bridge, semantic retrieval,
provider model calls, native Claude Code settings edits, real Codex managed
hook installation, package publishing, or production Helm.

## Current Gap

The backend and CLI now have most of the individual pieces:

- Compose starts API, worker, PostgreSQL, and Redis.
- The backend can create hook observations and outbox rows.
- The memory worker can turn an `ObservationRecorded` outbox row into a
  `MemoryCandidate`.
- The context service can index approved memory and return cited context.
- The CLI can connect, diagnose, and disconnect.

The missing links are:

- a test/operator bootstrap command that creates organization, team, project,
  identity, API key, and scoped capabilities;
- a thin CLI hook command that can submit hook JSON using the connected local
  config;
- an explicit approval/promotion command for the golden path, because the
  current memory worker creates candidates rather than auto-approved memory;
- a Compose E2E script and CI workflow that exercise the full loop.

## Decision

Add a single coherent E2E checkpoint:

1. Backend management command `engram_bootstrap_golden_path` creates an
   idempotent demo organization/team/project/API-key scope for the test.
2. CLI command `engram hook post-tool-use` reads hook JSON from stdin, merges
   connected config metadata, and posts `/v1/hooks/post-tool-use`.
3. Backend management command `engram_process_observation_outbox` processes
   pending `ObservationRecorded` outbox rows through the existing memory worker
   service.
4. Backend service and command `engram_promote_memory_candidate` approve one
   candidate, create `Memory` and `MemoryVersion`, mark the candidate promoted,
   and call `IndexMemoryVersion`.
5. CLI command `engram hook session-start` reads request JSON from stdin, calls
   `/v1/context/session-start`, and writes response JSON to stdout.
6. `scripts/e2e_golden_path.py` orchestrates Docker Compose and the commands
   above from a clean local config directory.
7. A GitHub Actions `Compose E2E` workflow runs the script on pull requests and
   pushes to `master`.

Local WSL currently cannot run Docker, so local verification records
`docker compose version` as blocked. The E2E workflow is the live Compose proof
for this checkpoint.

## Golden Fixture

The E2E uses one deterministic observation:

- repository root: `/workspace/engram`;
- branch: `master`;
- file path: `apps/backend/engram/hooks/services.py`;
- command: `pytest engram/hooks/hook_ingest_tests.py -v`;
- observation title: `Hook ingest replay handling is stable`;
- observation body: `The hook ingest path reuses accepted replay rows and keeps request ids idempotent.`

After processing and promotion, the future session-start request asks for the
same file path and query terms. The expected response contains:

- `status == "created"`;
- `purpose == "session_start"`;
- at least one context item;
- citation `M1`;
- the promoted memory title/body;
- `hook_specific_output.hookEventName == "SessionStart"`;
- no raw API key.

## Backend Bootstrap Command

`python manage.py engram_bootstrap_golden_path --api-key KEY --json` is
idempotent. It creates or reuses:

- organization slug `engram-e2e`;
- team slug `platform`;
- project slug `backend`;
- project/team link;
- service-account identity `golden-path-agent`;
- developer organization membership and project grant;
- project/team-scoped API key with `observations:write` and `memories:read`.

The command outputs JSON with organization, team, project, identity, and API key
ids. It does not print the raw API key.

## Hook CLI Commands

`engram hook post-tool-use`:

- reads a JSON object from stdin;
- loads connected config and credential;
- sends `POST /v1/hooks/post-tool-use`;
- fills `project_id`, `team_id`, `agent_runtime`, and `agent_version` from the
  selected runtime/config unless the stdin payload narrows them consistently;
- supplies defaults for `event_type`, `payload_schema_version`,
  `idempotency_key`, `event_id`, `request_id`, and `content_hash`;
- prints server response JSON to stdout;
- redacts the active API key from errors.

`engram hook session-start` follows the same local config/credential rules and
calls `POST /v1/context/session-start`.

Both commands are thin adapters. They do not cache memory, store hook payload
content on disk, run background workers, or call model providers.

## Promotion Command

`engram_promote_memory_candidate` is the explicit approval step for the first
golden path. It is not a general curation workflow.

Inputs:

- `--candidate-id ID`, or
- `--project-id ID --latest`.

Behavior:

- load a proposed candidate in scope;
- create or reuse an approved `Memory`;
- create or reuse version `1` with the candidate body;
- set `candidate.status = promoted` and `promoted_memory`;
- index the memory version with `IndexMemoryVersion`;
- output candidate, memory, version, and retrieval document ids.

## Compose E2E Script

`scripts/e2e_golden_path.py`:

1. ensures `deploy/compose/.env` exists by copying `.env.example` when needed;
2. runs `docker compose up -d --build --wait` from `deploy/compose`;
3. runs bootstrap inside the API container;
4. runs the host CLI `connect`;
5. runs the host CLI `hook post-tool-use`;
6. runs outbox processing inside the worker container;
7. runs candidate promotion inside the worker container;
8. runs the host CLI `hook session-start`;
9. asserts the response has the expected cited memory;
10. runs `docker compose down -v` in a `finally` block.

The script prints compact progress and command failures. It exits nonzero on
any missing assertion.

## Test Plan

Local non-Docker tests:

- backend management command tests for bootstrap, outbox processing, promotion,
  idempotency, and no raw key output;
- CLI hook command tests with fake transport for post-tool-use, session-start,
  default fields, key redaction, and malformed local state;
- repository tests requiring the E2E script and workflow.

Live Docker proof:

- `python3 scripts/e2e_golden_path.py` in the `Compose E2E` workflow.

## Boundaries

This slice owns:

- first item-12 E2E proof;
- test/operator bootstrap command;
- explicit promotion command for the golden path;
- thin CLI hook adapters for post-tool-use and session-start;
- Compose E2E script and CI workflow.

This slice defers:

- native Claude Code and Codex config mutation;
- hook response compatibility for every event type;
- MCP install and tools;
- automatic candidate approval policy;
- semantic/vector retrieval;
- real model-provider memory distillation;
- durable outbox relay daemon and dead-letter UI;
- frontend/admin inspection.

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

Required CI:

- Backend;
- Repository Quality;
- Compose E2E.

Docker remains blocked locally until Docker Desktop WSL integration is enabled.

## Self-Review

- The slice proves the product loop with real server APIs and database state.
- The approval step is explicit and auditable instead of silently
  auto-promoting every candidate.
- The CLI remains thin and server-backed.
- Local Docker unavailability is handled by CI proof, not by weakening the E2E
  requirement.
