# Compose Deployment

Local self-hosted profile for the parity backend runtime: API,
queue-specific Celery workers, relay, PostgreSQL, RabbitMQ broker,
Redis result/cache backend, and the Next.js admin frontend.

## Services

- `api`: Django + Gunicorn on port `8000`. Healthcheck probes
  `http://localhost:8000/-/readyz/`.
- `frontend`: Next.js admin console (`apps/frontend`) on port `3000`.
  Builds from the `apps/frontend` directory (see `apps/frontend/Dockerfile`,
  `pnpm start`). Server-side fetches reach the backend over the internal
  Compose network at `http://api:8000` via `NEXT_PUBLIC_ENGRAM_API_URL`.
  The home page and `/health` page call `${NEXT_PUBLIC_ENGRAM_API_URL}/-/healthz/`.
- `relay`: outbox relay shipping domain events to the broker.
- `worker-realtime`: `engram-realtime`
- `worker-near-realtime`: `engram-near-realtime`
- `worker-batch`: `engram-batch`
- `worker-highmemory`: `engram-highmemory`
- `worker-domain-events`: `engram-domain-events`
- `postgres`: PostgreSQL 16 (database/user/password `engram`).
- `redis`: Redis 7 result/cache backend.
- `rabbitmq`: RabbitMQ 3.13 broker (user/pass `engram`, vhost `engram`).

Workers are split by SLA queue:

- `worker-realtime`: `engram-realtime`
- `worker-near-realtime`: `engram-near-realtime`
- `worker-batch`: `engram-batch`
- `worker-highmemory`: `engram-highmemory`
- `worker-domain-events`: `engram-domain-events`

The Compose E2E workflow runs `scripts/e2e_golden_path.py` to prove the
first CLI/hook-to-context loop.

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
