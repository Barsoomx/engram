# Reference Gates

This document records the local reference repositories Engram will copy from,
what evidence was inspected, and when each gate applies.

Live state checked on 2026-06-25 from the Engram branch
`docs/parity-01-upstream-audit`:

- Engram `HEAD`: `5be75e333fd982f1ecb82a0277c7e273559a28e6`
- Engram `origin/master`: `5be75e333fd982f1ecb82a0277c7e273559a28e6`
- Engram `upstream`: `3fe0725a97e18b5edf3e61cde60e181ab2b6c997`
- Existing dirty file ignored for this audit: `.gitignore`

The reference repos are read-only inputs, not code to copy blindly.

## Gate Timing

Relevant now for the parity-map/docs slice:

- upstream behavior audit;
- reference-gates summary;
- Compose golden-path acceptance shape;
- repository governance and security checklist.

Relevant for backend and worker slices:

- Django settings and app boundaries;
- pytest, Ruff, migration, and Docker test style;
- durable outbox and Celery worker semantics;
- API/RBAC, audit, config, redaction, and observability gates.

Relevant for frontend slices:

- Next admin shell and dense operational UI patterns;
- auth/session and capability-gated screens;
- API client/query invalidation patterns;
- runtime env injection and frontend build/test gates.

Relevant for release slices:

- package artifact smoke tests;
- fresh install/fresh clone checks;
- publish, SBOM/license, dependency, and security scans.

## `django-celery-outbox`

Path:
`/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/django-celery-outbox`

Live branch at inspection time: `update-codecov...origin/update-codecov`.

Read first:

- `AGENTS.md`

### CI And Quality

Copy when Engram has a Python backend/package scaffold.

Evidence:

- `.github/workflows/tests.yml`
- `.github/workflows/codeql.yml`
- `.github/workflows/docs.yml`
- `.github/workflows/example.yml`
- `.github/workflows/publish.yml`
- `.github/workflows/stale.yml`
- `pyproject.toml`

Commands and jobs to mirror in Engram form:

- Ruff lint and format check.
- Type checking where package boundaries justify it.
- pytest unit/integration matrix.
- coverage reporting once tests exercise behavior.
- live broker smoke.
- parallel broker smoke.
- CodeQL/security checks.

### E2E And Compose

Copy immediately as the acceptance shape for the first Engram golden path:
Compose must start real dependencies and prove work drains through the async
pipeline.

Evidence:

- `.github/workflows/example.yml`
- `docker-compose.yml`
- `examples/minimal_django/docker-compose.yml`
- `examples/minimal_django/README.md`

Engram adaptation:

- Start PostgreSQL, Redis-compatible broker, backend API, worker, and CLI/test
  client.
- Submit a hook or CLI event.
- Verify the event persists.
- Verify async processing creates or updates memory.
- Verify a later context request receives authorized cited context.
- Verify queue/outbox backlog reaches the expected terminal state.

### Outbox And Worker Semantics

Use as the primary source for Engram's durable outbox behavior.

Evidence:

- `django_celery_outbox/models.py`
- `django_celery_outbox/relay/_message_selector.py`
- `django_celery_outbox/relay/_mutations.py`
- `django_celery_outbox/relay/_relay.py`
- `django_celery_outbox/relay/_config.py`
- `django_celery_outbox/management/commands/celery_outbox_relay.py`
- `docs/operations/runbook.md`
- `docs/operations/dead-letter.md`

Engram adaptation:

- Database rows are the source of truth for async work.
- Workers claim due work with database locking and skip-locked semantics.
- Retry, dead-letter, replay, and purge behavior must be explicit and tested.
- Management commands must be safe to run in containers and scripts.

### Packaging And Release

Defer until CLI/plugin/backend packages exist.

Evidence:

- `pyproject.toml`
- `MANIFEST.in`
- `scripts/smoke_installed_wheel.py`
- `scripts/check_release_contract.py`
- `.github/workflows/publish.yml`

Engram adaptation:

- Backend package/image, CLI package, and plugin packages each need artifact
  smoke tests before release.
- Release checks must inspect built artifacts rather than only rebuilding from
  source.

### Governance And Operations

Copy now as checklist items; implement as the matching Engram surfaces exist.

Evidence:

- `CODEOWNERS`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `.github/dependabot.yml`
- `docs/configuration.md`
- `docs/operations/runbook.md`

Engram adaptation:

- Keep ownership, security reporting, dependency review, and runbook coverage
  visible from the start.

## `altyn-backend`

Path:
`/mnt/c/Users/filipp/Desktop/gena/altyn-backend`

Live branch at inspection time: `staging...origin/staging`; the worktree had
untracked files. Treat it as a read-only architecture reference, not as a clean
release snapshot.

Read first:

- `AGENTS.md`
- `CLAUDE.md`

### Backend Layout And Settings

Copy for the first Django backend slice.

Evidence:

- `Dockerfile`
- `Dockerfile.codex`
- `docker-compose.yml`
- `docker-compose.ci.yml`
- `.env.example`
- `app/manage.py`
- `app/pyproject.toml`
- `app/pytest.ini`
- `app/settings/settings.py`
- `app/settings/test_settings.py`
- `app/settings/utils.py`
- `app/settings/logs.py`
- `app/settings/redis.py`

Engram adaptation:

- Backend commands should run inside containers once Compose exists.
- Settings should be env-driven, typed where practical, and container-friendly.
- Logging, Redis, tracing, and error handling should be configured before the
  worker path is treated as production-grade.

