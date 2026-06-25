# Celery Outbox Package Security Review

Date: 2026-06-25

Branch: `fix/use-celery-outbox-package`

Scope:

- hook ingest queueing;
- Celery task payloads;
- memory worker candidate creation;
- custom outbox model removal;
- Compose package relay path;
- repository evidence updates.

Commands and evidence:

| Check | Result |
| --- | --- |
| focused host backend | Exit 0. `cd apps/backend && poetry run pytest engram/core/core_models_tests.py engram/hooks/hook_ingest_tests.py engram/memory/memory_worker_tests.py -v` ran 57 tests. |
| full host backend | Exit 0. `cd apps/backend && poetry run pytest -v` ran 127 tests. |
| container focused backend | Exit 0. Compose Postgres ran 38 hook/worker tests after dev dependencies were installed in the ephemeral container. |
| container full backend plus lint | Exit 0 for pytest, ruff, and format in Compose Postgres. |
| container migrations | Exit 0. `core.0003_delete_outboxevent` applied and migration freshness reported `No changes detected`. |
| Compose golden path | Exit 0. Package relay delivered the observation-id task and future context included the generated memory. |
| repository checks | Exit 0 for layout, text quality, unit repository tests, and whitespace. |

Findings:

- No Critical findings.
- No Important findings.

Security checks:

- Hook responses no longer expose a transport outbox id.
- The package `CeleryOutbox` task payload contains only the accepted
  `observation_id`; tests assert raw API keys and provider-shaped secrets are
  absent from queued args, kwargs, and options.
- Duplicate hook replay returns existing domain rows and does not enqueue a
  second package transport row.
- Wrong-project and validation failures still occur before domain rows or
  package transport rows are created.
- The memory worker loads the observation by id, locks only the observation row
  on Postgres, and writes redacted candidate content/evidence.
- Malformed task ids raise `MemoryWorkerError('malformed observation id')`
  instead of leaking a raw UUID parser exception through worker logs.
- Compose retains `python manage.py celery_outbox_relay` only as the
  `django-celery-outbox` package relay, not as an Engram domain outbox worker.

Accepted risk:

- `core.0003_delete_outboxevent` deletes the old custom `core_outboxevent`
  table. This is intended for the package-transport refactor, but any deployed
  environment with pending custom outbox rows must drain or snapshot those rows
  before applying the migration.
