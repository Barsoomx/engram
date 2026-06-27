# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Development cycle 2026-06-25 through 2026-06-26. This cycle rewrites the useful
`claude-mem` product loop into a clean Python/server architecture and advances
Engram toward release readiness across retrieval, memory quality, provider
integration, client surfaces, and operations.

### Added

- Semantic retrieval foundation: deterministic character 3-gram embeddings
  provider adapter, `embedding_vector` on `RetrievalDocument` (migration
  `0004`), and cosine semantic fallback inside context packing that fires only
  when exact matching returns fewer items than the requested limit.
- Memory search API: `POST /v1/search` returning authorized, cited, ranked
  memory matches without persisting a context bundle; hybrid exact+semantic
  recall identical to the context path.
- Memory versioning: `POST /v1/memories/<id>/version` appends a `MemoryVersion`,
  bumps `current_version`, re-indexes the retrieval document, and audits
  `MemoryVersionCreated`; replay-protected by content-based idempotency.
- Memory links: `MemoryLink` model (migration `0005`) plus
  `POST/GET /v1/memories/<id>/links` for authorized, replay-protected
  code/symbol/commit/issue links attached to an approved memory.
- Memory candidate deduplication: content-hash dedupe prevents duplicate
  memory candidates from repeated observation delivery.
- Observation list API: `GET /v1/observations` read-only authorized listing of
  observations scoped to the resolved tenant/project/team.
- Memory digest generation: `GenerateDigest` collects approved source memories,
  resolves a `digest` model policy, generates a digest via the provider gateway,
  stores it as a `metadata.kind='digest'` memory plus version and retrieval
  document, and audits `DigestGenerated` (daily AI workflow foundation).
- Real provider adapter: `OpenAICompatibleGateway` for HTTP chat completions and
  embeddings via stdlib `urllib`, plus `get_provider_gateway()` factory; any
  OpenAI-compatible endpoint works via `ModelPolicy.metadata['base_url']`
  (OpenAI, GLM/ZhipuAI, OpenRouter, local). `ENGRAM_PROVIDER_MODE=real` opts in.
- Anthropic Messages gateway: Anthropic-compatible adapter for GLM-compatible
  providers routing through the Messages API schema.
- MCP bridge: newline-delimited JSON-RPC stdio server in `packages/mcp`
  exposing `engram_search`, `engram_context`, `engram_memory_link`, plus
  observations and memory-version tools; no local store, embeddings, or secrets.
- CLI commands: `engram search` (calls `POST /v1/search`),
  `engram memory version <id>`, `engram memory link <id>`,
  `engram memory links <id>`, and `engram observations` over existing
  authorized endpoints.
- Frontend admin UI: Next.js app with health page and memories inspection page
  served from `apps/frontend`.
- Memory export/backup: `engram_export_memories` management command for
  tenant-scoped memory export and backup.
- Compose frontend service: `frontend` service in
  `deploy/compose/docker-compose.yml` building and running the Next.js admin
  UI alongside backend, worker, relay, PostgreSQL, RabbitMQ, and Redis.
- Prometheus metrics: request metrics middleware and `/metrics` endpoint under
  `engram.core.observability`.
- Memory feedback loop: authorized `memories:review` callers can mark memory
  stale or refuted; retrieval projection flags updated; future context
  retrieval excludes corrected memory.
- Admin inspection API: `/v1/inspection/*` read-only memory, context-bundle,
  and audit-event inspection behind `memories:admin` and `audit:read`.
- Model policy secrets foundation: organization/team provider secret references,
  encrypted database envelopes, project/team/organization model policy
  resolution, fake provider adapter selection, and provider call audit records.
- Claude Code and Codex client packages: native plugin manifests and hook
  contracts for Claude Code and Codex thin clients.
- Upstream migration import: idempotent `engram_import_claude_mem` management
  command with dry-run report, stable external ids, redaction, and sanitized
  fixture-backed regression tests.
- Core models and migrations for the parity loop: organization/project/team,
  raw event envelope, observation, memory, memory candidate, retrieval
  document, session, context bundle, context bundle audit, audit event.
- Auth scope and API keys: hash-only API-key storage, capability narrowing,
  project/team/org denial, and resolved scope filters.
- Hook dry-run and observation ingest: session-start, post-tool-use, error, and
  decision hook events with payload size limits, redaction, replay idempotency,
  and tenant-scoped persistence.
- Memory candidate worker: server-side candidate creation, redaction,
  downstream event emission, duplicate delivery handling, and failure marking.
- CLI lifecycle: `engram connect`, `engram doctor`, `engram disconnect`,
  `engram hooks install` with redacted credentials and derived fingerprints.
- Compose runtime: Docker Compose with app, worker, relay, PostgreSQL,
  RabbitMQ broker, Redis result backend, and Celery SLA queue topology
  (`engram-realtime`, `engram-near-realtime`, `engram-batch`,
  `engram-highmemory`, `engram-domain-events`).
- E2E golden path: `scripts/e2e_golden_path.py` proving capture, worker-created
  memory, retrieval, and future-session context injection through Compose.
- Backend health endpoints: `/-/healthz/`, `/-/readyz/`, `/-/startup/`.
- Repository quality CI, layout contract, and monorepo skeleton across
  `apps/backend`, `apps/frontend`, `packages/cli`, `packages/mcp`,
  `packages/claude-plugin`, `packages/codex-plugin`, `plugin-repository`, and
  `deploy/compose`.

### Changed

- Worker auto-promotes observation-created memory candidates to approved memory
  and indexes retrieval documents in one transactional pipeline.
- Hook ingest now enqueues `engram.memory.process_observation_recorded` through
  the `django-celery-outbox` package transport `.delay(str(observation.id))`
  boundary instead of a custom outbox.
- Memory worker now uses model-policy-resolved provider generation before
  creating memory candidates; production call sites route through the provider
  gateway factory.
- Retrieval audit records the real `retrieval_strategy` plus
  `semantic_provider_call_id` and `semantic_document_ids` when the semantic
  fallback activates.
- Memory feedback and versioning services share extracted
  `lock_memory_for_update` and `ensure_memory_team_scope` helpers.

### Fixed

- Stale Compose state can no longer satisfy the E2E acceptance gate: the golden
  path clears volumes before startup and binds retrieval to the current run's
  hook raw event and source observation.
- Hook and context request content now enforce per-event and per-field size
  caps before persistence and retrieval processing.
- Provider secret envelopes fail closed when the production encryption key is
  missing or invalid; disabled secrets are excluded from policy resolution.
- Team-bound project policies no longer leak to other teams during model policy
  resolution.
- Replay of the same memory body reuses the latest version; concurrent updates
  serialize through `select_for_update` plus the unique-version constraint.
- Raw token-shaped values are redacted from candidate title, body, evidence,
  retrieval document paths, context `rendered_text`, and audit metadata.
- Cross-team and cross-project denial enforced across ingest, memory lookup,
  context bundle generation, inspection API, worker processing, and replay.
