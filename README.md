# Engram

Engineering memory between codebases and AI development agents.

Engram is a planned fork of `thedotmack/claude-mem` focused on shared
engineering memory for AI-assisted development. V1 is a high-quality evolution
of `claude-mem` with a narrower goal: reduce the time AI agents spend
re-learning a project.

The `upstream` branch keeps the original project snapshot. The `master` branch
is the product and architecture track for the server-only rewrite.

## North Star

The single source of truth for product vision is
[North Star](docs/north-star.md). Other documents should reference and
specialize that vision, not redefine it.

Core product invariants:

- LLM-agnostic memory shared by Claude Code, Codex, and future agents.
- Memory-first architecture: UI, CLI, API, MCP, and hooks are interfaces to the
  memory layer.
- Context, not search: the primary output is a ready-to-inject context bundle.
- Local-first ownership with self-hosted deployment as the primary scenario.
- Agent-native APIs and tools alongside the web UI.

## V1 Direction

- No local SQLite, Chroma, or background worker requirement on developer hosts.
- Lifecycle hooks are the deterministic control plane for memory capture,
  retrieval, observation generation, and policy-assisted guidance.
- Multi-project backend, frontend, API, CLI, memory storage, search, and context
  bundle generation.
- Sentry-like tenancy for organizations, projects, teams, agents, users, API
  keys, and scoped service accounts.
- Team-owned model configuration and provider secrets.
- On-premise first, SaaS-ready later.
- Python backend stack with explicit domain services, domain events, durable
  outbox, tracing, metrics, and audit logs.
- Dense admin UI for memory review, RBAC, secrets, model settings, and audit.

## Documentation

- [North Star](docs/north-star.md)
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

- one memory layer as the product core;
- one hook protocol per agent family;
- one local MCP bridge that calls the server, not a local memory store;
- one small client connect wizard, not a local worker installer;
- one scheduled AI workflow loop for digest and memory curation;
- one scope model reused by users, teams, projects, memories, API keys, and
  secrets;
- hybrid retrieval in V1: exact/grep-style retrieval plus semantic retrieval;
- context bundle generation as the main retrieval output;
- every injected memory must have provenance and authorization evidence;
- every write path must be idempotent and auditable.

## Current Status

This branch is a docs-first architecture branch. The inherited implementation
has been removed from `master`; the source snapshot is preserved on the
`upstream` branch for reference. The target runtime is a server-side Python
application with PostgreSQL-backed state, context assembly, hybrid retrieval,
and no
developer-machine memory worker.

## License

The fork is based on an Apache License 2.0 upstream project. Preserve required
notices and attribution while replacing product branding and implementation.
