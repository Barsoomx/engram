# claudex-teams

Enterprise server-side memory for Claude Code and Codex teams.

`claudex-teams` is a planned fork of `thedotmack/claude-mem` focused on teams,
companies, and on-premise deployments. The product goal is simple: every agent
session should capture useful observations, retrieve the right shared memory at
session start and during work, and update the company's knowledge base without
running local memory workers on developer machines.

The `upstream` branch keeps the original project snapshot. The `master` branch
is the product and architecture track for the server-only rewrite.

## Product Direction

- Server-only memory plane for Claude Code and Codex.
- No local SQLite, Chroma, or background worker requirement on developer hosts.
- Lifecycle hooks are the deterministic control plane for memory capture,
  retrieval, observation generation, and policy-assisted guidance.
- Sentry-like tenancy: organizations, teams, users, projects, API keys, and
  scoped service accounts.
- Team-owned model configuration and provider secrets.
- On-premise first, SaaS-ready later.
- Python backend stack with explicit domain services, domain events, durable
  outbox, tracing, metrics, and audit logs.
- Dense admin UI for memory review, RBAC, secrets, model settings, and audit.

## Documentation

- [Product requirements](docs/product-requirements.md)
- [V1 scope](docs/v1-scope.md)
- [Architecture](docs/architecture.md)
- [Backend contracts](docs/backend-contracts.md)
- [RBAC and scopes](docs/rbac-and-scopes.md)
- [Agent integrations](docs/agent-integrations.md)
- [Client installation and hook bootstrap](docs/client-installation.md)
- [MCP tools](docs/mcp-tools.md)
- [AI workflow loop](docs/ai-workflow-loop.md)
- [Admin UI requirements](docs/admin-ui-requirements.md)
- [Secrets and model configuration](docs/secrets-and-model-config.md)
- [Search and retrieval](docs/search-and-retrieval.md)
- [Operations and deployment](docs/operations-and-deployment.md)
- [Repository governance](docs/repository-governance.md)
- [Upstream fork boundary](docs/upstream-fork-boundary.md)
- [Research notes](docs/research-notes.md)

## Design Principles

The project should be powerful without becoming an enterprise policy maze.
Prefer a small number of composable concepts, explicit ownership, obvious
fallbacks, and inspectable behavior:

- one server-side source of truth;
- one hook protocol per agent family;
- one local MCP bridge that calls the server, not a local memory store;
- one small client connect wizard, not a local worker installer;
- one scheduled AI workflow loop for digest and memory curation;
- one scope model reused by users, teams, projects, memories, API keys, and
  secrets;
- hybrid search in V1: exact/grep-style retrieval plus semantic retrieval;
- every injected memory must have provenance and authorization evidence;
- every write path must be idempotent and auditable.

## Current Status

This branch is a docs-first architecture branch. The inherited implementation is
still present while the rewrite is planned, but it is not the target runtime. The
target runtime is a server-side Python application with PostgreSQL-backed state,
optional vector search, and no developer-machine memory worker.

## License

The fork is based on an Apache License 2.0 upstream project. Preserve required
notices and attribution while replacing product branding and implementation.
