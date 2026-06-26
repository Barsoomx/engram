# V1 Scope

## Goal

V1 must prove the [North Star](north-star.md) in the smallest useful form:
reduce repeated project re-learning by AI agents. It should be a high-quality
evolution of `claude-mem`, not a broad enterprise platform upfront.

## Included

- Django + Django REST Framework API.
- Frontend for daily operational workflows.
- CLI for client connection, diagnostics, and agent-native workflows.
- PostgreSQL as the source of truth.
- RabbitMQ-compatible broker, Redis result/cache backend, and Celery workers.
- Organization, project, team, agent, memory, session, and context model.
- User and API key model for human and agent access.
- Four roles: Owner, Admin, Developer, Auditor.
- Team and project memory scopes.
- Hook ingestion for Claude Code and Codex through one shared server protocol.
- Client connect golden path:

  ```bash
  engram connect --server URL --api-key KEY --project PROJECT
  ```

- Hook dry-run endpoint that prints resolved organization, team, project, actor,
  scopes, and server health.
- Local MCP bridge for developer and lead workflows.
- Observation ingestion.
- Memory candidate generation.
- AI workflow loop for daily team digest and automated memory curation.
- Approved memory retrieval.
- Context bundle generation for session start and task-focused requests.
- Hybrid search: exact/grep-style retrieval plus semantic vector retrieval.
- Organization/team model policy with Anthropic and OpenAI provider support for
  memory generation, curation, embeddings, and optional summarization tasks.
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
- Additional agent families such as Gemini CLI, Cursor, and OpenAI Agents.
- Qdrant scale-out adapter.
- Model rerank as a required path.
- Policy enforcement blocks. V1 may warn and audit, but blocking is a later
  hardening step.
- Managed hook distribution.
- Device/browser auth flow.
- Helm production deployment.
- Multi-region operation.

## Golden Path

1. Operator starts server with Compose.
2. Admin creates organization, team, project, user, and project-scoped API key.
3. Developer runs `engram connect --server URL --api-key KEY --project PROJECT`.
4. Installer writes thin hooks, configures the local MCP bridge, and calls
   dry-run.
5. Agent session starts; server injects a scoped context bundle.
6. Hooks record observations.
7. Scheduled AI workflow loop summarizes the day and curates memory candidates.
8. Future sessions receive approved memory as context bundles with citations and
   audit evidence.
9. Team lead uses MCP tools or admin UI to inspect digests, contradictions, and
   escalations.
