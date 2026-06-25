# Core Models Design

## Goal

Add the first authoritative PostgreSQL-backed data model for the `claude-mem`
parity loop. This slice creates Django models and migrations only. It does not
add API endpoints, API keys, authorization decisions, worker handlers,
retrieval ranking, provider calls, CLI behavior, or frontend views.

## Current Gate

The current roadmap item is "Add core models and migrations for the parity
loop." The previous checkpoint added the backend project, health endpoints,
Compose runtime, and backend CI. The next checkpoints need durable tables before
they can implement auth, hook ingest, worker processing, retrieval, and context
bundle APIs.

The hard parity gate requires PostgreSQL storage for:

- raw event envelopes;
- normalized observations;
- generated memory or memory candidates;
- retrieval documents;
- context-bundle audit records;
- durable outbox entries.

## Approaches Considered

### One Core App Now

Create one `engram.core` Django app containing the shared parity-loop models:
tenancy, projects, agents, sessions, events, observations, memory,
retrieval/context audit, audit events, and outbox entries.

Tradeoff: one app will later be split when domains grow. For the parity gate it
keeps migrations simple, avoids premature cross-app dependencies, and gives the
next slices one stable persistence boundary.

### Many Domain Apps Now

Create separate apps for identity, projects, observations, memory, context, and
outbox.

Tradeoff: domain boundaries look cleaner, but the first migrations become
dependency-heavy before any domain service exists. This increases ceremony for
no current behavior.

### Minimal Event Tables Only

Create only raw events, observations, and outbox, deferring memory/retrieval and
context bundle audit tables.

Tradeoff: smaller diff, but it does not satisfy the parity gate's storage
surface and would force immediate schema churn in the next worker/retrieval
slices.

## Decision

Use one `engram.core` app for this checkpoint. Keep models explicit and boring:
UUID primary keys, tenant/project foreign keys, scoped uniqueness constraints,
JSON fields for source payloads and extracted metadata, finite text choices for
states, and timestamps. Do not add generic polymorphic relations or hidden
frameworks.

The model names are stable enough for the first end-to-end loop:

- `Organization`
- `Team`
- `Project`
- `ProjectTeam`
- `Agent`
- `AgentSession`
- `RawEventEnvelope`
- `Observation`
- `MemoryCandidate`
- `Memory`
- `MemoryVersion`
- `RetrievalDocument`
- `ContextBundle`
- `ContextBundleItem`
- `AuditEvent`
- `OutboxEvent`

## Boundaries

This slice owns persistence shape and migration integrity. It must not decide
final RBAC policy, API-key behavior, hook serializers, worker retry code,
context packing, semantic retrieval, provider adapter behavior, or migration
import format.

Those later slices will use these models through domain services. If a later
service needs a field that is not required by the current parity contracts, it
should add a migration then.

## Data Model

### Tenant And Scope

`Organization` is the root tenant boundary. `Team` and `Project` belong to one
organization. `ProjectTeam` binds teams to projects for explicit collaboration.

Slugs are unique within their parent scope. Core records carry organization and,
where project-owned, project scope. This enables authorization filters to be
applied before retrieval or context packing.

### Agents And Sessions

`Agent` represents one runtime identity in an organization, such as Codex or
Claude Code. `AgentSession` binds an external session id to organization,
project, optional team, agent runtime, repository metadata, branch, cwd, status,
and prompt counter.

The first uniqueness rule is organization plus project plus external session
id. Runtime-specific ids remain metadata, not separate data ownership.

### Raw Events And Observations

`RawEventEnvelope` stores the authenticated hook/client event exactly as the
server accepted it after validation. It includes agent runtime, event type,
client event id, idempotency key, content hash, schema version, payload,
occurred time, received time, sequence number, and resolved scope.

`Observation` stores normalized evidence extracted from events or imports:
type, title, subtitle, facts, narrative, concepts, files read/modified,
generated model metadata, content hash, source metadata, and timestamps.

Duplicate client events are scoped by organization, project, session, and client
event id. Duplicate observations collapse by organization, project, session,
and content hash.

### Memory And Retrieval

`MemoryCandidate` records proposed memory from observations before promotion.
The first golden path may either auto-promote or include an explicit promotion
step; the schema supports both.

`Memory` is approved or durable memory. `MemoryVersion` records versioned
content and source provenance. `RetrievalDocument` is the searchable projection
for an approved memory version. It stores scope, source observation ids, file
paths, symbols, exact terms, full-text body, embedding reference, and freshness
flags.

This slice stores enough retrieval metadata for exact search and later vector
adapters. It does not implement ranking or embedding generation.

### Context And Audit

`ContextBundle` records each assembled/injected context artifact with request
id, session, purpose, query text, rendered text, token budget, authorization
scope evidence, selected count, and status.

`ContextBundleItem` records each selected memory/retrieval document, citation,
rank, inclusion reason, and scope evidence.

`AuditEvent` is the append-only operational evidence model for later auth,
memory reads/writes, context injection, and worker/provider actions.

### Outbox

`OutboxEvent` is the durable async bridge. It tracks organization/project/team,
aggregate type/id, event type, payload version, payload, idempotency key,
actor, correlation id, trace id, status, attempts, retry timing, lock owner,
lock time, last error, processed time, and timestamps.

The dispatcher and Celery task implementations are deferred. This slice must
still enforce idempotency keys and expose indexes needed by later claim/retry
logic.

## Testing

Tests must prove schema behavior, not only imports:

- core app is installed and migrations are present;
- scoped uniqueness for organization/team/project/session/event/observation;
- raw event duplicate replay cannot create a second row in the same scope;
- the same external ids are allowed in different organizations or projects;
- retrieval documents must point at the same organization/project scope as their
  memory version;
- context bundle items persist citations and scope evidence;
- outbox idempotency is unique per event type and idempotency key.

Migration checks must run through Django's migration tooling. API/RBAC/worker
tests are explicitly out of scope for this slice.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run`
- `cd apps/backend && poetry run python manage.py migrate --check`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`

Docker Compose smoke remains blocked until Docker is available in this WSL
distro.

## Self-Review

- No placeholder behavior: all listed models are in scope for this checkpoint.
- No North Star expansion: frontend, MCP, provider routing, and custom admin UI
  remain deferred.
- No runtime-specific ownership: agent runtime is metadata on events/sessions,
  not a memory schema boundary.
- No local worker regression: outbox and event storage are server-side only.
- No broad abstraction: one app and explicit models are enough for the next
  parity slices.
