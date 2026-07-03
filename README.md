# Engram

[![Backend](https://github.com/Barsoomx/engram/actions/workflows/backend.yml/badge.svg)](https://github.com/Barsoomx/engram/actions/workflows/backend.yml)
[![Frontend](https://github.com/Barsoomx/engram/actions/workflows/frontend.yml/badge.svg)](https://github.com/Barsoomx/engram/actions/workflows/frontend.yml)
[![CodeQL](https://github.com/Barsoomx/engram/actions/workflows/codeql.yml/badge.svg)](https://github.com/Barsoomx/engram/actions/workflows/codeql.yml)
[![PyPI version](https://img.shields.io/pypi/v/engram-connect.svg)](https://pypi.org/project/engram-connect/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

Shared engineering memory for AI coding agents. Self-hosted.

Engram gives Claude Code, Codex, and future agents a durable, project-scoped
memory of what they learned about your codebase — so they stop re-learning it
every session. Agents stream observations through lifecycle hooks; a background
loop distills them into curated memory; the next session opens with a
ready-to-inject context bundle. One memory layer, many interfaces: UI, CLI, API,
MCP, and hooks.

## Features

- **LLM-agnostic memory** shared across Claude Code, Codex, and future agents.
- **Context, not search** — the primary output is a ready-to-inject context
  bundle with provenance and authorization evidence, not a list of hits.
- **Deterministic capture** via lifecycle hooks (`SessionStart`, `PostToolUse`,
  `Error`, `Decision`); no local worker or vector store on developer hosts.
- **Hybrid retrieval** — exact/lexical recall plus semantic search over pgvector.
- **Scheduled AI workflow loop** distills sessions into memory and generates
  daily/weekly digests.
- **Multi-tenant RBAC** — organizations, teams, projects, agents, users, and
  scoped API keys, with automatic project routing by git repository URL.
- **Team-owned model config** and encrypted provider secrets, redacted from
  logs, traces, audit, and error responses.
- **Durable by construction** — transactional outbox, domain events, idempotent
  write paths, and an auditable trail for every injected memory.
- **Dense admin UI** for memory review, RBAC, secrets, model settings, and audit.
- **Native integrations** — Claude Code and Codex plugins, an MCP bridge, and a
  thin `engram` CLI client.

## Architecture

Memory-first. The memory layer is the product core; every other surface is an
interface onto it.

```
Claude Code / Codex ──hooks──▶ ┌───────────────────────────┐
        CLI ──────────────────▶│  Engram API (Django + DRF) │
        MCP bridge ───────────▶│  memory · retrieval ·      │
        Admin UI ─────────────▶│  context · RBAC · policy   │
                               └────────────┬──────────────┘
                                            │
              Celery workers + beat ◀───────┤  PostgreSQL + pgvector
              (distill · digest · relay)    │  Redis · RabbitMQ
```

The runtime is a server-side Python application with PostgreSQL-backed state,
hybrid retrieval, and context assembly. Agents run only a thin client — no
developer-machine memory worker.

## Quick Start

Get a working deployment with ingest and context retrieval verified in about
10–15 minutes.

```bash
git clone https://github.com/Barsoomx/engram.git
cd engram/deploy/compose
cp .env.example .env          # set ENGRAM_SECRET_KEY at minimum
docker compose up --build -d
```

Bootstrap a ready-to-use organization, project, and scoped API key:

```bash
export ENGRAM_GOLDEN_KEY='egk_local_quickstart_00112233445566778899'
docker compose exec api python manage.py engram_bootstrap_golden_path \
  --api-key "$ENGRAM_GOLDEN_KEY"
```

Install the thin CLI and connect an agent:

```bash
pip install engram-connect
engram connect --server http://localhost:8000 \
  --api-key "$ENGRAM_GOLDEN_KEY" --project <project_id>
engram doctor
```

The admin UI is at `http://localhost:3000`, the API at `http://localhost:8000`.

**[Full Quickstart →](docs/quickstart.md)**

## How It Works

1. **Capture** — agent lifecycle hooks stream observations to the API.
2. **Distill** — a background loop turns raw session activity into candidate
   memories, judged and promoted into curated memory.
3. **Retrieve** — hybrid lexical + semantic search, authorized before ranking.
4. **Inject** — the next session opens with a context bundle assembled from the
   most relevant, provenance-tagged memory.

See [AI workflow loop](docs/ai-workflow-loop.md) and
[Search and retrieval](docs/search-and-retrieval.md) for the internals.

## Documentation

- [Quickstart](docs/quickstart.md) — clone to verified deployment.
- [Architecture](docs/architecture.md) · [Backend contracts](docs/backend-contracts.md)
- [API reference](docs/api-reference.md) · [MCP tools](docs/mcp-tools.md)
- [RBAC and scopes](docs/rbac-and-scopes.md) · [Secrets and model config](docs/secrets-and-model-config.md)
- [Operations and deployment](docs/operations-and-deployment.md) · [Release runbook](docs/release-runbook.md)
- Guides: [CLI](docs/guides/cli.md) · [MCP](docs/guides/mcp.md) · [Plugins](docs/guides/plugins.md) · [Admin UI](docs/guides/admin-ui.md) · [API keys](docs/guides/api-keys.md) · [Auth](docs/guides/auth.md)

Product direction lives in the [North Star](docs/north-star.md); other documents
specialize that vision rather than redefine it.

## Design Principles

Powerful without becoming an enterprise policy maze — a small number of
composable concepts, explicit ownership, obvious fallbacks, and inspectable
behavior:

- one memory layer as the product core;
- one hook protocol per agent family;
- one local MCP bridge that calls the server, not a local memory store;
- one scope model reused by users, teams, projects, memories, API keys, and secrets;
- context bundle generation as the main retrieval output;
- every write path idempotent and auditable.

## Security

See [SECURITY.md](SECURITY.md) for the disclosure policy and the security review
cadence. Review artifacts are published under
[`docs/security/reviews/`](docs/security/reviews/).

## License

[Apache License 2.0](LICENSE). Engram is a fork of
[`thedotmack/claude-mem`](https://github.com/thedotmack/claude-mem); required
notices and attribution are preserved in [NOTICE](NOTICE).