### API, RBAC, And Domain Style

Copy when implementing DRF APIs and authorization checks.

Evidence:

- `app/settings/urls.py`
- `app/apps/manager/permissions.py`
- `app/apps/manager/views/brand_settings.py`
- `app/apps/manager/serializers/brand_settings.py`
- `app/apps/manager/tests/brand_settings_tests.py`

Engram adaptation:

- Views and serializers stay thin.
- Authorization decisions are explicit and testable.
- Negative cross-tenant tests are mandatory for ingest, search, context, and
  admin/API access.

### Worker And Domain Events

Use for Celery layout and operational patterns, while using
`django-celery-outbox` as the stronger durable outbox source.

Evidence:

- `app/apps/celery_app.py`
- `app/apps/celeryconfig.py`
- `app/apps/core/domain/event_dispatcher.py`
- `app/apps/core/domain/event_dispatcher_tests.py`
- `app/apps/lk/subscriber/management/commands/outbox_worker.py`
- `helm/values/altyn-backend-outbox-worker.yaml.gotmpl`

Engram adaptation:

- Celery tasks reload authoritative database state.
- Idempotency keys and tenant/project scope are part of every durable job.
- Domain events must be replayable and observable.

### Backend CI And Tests

Copy once backend code exists.

Evidence:

- `.gitlab-ci.yml`
- `app/pytest.ini`
- `app/conftest.py`
- `TESTING.md`

Command shapes to adapt:

- `docker compose -f docker-compose.yml -f docker-compose.ci.yml ...`
- `pytest -n auto --reuse-db`
- sequential transactional test jobs for migration/race cases.
- Ruff check/format in the backend container.

### Deployment And Config Contracts

Defer until deployment assets exist.

Evidence:

- `helmfile.yaml`
- `helm/values/altyn-backend-api.yaml.gotmpl`
- `helm/values/altyn-backend-celery-domain-events.yaml.gotmpl`
- `helm/values/altyn-backend-celerybeat.yaml.gotmpl`
- `app/settings/tests/test_helm_environment_contracts.py`

Engram adaptation:

- Deployment config must have tests that prove required environment variables,
  probes, and worker roles stay wired.

## `asgard-admin`

Path:
`/mnt/c/Users/filipp/Desktop/gena/frontend/asgard-admin`

Live branch at inspection time:
`feature/client-group-commission...origin/feature/client-group-commission`.

Read first:

- `AGENTS.md`

### Frontend Build And Test Gates

Copy for the frontend/admin UI slice, not for the first backend parity gate.

Evidence:

- `.gitlab-ci.yml`
- `Dockerfile`
- `Dockerfile.dev`
- `docker-compose.yml`
- `docker-compose.override.yml`
- `app/package.json`
- `app/next.config.js`
- `app/next-config.test.mjs`
- `app/lib/*.test.mjs`
- `app/components/ui/*test.mjs`

Command shapes to adapt:

- `npm ci` in that repo, but Engram uses `pnpm` unless a package boundary
  explicitly records otherwise.
- lint.
- typecheck.
- unit tests with `node:test`.
- Next production build.

### Runtime Env And Deploy

Copy when the Engram frontend image exists.

Evidence:

- `Dockerfile`
- `app/entrypoint.sh`
- `helmfile.yaml`
- `helm/values/asgard-admin-web.yaml.gotmpl`
- `app/.env.example`

Engram adaptation:

- Public frontend env must be intentionally injected.
- Secrets must not be exposed through `NEXT_PUBLIC_*`.
- Image build and runtime configuration need separate tests.

### Auth, API, Session, And Capabilities

Copy when Engram has an admin UI.

Evidence:

- `app/middleware.ts`
- `app/app/api/auth/[...nextauth]/options.ts`
- `app/service/_instances.ts`
- `app/service/manager.service.ts`
- `app/hooks/use-query/use-manager.ts`
- `app/app/providers.tsx`
- `app/lib/query-keys.ts`
- `app/components/auth/capability-gate.tsx`
- `app/lib/manager-capabilities.ts`

Engram adaptation:

- UI capability checks mirror backend scopes but do not replace backend
  authorization.
- API client and query keys should be predictable and testable.

### Admin UI Shape

Copy for dense operational screens after the parity E2E is green.

Evidence:

- `app/components/layout/app-shell.tsx`
- `app/components/layout/sidebar.tsx`
- `app/app/brand-settings/page.tsx`
- `app/components/operations/operations-table.tsx`

Engram adaptation:

- Build a work-focused memory/admin console, not a marketing dashboard.
- Prioritize tables, filters, detail panes, audits, and diagnostics.

## Engram Gate Checklist

Gate 1 (parity-01) satisfied this checklist on 2026-06-25; see
`docs/parity/2026-06-25-first-parity-gate-report.md`.

Before backend implementation begins:

- `docs/parity/claude-mem-parity-map.md` is committed.
- This `docs/reference-gates.md` is committed.
- The first backend plan states which reference gates it copies.

Before the first parity gate is declared complete:

- Compose golden path passes.
- Hook/CLI contract tests cover Claude Code and Codex or a committed parity
  rationale explicitly defers one.
- PostgreSQL stores raw events, normalized observations, generated memory,
  retrieval documents, and context-bundle audit records.
- Worker/outbox tests prove idempotency, retry, replay, and duplicate event
  handling.
- Context retrieval tests prove authorization-before-ranking and cited output.
- Migration compatibility has an idempotent fixture-backed test or an explicit
  unsupported-record report path.
