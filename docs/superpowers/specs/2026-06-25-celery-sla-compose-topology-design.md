# Celery SLA Compose Topology Design

## Goal

Align the local Compose worker topology with the reference Celery operational
pattern already copied into Engram's Celery foundation.

## Architecture

Engram already keeps the reference-style Celery settings under the Engram namespace:
quorum SLA queues, topic exchanges, `confirm_publish`, readiness files,
worker liveness bootstep, retryable task base, structlog integration, Redis
result backend, and the `django-celery-outbox` transport app.

This checkpoint makes the Compose runtime consume those queues explicitly.
Instead of one generic worker consuming the default queue implicitly, Compose
starts dedicated workers for:

- `engram-realtime`
- `engram-near-realtime`
- `engram-batch`
- `engram-highmemory`
- `engram-domain-events`

Each worker runs the same Engram Celery app and pins its queue with `-Q`.
The package outbox relay stays a separate service and remains the only process
that drains `django-celery-outbox` transport rows.

## Non-Goals

- No product-specific reference beat schedule copy.
- No reference queue names, task include list, Helm secrets, Grafana URLs, or
  Sentry DSN values.
- No change to memory-worker behavior, hook payloads, or domain event contracts.
- No new broker technology or custom outbox implementation.

## Verification

Repository contract tests prove the Compose file declares dedicated SLA worker
services, uses `-Q` with Engram queue names, and keeps the package relay
separate from worker runtime. Existing backend Celery tests continue to cover
quorum queues, `confirm_publish`, `OutboxCelery`, liveness, and structlog
integration.
