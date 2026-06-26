# Application Foundation Design

## Goal

Add a reusable backend application foundation for Engram services: Pydantic DTO
use cases, transactional use cases, domain errors, DRF error transport,
structured logging, Sentry hooks, and Celery worker configuration.

## Source Rule

The implementation intentionally mirrors the private backend reference instead
of inventing a smaller local abstraction. Engram adapts import paths, app names,
queue names, environment defaults, and private branding only where required by
this repository.

## Architecture

The foundation lives under `engram.core` and is importable without tying memory,
hooks, or context services to a concrete feature migration. Existing services do
not move to the new base classes in this slice; this keeps the checkpoint
reviewable while making the base unit available for later service work.

Domain events are collected in `EventStore` and dispatched through a singleton
dispatcher. Under pytest the dispatcher runs handlers in-process. Outside tests
it wraps handlers as Celery tasks and uses transaction-aware enqueueing.

DRF and Django middleware use one domain-error payload contract so API responses
stay consistent across view and middleware paths.

Logging and Sentry setup are centralized in `settings/logs.py` and
`engram.core.observability`. Django settings install `django_structlog`, request
context middleware, the domain exception middleware, and the DRF exception
handler.

Celery configuration defines explicit SLA queues, quorum queue arguments,
confirm-publish broker options, readiness/liveness files, JSON serializers, and
model serialization. Existing worker task discovery remains active.

## Verification

Focused backend tests cover DTO validation, domain-error metadata and payloads,
event dispatch, transactional lifecycle ordering, observability filters, and
Celery queue/app configuration. Repository layout tests require the new
foundation files and settings hooks so the slice cannot silently disappear.
