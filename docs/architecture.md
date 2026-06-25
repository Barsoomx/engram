# Architecture

## Target Shape

Engram is the engineering memory layer described in [North Star](north-star.md).
The backend is a server-side Python application. Agent hooks and local tools
are thin clients. The server owns identity,
authorization, observation ingestion, memory generation, context assembly, model
routing, secret access, audit, and background work.

V1 stack:

- Django and Django REST Framework.
- PostgreSQL as the system of record.
- Redis-compatible broker for background jobs.
- Celery worker pools for ingestion follow-up, digest generation, memory
  curation, indexing, and retention tasks.
- Transaction-safe Celery enqueueing through `django-celery-outbox`.
- OpenTelemetry traces, structured logs, metrics, and error reporting.
- Admin frontend as a dense operational console.

The inherited worker implementation is source material, not the target runtime.
All local-worker responsibilities move behind authenticated server APIs.

## Domains

- Identity: organizations, teams, users, memberships, invitations, sessions.
- Access: roles, scopes, grants, API keys, service accounts.
- Projects: repositories, paths, branches, environments, integration metadata.
- Agents: agent identities, runtime families, hook registrations, event schemas.
- Observations: raw hook events, normalized observations, source references.
- Memory: candidate memories, approved memories, versions, conflicts, expiry.
- Context: context requests, exact retrieval, semantic retrieval, ranking,
  context packing, citations.
- AI workflows: scheduled team digests and autonomous memory curation.
- Secrets: provider keys, signing keys, webhook secrets, encryption metadata.
- Model policy: provider/model selection per organization, team, project, task.
- Audit: immutable append-only activity stream.
- Operations: deployment profile, health, migrations, queue state.

## Request Flow

1. Agent hook calls the server with an API key or signed token.
2. Server resolves tenant, user or service account, team, project, repository,
   branch, and requested operation.
3. Authorization builds an effective scope from organization, team, user, and API
   key grants.
4. The domain service validates the event and stores a normalized observation or
   retrieval request.
5. Follow-up work is queued through `django-celery-outbox` in the same database
   transaction.
6. Background workers distill observations, update indexes, generate team
   digests, curate memory candidates, and mark conflicts or stale memories.
7. Context APIs filter candidates by authorization before ranking and context
   packing.
8. Hook response returns compact guidance, citations, and debug metadata.

## Async Work

Memory generation is asynchronous and must be replayable. Current V1 models the
live transport as id-only Celery tasks persisted by `django-celery-outbox`.

Current task signals:

- `engram.memory.process_observation_recorded`

The queued payload carries the observation id. Workers reload tenant, project,
team, actor, source hook, correlation, and trace context from authoritative
domain rows. Future domain-event expansion requires a separate decision record.

## Persistence

PostgreSQL stores all authoritative state:

- tenancy, teams, memberships, roles, API keys;
- provider secret envelopes and external vault references;
- raw hook event envelopes;
- normalized observations;
- memory versions and source references;
- search documents and exact indexes;
- audit log and `django-celery-outbox` transport rows.

Vector storage starts as a replaceable adapter. `pgvector` is the simplest
default for on-premise deployments. Qdrant is a later adapter when customers
need independent vector scaling or operational separation.

The authoritative data model is LLM-agnostic. Models and agent runtimes are
metadata on events, memories, and context bundles, not owners of the memory
schema.

## API Surface

Public server APIs:

- `/v1/hooks/claude-code/*`
- `/v1/hooks/codex/*`
- `/v1/observations`
- `/v1/memories`
- `/v1/search`
- `/v1/context`
- `/v1/context/session-start`
- `/v1/hooks/dry-run`
- `/v1/projects`
- `/v1/api-keys`
- `/v1/model-policies`
- `/v1/audit`

Admin APIs use the same domain services and authorization checks as hook APIs.
There should be no hidden local-only API path.

## Simplicity Constraints

- Prefer one explicit scope model over separate permission systems per domain.
- API keys can narrow privileges, never expand them beyond the owning principal.
- Memory state transitions are finite and visible.
- Context assembly must be explainable: why this memory, why this scope, why
  this source, why this model.
- Background work is idempotent and retryable.
- Every asynchronous side effect goes through the approved transaction-safe
  Celery transport.

See [Backend contracts](backend-contracts.md) for the domain service, async
transport, RBAC, observability, and vault contracts required by implementation.
