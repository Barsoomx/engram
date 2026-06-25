# Memory Candidate Worker Design

Supersession note (2026-06-25): this historical design described an
Engram-owned domain outbox worker. The live contract now uses
`django-celery-outbox package transport`; hook ingest queues
`engram.memory.process_observation_recorded` with the observation id through
the Celery task `.delay(...)` call. The package-owned relay may run
`python manage.py celery_outbox_relay`, but Engram no longer requires a custom
`OutboxEvent` worker command or outbox migration gate.

## Goal

Add the first server-side worker slice for the parity loop: consume durable
`ObservationRecorded` outbox events and create idempotent `MemoryCandidate`
records from accepted observations.

This slice is backend-only. It does not add provider calls, embeddings,
retrieval documents, context APIs, CLI behavior, frontend screens, MCP tools,
manual approval workflows, or automatic memory promotion.

## Current Gate

The current roadmap item is "Add memory candidate model and worker skeleton."
The model already exists in `engram.core`; the missing behavior is the
server-side worker boundary that turns an accepted observation into a durable
memory candidate and records the next domain event for later indexing/context
work.

The hard parity gate eventually requires a worker to create or update useful
memory from captured activity. This checkpoint proves the authoritative
outbox/Celery path and candidate write. Later checkpoints can replace the
deterministic candidate builder with provider-backed distillation and can
promote/index approved memories.

## Approaches Considered

### Deterministic Candidate Builder

Create a small `engram.memory` app with one domain service and one Celery task.
The service reloads an `ObservationRecorded` outbox row, locks it, creates one
candidate from the linked observation, emits `MemoryCandidateCreated`, and marks
the source outbox row done.

Tradeoff: the candidate is simple, but the outbox/worker idempotency path is
real and testable without model-provider secrets or retrieval infrastructure.

### Provider Distillation In This Slice

Call a model provider from the worker and parse generated memory output.

Tradeoff: closer to the final product loop, but it requires provider policy,
secret resolution, redaction classification, retry semantics, and test doubles
that are not yet implemented. That would make this checkpoint too wide.

### Promote Memory Immediately

Create approved `Memory` and `MemoryVersion` rows directly from observations.

Tradeoff: it would help an E2E path sooner, but it bypasses the explicit
candidate/approval decision still required by `goal.md`. Promotion belongs with
the retrieval/context slice or an explicit golden-path decision.

## Decision

Create `engram.memory` with:

- `ProcessObservationRecorded.execute()` as the domain boundary;
- `process_observation_recorded_outbox` as the Celery task wrapper;
- focused pytest coverage for success, idempotency, wrong event handling,
  missing observation handling, and redaction-safe evidence payloads.

The worker accepts only an outbox event id. It treats the outbox row and
database state as authoritative; task payloads never include observation text or
raw hook payloads.

## Domain Service Behavior

`ProcessObservationRecorded.execute(outbox_event_id, worker_id)`:

1. locks the `OutboxEvent` row with `select_for_update()`;
2. requires `event_type == "ObservationRecorded"`;
3. increments attempts and marks the event `processing`;
4. reloads the `Observation` from `payload.observation_id`;
5. builds a deterministic candidate title/body from the observation;
6. creates or reloads a `MemoryCandidate` using a stable content hash derived
   from the observation id and observation content hash;
7. stores evidence with ids, event type, observation title, and redacted file
   metadata only;
8. emits a `MemoryCandidateCreated` outbox event with id-only payload;
9. marks the source outbox row `done` with `processed_at`;
10. returns the candidate, source outbox row, downstream outbox row, and a
    duplicate flag.

Duplicate delivery must be safe. If the source outbox row is already `done`,
the service reloads the existing candidate/downstream event and returns without
creating new rows. If the candidate or downstream event already exists while the
source row is not done, the service reuses them and then marks the source row
done.

Failure handling is intentionally narrow. If the outbox row is malformed or the
observation cannot be loaded in scope, the service marks the source row
`failed`, records a redacted `last_error`, sets `next_retry_at`, and raises a
domain error. Full bounded exponential backoff and dead-letter management are
deferred to the durable outbox dispatcher slice.

## Candidate Shape

The deterministic candidate uses existing core fields:

- `organization`, `project`, and `team` come from the observation;
- `source_observation` points at the observation;
- `title` is the observation title, truncated to 255 characters;
- `body` is observation body if present, otherwise the title;
- `status` is `proposed`;
- `visibility_scope` is `project`;
- `confidence` is `0.500`;
- `content_hash` is a SHA-256 hash of the observation id and content hash;
- `evidence` contains ids and redacted observation metadata.

This does not claim final memory quality. It creates a durable, explainable
candidate for later promotion/indexing.

## Boundaries

This slice owns:

- the memory worker app boundary;
- deterministic observation-to-candidate conversion;
- Celery task registration for the candidate worker;
- source outbox status transitions for this event type;
- downstream `MemoryCandidateCreated` outbox emission;
- repository gates requiring memory worker files.

This slice defers:

- generic outbox polling/relay commands;
- provider calls and prompt parsing;
- provider secrets and model policy resolution;
- embeddings and retrieval document writes;
- `Memory`/`MemoryVersion` promotion;
- context bundle APIs and session-start injection;
- CLI and hook adapter packages;
- frontend/admin memory review screens;
- dead-letter replay UI and full retry/backoff policy.

## Testing

Tests must prove behavior:

- an `ObservationRecorded` event creates one proposed memory candidate and one
  `MemoryCandidateCreated` outbox event;
- the source outbox row is marked `done` with attempts and processed timestamp;
- duplicate delivery returns the same candidate and does not create duplicate
  candidates or downstream outbox rows;
- already-created candidate/downstream rows can be reused if the source outbox
  row is still pending;
- malformed source events are marked `failed` and create no candidate;
- missing observation ids are marked `failed` and create no candidate;
- candidate evidence and downstream outbox payloads do not contain raw API keys,
  bearer tokens, hook payloads, or unredacted tool output;
- the Celery task wrapper delegates by outbox id and is autodiscoverable.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings`
- `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`
- `docker compose version`

Docker Compose smoke remains blocked until Docker is available in this WSL
distro.

## Self-Review

- No North Star expansion: retrieval, context APIs, frontend, MCP, provider
  routing, and custom admin workflows remain deferred.
- No local worker regression: the worker is server-side Celery only.
- No raw credential persistence: worker inputs and downstream outbox payloads
  are id-only, and candidate evidence uses redacted observation metadata.
- Idempotency is database-backed through stable candidate hashes and outbox
  source/idempotency constraints.
