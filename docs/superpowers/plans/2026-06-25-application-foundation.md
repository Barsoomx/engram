# Application Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the reusable Engram backend application foundation copied from the private backend reference with repository-local naming.

**Architecture:** Keep the new foundation under `engram.core`, wire it through Django settings, and leave existing feature services unmigrated in this checkpoint. Celery receives the copied queue/configuration patterns while preserving Engram task discovery.

**Tech Stack:** Django, DRF, Celery, django-celery-outbox, Pydantic v2, structlog, django-structlog, structlog-sentry, sentry-sdk, pytest, Ruff.

## Global Constraints

- Copy the private backend reference foundation intentionally; only adapt imports, Engram names, environment defaults, and private branding.
- Do not introduce the private source product name into tracked Engram files.
- Do not migrate existing Engram feature services to the foundation in this slice.
- Run focused backend tests, Ruff, repository layout, repository quality, and Compose verification before completion.

---

### Task 1: Domain, Use Case, Error, And Observability Foundation

**Files:**
- Create: `apps/backend/engram/core/domain/**`
- Create: `apps/backend/engram/core/middlewares/**`
- Create: `apps/backend/engram/core/observability/**`
- Create: `apps/backend/settings/logs.py`
- Modify: `apps/backend/settings/settings.py`
- Test: `apps/backend/engram/core/application_foundation_tests.py`

**Interfaces:**
- Produces: `BaseUseCase`, `UseCaseTransactional`, `DomainError`, `DomainEvent`, `EventStore`, `custom_exception_handler`, `ExceptionHandlingMiddleware`, `configure_logger`.

- [ ] Write focused failing tests for DTOs, domain errors, middleware payloads, event dispatch, transactional ordering, and observability filters.
- [ ] Run `cd apps/backend && poetry run pytest engram/core/application_foundation_tests.py -v` and confirm missing foundation imports fail.
- [ ] Copy/adapt the foundation modules and Django settings hooks.
- [ ] Re-run the focused test command until it passes.

### Task 2: Celery Application Foundation

**Files:**
- Create: `apps/backend/engram/celeryconfig.py`
- Create: `apps/backend/engram/celery_app.py`
- Create: `apps/backend/engram/celery_bootsteps.py`
- Create: `apps/backend/engram/core/retryable_django_task.py`
- Create: `apps/backend/engram/core/redis_sentinel.py`
- Modify: `apps/backend/engram/__init__.py`
- Test: `apps/backend/engram/core/celery_foundation_tests.py`

**Interfaces:**
- Produces: `engram.celery_app.app`, queue constants, quorum `task_queues`, confirm-publish broker options, readiness/liveness probe files.

- [ ] Write focused failing Celery configuration tests.
- [ ] Run `cd apps/backend && poetry run pytest engram/core/celery_foundation_tests.py -v` and confirm missing Celery foundation imports fail.
- [ ] Copy/adapt Celery config, app, and liveness bootstep modules.
- [ ] Re-run the focused Celery test command until it passes.

### Task 3: Contracts And Verification

**Files:**
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/poetry.lock`
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`

**Interfaces:**
- Produces: dependency lock coverage and repository contract coverage for the foundation.

- [ ] Lock dependencies with Poetry.
- [ ] Add repository layout and settings-contract assertions for the new files.
- [ ] Run focused backend tests, Ruff, repository tests, quality scripts, and Compose backend verification.
- [ ] Commit with `feat: add application foundation`.
