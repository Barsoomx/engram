# First Parity Gate Security Roll-Up

Date: 2026-06-25

Current outbox contract: Engram uses `django-celery-outbox package transport`.
Hook ingest queues `engram.memory.process_observation_recorded` with the
observation id through the Celery task `.delay(...)` call. The Compose relay is
the package transport relay, not an Engram domain outbox processor.

Scope:

- access/API-key/RBAC scope resolution;
- hook ingest validation, idempotency, redaction, and outbox enqueueing;
- context retrieval authorization-before-ranking and audit records;
- upstream importer tenant isolation, redaction, idempotency, and unsupported
  record reporting;
- CLI and Codex plugin hook response contracts.

Out of scope until later checkpoints:

- native Claude Code plugin implementation;
- MCP bridge implementation;
- frontend/admin UI;
- provider-secret adapters and model-provider calls;
- semantic/vector retrieval breadth;
- signed plugin release channels;
- production deployment and public network exposure.

## Verdict

SECURITY APPROVED for the first Codex-led CLI/hooks/API parity gate after the
request-size-limit fix in this checkpoint.

## Findings

CRITICAL: none.

IMPORTANT:

- RESOLVED: authenticated hook/context request content had no Engram-level
  per-event/per-field caps before persistence and retrieval processing.

MINOR: none.

## Fix Evidence

Hook ingest now caps:

- nested hook payload JSON byte size;
- observation body length;
- observation `files_read` and `files_modified` list size and item length;
- `repository_url`, `repository_root`, and `cwd` length.

Context requests now cap:

- `agent_version`, `agent_external_id`, `correlation_id`, `trace_id`, and
  `branch` length;
- `query` length;
- `file_paths` and `symbols` list size and item length;
- `repository_url`, `repository_root`, and `cwd` length.

Regression tests prove oversized requests return HTTP 400 before durable writes
or context bundle creation.

## Commands

- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py engram/context/context_api_tests.py -v"` exit 0, 44 passed.
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check engram/hooks engram/context && ruff format --check engram/hooks engram/context"` exit 0.

TDD evidence:

- Oversized context repository metadata regression test first failed with HTTP
  200 instead of 400.
- Oversized context fixed-length metadata regression test first failed with a
  Django validation error on `AuditEvent.correlation_id`.
- After serializer validators were added, the focused single test passed.

## Residual Risks

- Limits are local serializer constants. Configurable tenant or deployment
  policy can be added later if operational evidence requires it.
- This approval does not cover deferred Claude Code, MCP, frontend/admin,
  provider-secret, semantic retrieval, signed release, or production exposure
  surfaces.
