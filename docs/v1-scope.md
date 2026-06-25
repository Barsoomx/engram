# V1 Scope

## Goal

V1 must prove the server-only memory loop without building a large enterprise
platform upfront. It should be small enough to implement, test, and operate
end-to-end.

## Included

- Django + Django REST Framework API.
- PostgreSQL as the source of truth.
- Redis-compatible broker and Celery workers.
- Organization, team, project, user, and API key model.
- Four roles: Owner, Admin, Developer, Auditor.
- Team and project memory scopes.
- Hook ingestion for Claude Code and Codex through one shared server protocol.
- Client connect golden path:

  ```bash
  npx claudex-teams connect --server URL --api-key KEY --project PROJECT
  ```

- Hook dry-run endpoint that prints resolved organization, team, project, actor,
  scopes, and server health.
- Observation ingestion.
- Memory candidate generation.
- AI workflow loop for daily team digest and automated memory curation.
- Approved memory retrieval.
- PostgreSQL full-text search plus `pg_trgm`.
- Audit evidence for hook calls, memory reads/writes, API keys, and secrets.
- Organization/team provider secrets through vault adapter or encrypted database
  envelopes.
- Docker Compose deployment for development and first on-premise trials.
- Dense admin console for the included operational flows.

## Later

- Custom roles.
- Service accounts beyond API-key-owned agent identity.
- User-private, organization-wide, memory-pack, and policy-pack scopes.
- Legal hold and eDiscovery workflows.
- SaaS billing and chargeback.
- Qdrant.
- Embedding rerank as a required path.
- Policy enforcement blocks. V1 may warn and audit, but blocking is a later
  hardening step.
- Managed hook distribution.
- Device/browser auth flow.
- Helm production deployment.
- Multi-region operation.

## Golden Path

1. Operator starts server with Compose.
2. Admin creates organization, team, project, user, and project-scoped API key.
3. Developer runs `npx claudex-teams connect --server URL --api-key KEY --project PROJECT`.
4. Installer writes thin hooks and calls dry-run.
5. Agent session starts; server injects scoped memory.
6. Hooks record observations.
7. Scheduled AI workflow loop summarizes the day and curates memory candidates.
8. Future sessions retrieve approved memory with citations and audit evidence.
