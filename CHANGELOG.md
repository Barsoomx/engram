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

- Native Codex harness support: repository-backed marketplace packaging,
  lifecycle hooks, MCP workspace routing, Claude Code/Codex/Both installer UI,
  and a real containerized Codex E2E baseline.
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
- MCP bridge: newline-delimited JSON-RPC stdio server shipped inside
  `engram-connect` (`engram_cli/mcp_server.py`, `engram_cli/mcp_tools.py`)
  exposing `engram_search`, `engram_context`, `engram_memory_link`,
  `engram_observations`, `engram_memory_version`, and
  `engram_memory_feedback`; delivered automatically to Claude Code via the
  plugin (plugin-root `.mcp.json`) and registered elsewhere with
  `engram mcp install`/`engram mcp serve`; no local store, embeddings, or
  secrets, and no API key written into agent configs. The standalone
  `packages/mcp` package is retired.
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
  `apps/backend`, `apps/frontend`, `packages/cli`,
  `packages/claude-plugin`, `packages/codex-plugin`, `plugin-repository`, and
  `deploy/compose` (the former `packages/mcp` merged into `packages/cli`).
- Repository-URL project routing parity: `repository_url` is now accepted as
  an alternative to `project_id` on observations (list/detail) and memory
  (feedback, version, links, diff) endpoints, matching hooks/context/search.
  One precedence ladder (per-call argument/`--project` > `ENGRAM_PROJECT_ID` >
  `~/.engram/config.json` `project_id` > repository derived from the current
  workspace) now governs project selection identically across all six MCP
  tools, `engram search`/`observations`/`memory version|link|links`, and hook
  ingest. The MCP bridge derives the workspace from `CLAUDE_PROJECT_DIR` when
  set (falling back to `cwd`), so a Claude Code plugin-cache working directory
  can no longer mis-route memory to the wrong project.

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
- Cross-project isolation on repository-URL routing: hooks, search, and
  context previously resolved a `repository_url` to a project without
  verifying the requesting scope's project membership, so a project-scoped
  API key could read or write another in-org project's memory by sending its
  `repository_url`. All repository-URL paths (including the new observations
  and memory endpoints) now run through a shared resolver that enforces
  membership before any query executes, denying `project_scope_denied` on a
  mismatch; a project-bound key's binding wins even if it also carries the
  `projects:agent` capability. `git_remote_url` strips URL userinfo,
  including the password-only form (`https://:token@host/...`, previously
  missed because an empty username was treated as absent), so embedded
  credentials never reach a request payload or query string.

## engram-connect 0.6.0 / claude-plugin 0.1.13 / codex-plugin 0.3.0 - 2026-07-21

MCP/CLI parity campaign (PRs #277-#283): the agent tool surface catches up
with the backend memory loop. Nine MCP tools now ship.

### Added

- `engram_memory_propose`: agents deliberately record a durable fact; routed
  through the full curation pipeline (deterministic gates, shortlist,
  evidence-aware judge) via a new typed `agent_proposal` candidate provenance
  and `POST /v1/memories/propose` (`memories:propose`).
- `engram_memory_get`: read one memory in full by id - untruncated body,
  version history, links, optional version diff.
- `engram_audit`: memory-scoped audit trace (who refuted/revised/curated and
  when), newest-first via a new ordering-aware audit-events inspection path
  with `target_id`/`target_type` filters.
- `kinds` filter on `engram_search`/`engram_context` (+ CLI `--kind`) to fetch
  conventions/decisions/gotchas on a topic; search render now surfaces match
  reasons, matched terms, and retrieval warnings; context render appends a
  citations map (`[Mn]` -> memory id).
- `engram_observations` filters: `observation_type`, `session_id`, `since`,
  `until`, `offset`; renders `observed_at` and session.
- `confirmed` feedback action: confirming a memory resets its confidence-decay
  anchor (`last_confirmed_at`) without touching stale/refuted state.
- `conflict_excluded` retrieval warning plus a fail-closed
  `has_open_conflict`-aware `authorized_for_injection` in inspection.
- Per-call `request_id` and `team_id` on the six base MCP tools;
  `engram mcp-install` no longer requires a configured `project_id`
  (repository-url routing).
- Compose golden path now exercises all new tools end-to-end in both
  project-scoped and repo-url modes.

### Fixed

- Fail-open team-scope reads on memory `version`/`links`/`diff` GET endpoints
  (visibility whitelist: deny SESSION/ORGANIZATION/null-team/foreign-TEAM).
- Unproven-digest link leak on the links GET path (digest quarantine).
- Cross-team audit target-title disclosure (org/project/whitelist-scoped
  resolution) and cross-team digest-title quarantine.
- `provenance` and `scope` transition errors now classify as terminal
  `INVALID_INPUT` for candidate-decision work instead of retrying forever.
- Curation prompt no longer steers cross-visibility duplicates to
  `publish_new`; redundant matches route to targeted rejection.
- Exception text no longer reaches propose error responses
  (CodeQL stack-trace-exposure).
