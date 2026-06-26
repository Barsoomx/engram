# Compose Deployment

Local self-hosted profile for the parity backend runtime: API,
queue-specific Celery workers, relay, PostgreSQL, RabbitMQ broker, and
Redis result/cache backend.

Workers are split by SLA queue:

- `worker-realtime`: `engram-realtime`
- `worker-near-realtime`: `engram-near-realtime`
- `worker-batch`: `engram-batch`
- `worker-highmemory`: `engram-highmemory`
- `worker-domain-events`: `engram-domain-events`

The Compose E2E workflow runs `scripts/e2e_golden_path.py` to prove the
first CLI/hook-to-context loop.
