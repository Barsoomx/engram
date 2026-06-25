# Celery SLA Compose Topology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or equivalent delegated TDD implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Compose run the reference-style Celery SLA queues that Engram already declares.

**Architecture:** Keep `engram.celeryconfig` and `engram.celery_app` as the
application-level Celery foundation. Change Compose to run dedicated workers
with explicit `-Q` bindings for each Engram SLA queue.

**Tech Stack:** Docker Compose, Celery, RabbitMQ quorum queues, Redis result backend, `django-celery-outbox`, repository contract tests.

## Global Constraints

- Preserve `django-celery-outbox`; do not reintroduce a custom domain outbox.
- Preserve RabbitMQ `confirm_publish` and quorum queue settings.
- Use Engram queue names, not product-specific reference queue names.
- Do not copy product-specific beat schedules or deployment secrets.
- Keep the package relay separate from worker services.

---

### Task 1: Compose SLA Workers

**Files:**
- Modify: `deploy/compose/docker-compose.yml`
- Modify: `deploy/compose/README.md`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Create: `docs/superpowers/specs/2026-06-25-celery-sla-compose-topology-design.md`
- Create: `docs/superpowers/plans/2026-06-25-celery-sla-compose-topology.md`
- Modify: `docs/verification-matrix.md`

**Interfaces:**
- Produces: `worker-realtime`
- Produces: `worker-near-realtime`
- Produces: `worker-batch`
- Produces: `worker-highmemory`
- Produces: `worker-domain-events`

- [x] Delegate compose/test implementation to a worker with disjoint write scope.
- [x] Write failing repository contract for dedicated SLA worker services and `-Q` queue bindings.
- [x] Implement Compose services adapted from the reference worker topology.
- [x] Update Compose README with the worker queue split.
- [x] Run repository contract tests.
- [x] Run backend Celery tests.
- [x] Run Compose checks that prove the updated service graph is valid.
- [x] Record verification evidence.
- [x] Commit with `chore: add celery sla compose workers`.
