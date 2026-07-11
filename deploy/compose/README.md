# Compose Deployment

Local self-hosted profile for the parity backend runtime: API,
queue-specific Celery workers, relay, PostgreSQL, RabbitMQ broker,
Redis result/cache backend, and the Next.js admin frontend.

## Services

- `api`: Django + Granian (WSGI) on port `8000`. Healthcheck probes
  `http://localhost:8000/-/readyz/`. Worker count is tunable via
  `ENGRAM_WEB_WORKERS` (default `2`); each worker is bounded to `4` blocking
  threads, respawns on unexpected exit, honors a `35s` graceful-kill timeout,
  and respawns above `ENGRAM_WEB_MAX_RSS_MB` MiB (default `512`).
- `frontend`: Next.js admin console (`apps/frontend`) on port `3000`.
  Builds from the `apps/frontend` directory (see `apps/frontend/Dockerfile`,
  `pnpm start`). Server-side fetches reach the backend over the internal
  Compose network at `http://api:8000` via `NEXT_PUBLIC_ENGRAM_API_URL`.
  The home page and `/health` page call `${NEXT_PUBLIC_ENGRAM_API_URL}/-/healthz/`.
- `relay`: outbox relay shipping domain events to the broker.
- `beat`: Celery beat scheduler with its schedule stored on a durable volume.
- `worker-realtime`: `engram-realtime`
- `worker-near-realtime`: `engram-near-realtime`
- `worker-batch`: `engram-batch`
- `worker-highmemory`: `engram-highmemory`
- `worker-domain-events`: `engram-domain-events`
- `postgres`: PostgreSQL 18 (database/user/password `engram`).
- `redis`: Redis 7.4.0-v8 result/cache backend.
- `rabbitmq`: RabbitMQ 4.3.2 broker (user/pass `engram`, vhost `engram`).

The workers are split by the SLA queues listed above.

The Compose E2E workflow runs `scripts/e2e_golden_path.py` to prove the
first CLI/hook-to-context loop and `scripts/e2e_runtime_durability.py` to prove
the disposable runtime's loss-recovery contract.

## Runtime durability contract

Compose stores PostgreSQL, RabbitMQ, and the Beat schedule in the named
`postgres_data`, `rabbitmq_data`, and `beat_data` volumes. An ordinary
`docker compose down` preserves those volumes. `docker compose down -v` deletes
them and is destructive; use it only to reset disposable development or test
state, never as part of a release or recovery procedure.

RabbitMQ persistence requires a stable hostname/nodename pair. The defaults are
hostname `rabbitmq` and nodename `rabbit@rabbitmq`. The optional
`ENGRAM_RABBITMQ_HOSTNAME` and `ENGRAM_RABBITMQ_NODENAME` variables can pin an
existing node identity, but the hostname and the nodename suffix must agree.
For D2, record the exact existing pair and set both variables from that record;
never guess either value independently or start RabbitMQ blank.

All services use `restart: unless-stopped`, which recovers unexpected exits but
does not override a deliberate `docker compose down`. Compose sends `SIGTERM`
with exact grace periods of 45 seconds for the API, 12 minutes for every
worker, 30 seconds for Beat, and 60 seconds for the relay. API startup applies
migrations, bootstraps the admin, and then uses `exec` to hand PID 1 to Granian.
Workers reconnect indefinitely both during startup and after broker loss, so
operator intervention is not required for an ordinary broker restart.

This D1 contract applies only to fresh or disposable Compose projects. Reusing
recorded deployment storage and RabbitMQ identity is a D2 operation with a
reviewed, recorded bootstrap procedure. Do not cross that boundary by deleting
release volumes with `down -v`, inventing a node identity, or starting with
blank RabbitMQ state.

## Frontend

The `frontend` service builds `apps/frontend/Dockerfile` with the
`apps/frontend` directory as the build context (the Dockerfile copies
`package.json`, installs dependencies, runs `pnpm build`, and starts
`pnpm start` on port `3000`).

Server-side data fetching uses `NEXT_PUBLIC_ENGRAM_API_URL` to resolve
the backend. Inside Compose it is set to `http://api:8000` so the
container reaches the `api` service over the private Docker network;
both the home page and the `/health` page issue
`GET http://api:8000/-/healthz/`. Expose the console on the host via
port `3000`.
