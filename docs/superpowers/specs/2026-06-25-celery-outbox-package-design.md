# Celery Outbox Package Design

## Decision

Engram must use `django-celery-outbox` as the durable Celery transport package
instead of carrying a custom Engram `OutboxEvent` domain model.

The hook ingest path persists the domain records it owns, then calls the memory
worker Celery task with stable domain ids:

1. write `RawEventEnvelope`, `Observation`, and `ObservationSource` in one
   database transaction;
2. call `process_observation_recorded.delay(str(observation.id))` in the same
   transaction;
3. let `django-celery-outbox` persist the queued `CeleryOutbox` row;
4. let the package relay publish the Celery message to the worker;
5. let the worker load the `Observation` by id and create or reuse the
   `MemoryCandidate`.

There is no custom `OutboxEvent`, `OutboxStatus`, `MemoryCandidateCreated`
domain outbox row, or manual observation-outbox processing command in this
checkpoint.

## Scope

This checkpoint removes the custom app outbox from the backend runtime:

- remove `OutboxEvent` and `OutboxStatus` from `engram.core.models`;
- add a migration that drops the old `core_outboxevent` table;
- remove hook response `outbox_event_id`;
- replace worker input `outbox_event_id` with `observation_id`;
- keep `django_celery_outbox.models.CeleryOutbox` assertions where they prove
  `.delay(...)` used the package transport;
- keep the Compose relay service because it belongs to the package transport;
- remove the manual `engram_process_observation_outbox` command.

Historical specs and migrations may still mention prior checkpoints, but live
runtime docs and verification evidence must describe the package-backed flow.

## Non-Goals

- Do not build another relay, dispatcher, polling command, or domain-event
  framework.
- Do not add provider/model-policy work.
- Do not add frontend, MCP, or admin UI work.
- Do not broaden memory quality workflows beyond the existing candidate
  creation path.

## API Behavior

Hook ingest responses remain `202 Accepted` and include:

- `status`;
- `duplicate`;
- `request_id`;
- `raw_event_id`;
- `observation_id`;
- `agent_session_id`.

They no longer include `outbox_event_id`.

Duplicate hook submissions must not create duplicate raw events, observations,
memory candidates, or package `CeleryOutbox` rows.

## Worker Behavior

`ProcessObservationRecorded` accepts an `observation_id`.

The worker:

1. loads and locks the `Observation` with organization, project, team, and raw
   event relations;
2. creates or reuses one `MemoryCandidate` keyed by the existing content hash;
3. stores redacted evidence containing `observation_id`, `raw_event_id`,
   `event_type`, `title`, `files_read`, and `files_modified`;
4. returns `duplicate=True` when the candidate already existed;
5. raises a redacted `MemoryWorkerError` for missing or malformed observations.

Retry and dead-letter behavior belongs to Celery plus `django-celery-outbox`,
not to an Engram model.

## Verification

Required local verification:

- focused hook ingest tests prove `.delay(str(observation.id))` writes exactly
  one `CeleryOutbox` row with only id payload;
- focused memory worker tests prove candidate creation, duplicate delivery,
  redaction, task delegation by observation id, and malformed id failures;
- backend test suite passes;
- migration freshness passes;
- Docker Compose golden path passes with the package relay and real Celery
  worker;
- repository quality tests pass without layout assertions for the deleted manual
  outbox command.
