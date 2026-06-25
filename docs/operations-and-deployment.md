# Operations And Deployment

## Deployment Profiles

Development:

- Docker Compose with app, PostgreSQL, Redis-compatible broker, and worker.
- Local provider secret mode may use encrypted database storage.

V1 production trial:

- Docker Compose on a controlled company host.
- External PostgreSQL is allowed if the operator owns backups.
- Helm is not required for V1.

Standard on-premise:

- Kubernetes Helm chart.
- PostgreSQL supplied by the customer or an operator.
- Redis-compatible broker supplied by the customer or chart dependency.
- External vault optional.
- Ingress, TLS, metrics, traces, and logs configured through values.

Enterprise/SaaS:

- Horizontally scaled API pods.
- Separate worker pools for observation ingestion, memory distillation,
  embeddings, retention, and export.
- Optional Qdrant cluster.
- Tenant-aware rate limits, quotas, and billing events.

## Runtime Components

- API service: synchronous hook/admin/API traffic.
- Worker service: background jobs.
- Scheduler: retention, stale memory checks, model health checks.
- Admin frontend: operational console.
- Migration job: database migrations and data migrations.

No component should require a developer-machine local memory worker.

## Observability

Required telemetry:

- request id and trace id across hooks, API, outbox, workers, and provider calls;
- span id, actor id, team/project ids, hook event id, idempotency key, outbox
  event id, worker job id, and provider call id where present;
- structured logs with tenant-safe redaction;
- metrics for hook latency, retrieval latency, queue depth, distillation cost,
  provider errors, secret failures, and audit write failures;
- traces for retrieval pipelines and model calls;
- health checks for database, queue, secret store, provider policy, and index lag.

## Failure Modes

Server unavailable:

- hooks return no memory or use bounded metadata-only retry mode according to
  admin policy;
- agents should keep working without local worker startup.

Secret store unavailable:

- provider-backed generation stops for affected tenants;
- retrieval of already-approved memory can continue if it does not require a
  provider call;
- incident is visible in admin health and audit.

Queue lag:

- observation ingestion remains synchronous and durable;
- distillation/index freshness indicators show lag;
- retrieval can use existing approved memory.

Bad memory:

- users can flag injected memory;
- curators can archive, supersede, or mark conflict;
- future retrieval excludes archived versions.

## Data Governance

- Tenant-scoped export.
- Retention policies per organization and memory type.
- Legal hold for audit and selected memory.
- Redaction workflow for sensitive observations.
- Immutable audit events with tamper-evident hashes as a later hardening step.
