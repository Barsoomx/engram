# Architecture

## Target Shape

`claudex-teams` is a server-side Python application. Agent hooks are thin
clients. The server owns identity, authorization, observation ingestion, memory
generation, search, model routing, secret access, audit, and background work.

V1 stack:

- Django and Django REST Framework.
- PostgreSQL as the system of record.
- Redis-compatible broker for background jobs.
- Celery worker pools for ingestion follow-up, digest generation, memory
  curation, indexing, and retention tasks.
- Durable outbox for domain events and integration fan-out.
- OpenTelemetry traces, structured logs, metrics, and error reporting.
- Admin frontend as a dense operational console.

The inherited worker implementation is source material, not the target runtime.
All local-worker responsibilities move behind authenticated server APIs.

## Domains

- Identity: organizations, teams, users, memberships, invitations, sessions.
- Access: roles, scopes, grants, API keys, service accounts.
- Projects: repositories, paths, branches, environments, integration metadata.
- Agent integrations: hook registrations, agent identities, event schemas.
- Observations: raw hook events, normalized observations, source references.
- Memory: candidate memories, approved memories, versions, conflicts, expiry.
- Retrieval: exact search, semantic search, ranking, context packing.
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
5. Domain events are written to the outbox in the same database transaction.
6. Background workers distill observations, update indexes, generate team
   digests, curate memory candidates, and mark conflicts or stale memories.
7. Retrieval APIs filter candidates by authorization before ranking and context
   packing.
8. Hook response returns compact guidance, citations, and debug metadata.

## Domain Events

Events are first-class because memory generation is asynchronous and must be
replayable.

Core events:

- `HookEventReceived`
- `ObservationRecorded`
- `ObservationClassified`
- `MemoryCandidateCreated`
- `MemoryApproved`
- `MemorySuperseded`
- `MemoryConflictDetected`
- `MemoryRefuted`
- `TeamDigestGenerated`
- `MemoryCuratorActionRecorded`
- `MemoryRetrieved`
- `SecretUsed`
- `ModelPolicyResolved`
- `ApiKeyRotated`
- `ScopeGrantChanged`

Each event carries tenant id, actor id, correlation id, trace id, source hook,
and idempotency key.

## Persistence

PostgreSQL stores all authoritative state:

- tenancy, teams, memberships, roles, API keys;
- provider secret envelopes and external vault references;
- raw hook event envelopes;
- normalized observations;
- memory versions and source references;
- search documents and exact indexes;
- audit log and outbox.

Vector storage starts as a replaceable adapter. `pgvector` is the simplest
default for on-premise deployments. Qdrant is a later adapter when customers
need independent vector scaling or operational separation.

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
- Retrieval must be explainable: why this memory, why this scope, why this model.
- Background work is idempotent and retryable.
- Every cross-domain side effect goes through the outbox.

See [Backend contracts](backend-contracts.md) for the domain service, durable
outbox, RBAC, observability, and vault contracts required by implementation.
