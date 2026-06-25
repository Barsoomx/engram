# Backend Health And Compose Design

## Context

Engram has a committed upstream parity map, reference-gates document, and tested
monorepo skeleton. The next roadmap gate is the first backend/runtime slice:
create a real Django backend project, expose health endpoints, and add local
Compose wiring for the backend deployment shape.

Current live state for this design:

- branch: `feat/parity-03-backend-health-compose`;
- base checkpoint: `3e289d7a24070e7139338ba1258a0047dec728af`;
- `origin/master`: `5be75e333fd982f1ecb82a0277c7e273559a28e6`;
- `upstream`: `3fe0725a97e18b5edf3e61cde60e181ab2b6c997`;
- pre-existing local dirty file: `.gitignore`.

Local tool state:

- Python 3.12.3 is available.
- Poetry 1.8.2 is available.
- Docker and Docker Compose are not available in this WSL distro, so Compose can
  be authored and statically verified here, but live Compose boot must be
  recorded as not run until Docker integration is enabled.

## Reference Evidence

The outbox reference was inspected at
`3fd9c76a16b082ca1cbf29f1358f6baa2ed79f5e` on branch `update-codecov`.
The useful patterns for this slice are:

- example Django app shape with `manage.py`, project settings, URLs, and app
  package;
- HTTP health endpoint used by Compose healthcheck;
- Postgres service health through `pg_isready`;
- CI smoke shape using `docker compose up -d --wait` and `curl -f`.

The private backend reference was inspected at
`6613ced5a8fb3e3802c3e0c2c6d61f524f977264` on branch `staging`. The useful
patterns for this slice are:

- Poetry project under the backend root with Python 3.12 and Django 5.2;
- runtime settings module plus test settings module;
- pytest discovery for `*_tests.py`;
- Ruff with single quotes and Python 3.12 target;
- health routes under `/-/healthz/`, `/-/readyz/`, and `/-/startup/`;
- container-first test command shape once Compose exists.

Do not copy private package-index configuration, product names, domain sprawl,
custom middleware, or production deployment details from the reference.

## Approved Direction

Build one narrow backend runtime checkpoint:

- `apps/backend` becomes a real Django backend root;
- runtime settings live under `apps/backend/settings`;
- a minimal internal app under `apps/backend/engram/health` owns health views
  and tests;
- endpoints are:
  - `/-/healthz/`: process liveness, no database query;
  - `/-/readyz/`: database readiness check;
  - `/-/startup/`: Django startup/database check;
- Poetry, pytest, pytest-django, Ruff, Django, DRF, PostgreSQL driver, Redis
  client, Celery, and Gunicorn are declared in `apps/backend/pyproject.toml`;
- Dockerfile builds one backend image;
- Compose defines `api`, `worker`, `postgres`, and `redis` using that image;
- the worker is a real Celery worker backed by Redis, not a fake long-running
  shell command;
- CI gains a backend workflow for Poetry install, Ruff, and pytest.

The continuation request keeps the active goal moving and serves as approval to
proceed with this narrow backend slice.

## Alternatives Considered

### Health Endpoint Only

This would be very small, but it would leave the deployment shape unproven and
would not advance the Compose gate required by `goal.md`.

### Full Hook Ingest And Outbox Now

This would start the real parity loop faster, but it mixes too many contracts:
identity, API keys, raw event storage, outbox, Celery idempotency, and context
retrieval. Those require their own service-boundary tests and security review.

### Backend Health Plus Real Compose Shell

This is selected. It creates the smallest runtime base that later hook,
outbox, and context slices can build on, while keeping behavior verifiable.

## Architecture

Backend files live under `apps/backend`.

Runtime layout:

- `manage.py`: Django command entrypoint.
- `settings/settings.py`: environment-driven runtime settings.
- `settings/test_settings.py`: test-specific overrides.
- `settings/urls.py`: root URL routing.
- `settings/wsgi.py` and `settings/asgi.py`: deployment entrypoints.
- `engram/celery_app.py`: Celery application configured from Django settings.
- `engram/health/views.py`: liveness/readiness/startup views.
- `engram/health/urls.py`: health route module.
- `engram/health/health_tests.py`: pytest coverage for the three endpoints.
- `Dockerfile`: backend API/worker image.
- `deploy/compose/docker-compose.yml`: local runtime with API, worker,
  PostgreSQL, and Redis.
- `deploy/compose/.env.example`: non-secret local development values.

Settings use explicit environment variables for:

- `ENGRAM_SECRET_KEY`;
- `ENGRAM_DEBUG`;
- `ENGRAM_ALLOWED_HOSTS`;
- `ENGRAM_DATABASE_URL`;
- `ENGRAM_REDIS_URL`;
- `ENGRAM_CELERY_BROKER_URL`;
- `ENGRAM_CELERY_RESULT_BACKEND`;
- `ENGRAM_LOG_LEVEL`.

The first settings implementation may parse database and Redis URLs locally.
It must not introduce provider secrets, API keys, model policy, RBAC tables, or
hook ingest schemas.

## Health Semantics

`/-/healthz/` returns HTTP 200 and JSON showing the process is alive. It must
not query the database so load balancers can use it during dependency outages.

`/-/readyz/` runs a minimal database query. It returns HTTP 200 when the
database is reachable and HTTP 503 when not ready. It is the API service
readiness check.

`/-/startup/` uses the same database-backed check as readiness for now. Later
slices can add migration, queue, secret-store, and model-policy checks when
those components exist.

Responses expose only operational status and component names. They do not echo
connection strings, credentials, raw exceptions, environment variables, or
tenant data.

## Compose Semantics

Compose is the local and first on-premise runtime shell.

Services:

- `postgres`: PostgreSQL 16 Alpine with `pg_isready` healthcheck.
- `redis`: Redis 7 Alpine with `redis-cli ping` healthcheck.
- `api`: backend image, waits for Postgres and Redis health, runs migrations,
  then starts Gunicorn on port 8000.
- `worker`: same backend image, waits for Postgres and Redis health, runs a
  Celery worker using Redis as broker.

The API service healthcheck calls `/-/readyz/`.

Because Docker is unavailable locally in this environment, this slice verifies
Compose with static tests and records the live boot command as not run. When
Docker is available, the required live command is:

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build --wait
```

## Testing Strategy

Use TDD:

1. Add tests for health endpoint behavior before adding health views.
2. Add settings/URL tests before finalizing Django routing.
3. Add static tests for Dockerfile and Compose service contracts before writing
   Compose.
4. Add workflow tests before adding the backend CI workflow.

Backend behavior tests use pytest and pytest-django inside `apps/backend`.
Repository-level workflow and Compose contract tests remain stdlib `unittest`
under root `tests/repository`.

## Deferred Work

This checkpoint intentionally does not add:

- hook ingest endpoints;
- API key or agent token models;
- tenancy/RBAC models;
- raw event, observation, memory, retrieval, audit, or outbox tables;
- provider secret storage;
- model provider adapters;
- context bundle generation;
- frontend runtime;
- live Docker verification in this WSL environment.

Those belong to later parity slices after this backend shell is green.

## Success Criteria

- `poetry install` succeeds in `apps/backend`.
- Backend tests pass with `poetry run pytest`.
- Ruff check and format check pass in `apps/backend`.
- Root repository tests pass.
- Health endpoints are covered by tests.
- Compose contract tests prove API, worker, PostgreSQL, Redis, healthchecks,
  and backend image wiring are present.
- Backend CI workflow calls Poetry install, Ruff, pytest, and root repository
  checks.
- Live Docker command is either run successfully or explicitly recorded as not
  run because Docker is unavailable.
