# `claude-mem` Parity Map

This map is the required hard gate before Engram backend implementation starts.
It records the useful upstream `claude-mem` behavior that Engram must preserve,
replace, drop, or defer while rewriting the product into a server-side Python
architecture.

Audited upstream commit:
`3fe0725a97e18b5edf3e61cde60e181ab2b6c997` (`upstream`, tag `v13.8.1`).

Engram audit branch:
`docs/parity-01-upstream-audit`.

Live Engram base at audit time:
`5be75e333fd982f1ecb82a0277c7e273559a28e6`.

## Classification Key

- `preserve`: keep the observable product behavior or contract.
- `replace`: keep the semantic intent, but implement it through Engram's
  Django/PostgreSQL/Celery/server-side architecture.
- `drop`: intentionally exclude from Engram V1/parity.
- `defer`: record for a later gate after the first parity loop.

## Audited Files And Commands

Primary upstream files inspected:

- `package.json`
- `plugin/hooks/hooks.json`
- `plugin/hooks/codex-hooks.json`
- `plugin/.codex-plugin/plugin.json`
- `.codex-plugin/plugin.json`
- `.claude-plugin/plugin.json`
- `src/cli/hook-command.ts`
- `src/cli/types.ts`
- `src/cli/adapters/claude-code.ts`
- `src/cli/adapters/codex.ts`
- `src/cli/adapters/cursor.ts`
- `src/cli/adapters/gemini-cli.ts`
- `src/cli/handlers/context.ts`
- `src/cli/handlers/session-init.ts`
- `src/cli/handlers/observation.ts`
- `src/cli/handlers/file-context.ts`
- `src/cli/handlers/summarize.ts`
- `src/cli/handlers/user-message.ts`
- `src/hooks/hook-response.ts`
- `src/shared/paths.ts`
- `src/shared/SettingsDefaultsManager.ts`
- `src/shared/EnvManager.ts`
- `src/shared/hook-io.ts`
- `src/shared/hook-settings.ts`
- `src/shared/worker-utils.ts`
- `src/shared/hook-constants.ts`
- `src/npx-cli/index.ts`
- `src/npx-cli/commands/install.ts`
- `src/npx-cli/commands/doctor.ts`
- `src/npx-cli/commands/uninstall.ts`
- `src/npx-cli/commands/runtime.ts`
- `src/npx-cli/install/error-reporter.ts`
- `src/npx-cli/utils/paths.ts`
- `src/sdk/prompts.ts`
- `src/sdk/parser.ts`
- `src/sdk/output-classifier.ts`
- `src/services/context/ContextBuilder.ts`
- `src/services/context/ObservationCompiler.ts`
- `src/services/context/formatters/AgentFormatter.ts`
- `src/services/sqlite/schema.sql`
- `src/services/sqlite/SessionStore.ts`
- `src/services/sqlite/SessionSearch.ts`
- `src/services/sqlite/observations/store.ts`
- `src/services/worker/SessionManager.ts`
- `src/services/worker/SessionMessageBuffer.ts`
- `src/services/worker/SearchManager.ts`
- `src/services/worker/search/SearchOrchestrator.ts`
- `src/services/worker/search/HybridSearchStrategy.ts`
- `src/services/worker/search/strategies/ChromaSearchStrategy.ts`
- `src/services/worker/search/types.ts`
- `src/services/worker/FormattingService.ts`
- `src/services/worker/http/routes/SessionRoutes.ts`
- `src/services/worker/http/routes/SearchRoutes.ts`
- `src/services/worker/http/routes/MemoryRoutes.ts`
- `src/services/transcripts/processor.ts`
- `src/services/hooks/server-beta-client.ts`
- `src/services/hooks/server-beta-bootstrap.ts`
- `src/server/routes/v1/ServerV1Routes.ts`
- `src/server/routes/v1/ServerV1PostgresRoutes.ts`
- `src/server/generation/providers/shared/prompt-builder.ts`
- `src/server/generation/processGeneratedResponse.ts`
- `src/server/jobs/ServerJobQueue.ts`
- `src/storage/postgres/schema.ts`
- `src/servers/mcp-server.ts`
- `cursor-hooks/CONTEXT-INJECTION.md`
- `cursor-hooks/INTEGRATION.md`
- `cursor-hooks/PARITY.md`
- `cursor-hooks/README.md`

Primary commands and scripts inspected:

- `npx claude-mem install`
- `npx claude-mem uninstall`
- `npx claude-mem doctor`
- `npx claude-mem start|stop|restart|status`
- `npx claude-mem server ...`
- `npx claude-mem worker ...`
- `npx claude-mem search <query>`
- `npx claude-mem transcript watch`
- `npm run worker:start|worker:stop|worker:restart|worker:status`
- `npm run cursor:install|cursor:uninstall|cursor:status|cursor:setup`
- `npm run test`
- `npm run test:sqlite`
- `npm run test:search`
- `npm run test:context`
- `npm run e2e:server-beta:docker`

## Executive Decision

The useful upstream product loop is preserved:

1. an agent session starts;
2. prior memory/context is injected;
3. prompt/tool/session hooks capture evidence;
4. observations are normalized;
5. useful memory is generated or updated;
6. retrieval finds relevant memory;
7. the next session receives compact cited context.

The upstream local runtime architecture is replaced:

- no local authoritative SQLite runtime store;
- no local Chroma/vector authority;
- no persistent local summarization worker;
- no local worker daemon as the control plane;
- no local provider secrets;
- no runtime-visible `claude-mem` product naming outside migration/parity docs.

Engram implements the loop with Django/DRF, PostgreSQL, Celery, durable outbox,
server-side model adapters, and thin CLI/hook/plugin clients.

## Hook Entrypoints

### Claude Code

Source:

- `plugin/hooks/hooks.json`
- `src/cli/adapters/claude-code.ts`
- `src/cli/handlers/context.ts`
- `src/cli/handlers/session-init.ts`
- `src/cli/handlers/observation.ts`
- `src/cli/handlers/file-context.ts`
- `src/cli/handlers/summarize.ts`

Upstream behavior:

- `Setup` runs a version check.
- `SessionStart` for `startup|clear|compact` starts the worker and requests
  context.
- `UserPromptSubmit` initializes the session and records prompt history.
- `PostToolUse` captures tool observations.
- `PreToolUse` for `Read` injects file-specific context.
- `Stop` queues session summary generation.
- Raw input accepts `session_id`, `id`, or `sessionId`, plus `cwd`, `prompt`,
  `tool_name`, `tool_input`, `tool_response`, `transcript_path`, `agent_id`,
  and `agent_type`.
- Output uses `hookSpecificOutput.additionalContext` and optional
  `systemMessage`.

Classification:

- `preserve`: Claude Code event semantics needed for the parity loop.
- `preserve`: normalized payload fields and response envelope.
- `preserve`: `additionalContext` injection at session start.
- `replace`: local worker start/version-check commands.
- `preserve`: native Claude Code package and response formatting for the same
  first package event contract as Codex: `SessionStart`, `PostToolUse`,
  `Error`, and `Decision`.
- `defer`: `PreToolUse` file timeline injection unless the first golden fixture
  requires it.
- `defer`: `Stop` summary generation as a runtime hook until the session-summary
  worker contract is implemented.

Engram target:

- Hook commands execute a small Engram adapter.
- Adapter posts authenticated events to `/v1/hooks/*` and `/v1/context/*`.
- Session start returns an authorized context bundle in the target agent's
  supported response shape.
- Missing server, bad credential, bad project, malformed payload, provider
  failure, and unsupported agent version all produce actionable contract-tested
  errors.
- 2026-06-25 checkpoint: `packages/claude-plugin` plus the thin
  `engram_cli hook` commands implement the Claude Code-native package and
  response contract for `SessionStart`, `PostToolUse`, `Error`, and `Decision`.

### Codex

Source:

- `plugin/hooks/codex-hooks.json`
- `plugin/.codex-plugin/plugin.json`
- `.codex-plugin/plugin.json`
- `src/cli/adapters/codex.ts`

Upstream behavior:

- `SessionStart` for `startup|resume` loads context.
- `UserPromptSubmit` initializes prompt/session state.
- `PreToolUse` targets Bash and read-like MCP tools.
- `PostToolUse` captures all tool use.
- `Stop` summarizes.
- The hook environment sets `CLAUDE_MEM_CODEX_HOOK=1`.
- Codex adapter requires valid `cwd` and `session_id`.
- Output supports top-level `continue`, `systemMessage`, `decision`, `reason`,
  and `hookSpecificOutput`.

Classification:

- `preserve`: Codex-native hook coverage before the parity gate can be called
  complete, unless a committed defer rationale explicitly says otherwise.
- `preserve`: strict session validation and response compatibility.
- `replace`: package ids, branding, local script resolution, and worker command.
- `defer`: `PreToolUse` and `Stop` remain outside the first Codex package
  contract; the first package contract covers `SessionStart`, `PostToolUse`,
  `Error`, and `Decision`.

Engram target:

- Codex plugin package is a thin client over the same Engram server API schema.
- Contract tests prove Codex-specific responses emit only supported fields.
- 2026-06-25 checkpoint: `packages/codex-plugin` plus the thin
  `engram_cli hook` commands implement the Codex-native hook contract for
  `SessionStart`, `PostToolUse`, `Error`, and `Decision`.

### Cursor, Gemini CLI, And Other IDE Hooks

Source:

- `src/cli/adapters/cursor.ts`
- `src/services/integrations/CursorHooksInstaller.ts`
- `src/cli/adapters/gemini-cli.ts`
- `src/services/integrations/GeminiCliHooksInstaller.ts`
- `cursor-hooks/*`

Upstream behavior:

- Cursor maps prompt, MCP/shell, file edit, and stop hooks.
- Cursor writes context into `.cursor/rules/claude-mem-context.mdc`.
- Gemini maps `SessionStart`, `BeforeAgent`, `AfterAgent`, `BeforeTool`,
  `AfterTool`, `PreCompress`, and `Notification`.

Classification:

- `defer`: not required for the first parity gate.
- `preserve`: adapter boundary idea for future runtime-neutral integration.
- `replace`: file-based context naming and local worker assumptions.

## Payload Schema And Versioning

Source:

- `src/cli/types.ts`
- `src/core/schemas/agent-event.ts`
- `src/core/schemas/session.ts`
- `src/core/schemas/memory-item.ts`
- `src/services/hooks/server-beta-client.ts`

Upstream behavior:

- Normalized hook input includes session, cwd, prompt, tool, transcript,
  permission, model, and agent identity fields.
- Server beta schemas introduce `serverSessionId`, `contentSessionId`,
  `memorySessionId`, `platformSource`, event source/type, and memory kind/source.
- Event endpoints accept source adapters and source event ids.

Classification:

- `preserve`: normalized hook payload fields and runtime-neutral server concepts.
- `replace`: TypeScript/Zod implementation with Python/DRF serializers and
  fixture-backed schemas.

Engram target:

- All public hook, CLI, plugin, MCP, and context-bundle contracts are versioned.
- Client event id, runtime, session id, sequence or monotonic timestamp, event
  type, payload schema version, and content hash are required where applicable.
- Duplicate event submissions return the existing accepted result.

## CLI, Install, Doctor, And Disconnect

Source:

- `package.json`
- `src/npx-cli/index.ts`
- `src/npx-cli/commands/install.ts`
- `src/npx-cli/commands/doctor.ts`
- `src/npx-cli/commands/uninstall.ts`
- `src/npx-cli/commands/runtime.ts`
- `src/services/hooks/server-beta-bootstrap.ts`

Upstream behavior:

- CLI name is `claude-mem`.
- `install` performs plugin marketplace registration, dependency setup, IDE
  setup, provider/runtime prompts, optional worker start, and health probing.
- There is no top-level `connect` or `disconnect`; practical equivalents are
  `install`, `uninstall`, runtime commands, and server API-key bootstrap.
- `doctor` is read-only and exits nonzero when required checks fail.
- Install error reporting writes categorized `last-install-error.json` with
  remediation.

Classification:

- `preserve`: connect/install, doctor, uninstall/disconnect, dry-run, health
  probe, categorized remediation, and server credential bootstrap semantics.
- `replace`: command naming with `engram connect`, `engram doctor`,
  `engram disconnect`; remove local worker dependency setup.
- `drop`: local worker lifecycle commands as user-facing required runtime.
- `defer`: telemetry management, broad IDE utilities, `adopt`, `cleanup`, and
  transcript watcher unless migration compatibility needs them.

Engram target:

- Golden path command:

  ```bash
  engram connect --server URL --api-key KEY --project PROJECT
  ```

- The command writes thin hook config, stores only allowed local credential
  metadata, and calls dry-run.
- `doctor` verifies server reachability, credential validity, project binding,
  hook state, agent compatibility, and optional MCP bridge state.
- `disconnect` removes local Engram-owned hook entries and credential metadata.

## Config Paths And Environment

Source:

- `src/shared/paths.ts`
- `src/npx-cli/utils/paths.ts`
- `src/shared/SettingsDefaultsManager.ts`
- `src/shared/EnvManager.ts`

Upstream behavior:

- Data defaults to `~/.claude-mem`.
- Settings live in `~/.claude-mem/settings.json`.
- Env file lives in `~/.claude-mem/.env`.
- Claude plugin metadata lives under `~/.claude/plugins/...`.
- Important env vars include worker host/port, provider keys, Chroma settings,
  Redis/queue settings, server beta URL/API key/project, runtime selection, and
  provider/model settings.
- Env isolation blocks leaking Claude auth/OAuth variables into worker env and
  preserves a whitelist for provider config.

Classification:

- `preserve`: env isolation and explicit local config boundaries.
- `replace`: `CLAUDE_MEM_*` variables and `~/.claude-mem` paths with Engram
  names.
- `drop`: local provider secret configuration in client runtime.

Engram target:

- Local state may include server URL, project id, hook metadata, redacted
  credential fingerprint, and scoped agent token.
- Local state must not include provider secrets, memory database, embeddings,
  summarization worker, durable queue, cached memory bundles, or unredacted
  prompt/tool bodies.

## Session And Transcript Lifecycle

Source:

- `src/services/sqlite/schema.sql`
- `src/services/sqlite/SessionStore.ts`
- `src/services/worker/SessionManager.ts`
- `src/services/worker/SessionMessageBuffer.ts`
- `src/services/transcripts/processor.ts`
- `src/cli/handlers/session-init.ts`
- `src/cli/handlers/summarize.ts`

Upstream behavior:

- `sdk_sessions` maps `content_session_id` to `memory_session_id`, project,
  platform source, prompt counter, and status.
- `UserPromptSubmit` creates or reuses a session, stores prompt history, strips
  private tags, truncates oversized prompts, and dedupes repeated prompts.
- `PostToolUse` queues observations with tool name/input/response, cwd, prompt
  number, agent id/type, and optional tool use id.
- `Stop` extracts the last assistant message from hook input or transcript path
  and queues summary generation.
- Transcript processing can replay Claude JSONL session artifacts into sessions,
  prompts, tool events, observations, and session-end actions.
- Worker restart discards stale SDK memory session ids and relies on new SDK
  capture; poisoned sessions respawn while preserving pending messages.

Classification:

- `preserve`: content session id, server session id, prompt counter, project,
  runtime, cwd, transcript provenance, agent identity, status, and prompt/tool
  linkage.
- `replace`: in-memory session buffer and local SQLite session authority.
- `defer`: live transcript watcher and runtime transcript replay. Migration
  import may read transcript artifacts where stable data can be reconstructed,
  but the runtime path must not depend on transcript watching.

Engram target:

- PostgreSQL owns sessions and raw events.
- Hook ingest resolves organization/team/project/agent/session scope before
  writes.
- Session ids and external ids are unique within organization/project scope.
- Migration/import may read transcript artifacts, but runtime must not rely on
  a local transcript watcher.

## Observation Generation

Source:

- `src/sdk/prompts.ts`
- `src/sdk/parser.ts`
- `src/sdk/output-classifier.ts`
- `src/services/worker/agents/ResponseProcessor.ts`
- `src/server/generation/providers/shared/prompt-builder.ts`
- `src/server/generation/processGeneratedResponse.ts`

Upstream behavior:

- Provider prompt asks for XML `<observation>` blocks with `type`, `title`,
  `subtitle`, `facts`, `narrative`, `concepts`, `files_read`, and
  `files_modified`.
- Empty or private-only content returns `<skip_summary />`.
- Parser validates type against mode config, removes the type from concepts,
  skips empty observations, and parses summaries.
- Private, context, and system tags are stripped before generation and again
  before persistence.
- Output classifier distinguishes XML, idle, prose, and poisoned output.
- Invalid/poisoned output has bounded retry/respawn behavior.
- Server beta already models idempotent Postgres persistence using generation
  keys, source links, job status transitions, and audit.

Classification:

- `preserve`: observation field schema and parser behavior as fixture-backed
  observable behavior.
- `preserve`: privacy stripping before provider calls and before persistence.
- `preserve`: skip/empty semantics.
- `preserve`: bounded invalid-output failure semantics.
- `replace`: local SDK multi-turn worker and TypeScript provider path with
  server-side model provider adapters and Celery jobs.

Engram target:

- Provider calls happen only server-side.
- Outbox/Celery job id is the authority for generation retries.
- Observation generation is idempotent by source event id plus content hash or
  generation key.
- Provider errors are classified and redacted.

## Memory Create, Update, Dedupe, Stale, And Refuted Behavior

Source:

- `src/services/sqlite/schema.sql`
- `src/services/sqlite/SessionStore.ts`
- `src/services/sqlite/observations/store.ts`
- `src/services/worker/http/routes/MemoryRoutes.ts`
- `src/server/routes/v1/ServerV1Routes.ts`
- `src/server/generation/processGeneratedResponse.ts`
- `docs/server-beta-architecture-and-team-vision.md`

Upstream behavior:

- Local SQLite stores generated observations and session summaries.
- `observations` includes type, title, subtitle, facts, narrative, concepts,
  files read/modified, prompt number, discovery tokens, agent identity,
  generated model, metadata, timestamps, and content hash.
- Unique `(memory_session_id, content_hash)` collapses duplicate observations.
- Manual memory save creates a discovery observation and optionally syncs to
  Chroma.
- Server beta includes a more durable Postgres observation/source/job/audit
  path, but it is not the final Engram architecture.
- No complete runtime stale/refuted/supersede lifecycle was found. Upstream
  docs treat stale detection, merge, supersede, and contradiction UX as open
  questions.

Classification:

- `preserve`: observation fields, provenance, source links, timestamps, agent
  identity, idempotent dedupe, manual observation insertion, and model metadata.
- `replace`: local SQLite/Chroma persistence with PostgreSQL memory,
  observation, retrieval-document, source, version, and audit records.
- `defer`: rich stale/refuted/supersede lifecycle until the memory-quality gate.
- `drop`: treating telemetry rollups as memory lifecycle state.

Engram target:

- Parity gate needs generated memory or memory candidates plus retrieval
  documents and context-bundle audits.
- If memory candidates are used in the first golden path, the E2E must include
  approval/promotion before context injection.
- Stale/refuted fields may exist as thin contracts, but rich workflow belongs
  after parity is green.

## Search, Retrieval, Ranking, And Citations

Source:

- `src/services/sqlite/SessionSearch.ts`
- `src/services/worker/SearchManager.ts`
- `src/services/worker/search/SearchOrchestrator.ts`
- `src/services/worker/search/HybridSearchStrategy.ts`
- `src/services/worker/search/strategies/ChromaSearchStrategy.ts`
- `src/services/worker/search/types.ts`
- `src/services/worker/FormattingService.ts`
- `src/services/worker/http/routes/SearchRoutes.ts`
- `src/services/context/ObservationCompiler.ts`
- `src/services/context/ContextBuilder.ts`

Upstream behavior:

- SQLite FTS and metadata filters search observations, sessions, and prompts.
- Filters include project, platform source, type, date, concepts, and files.
- Chroma semantic search uses metadata filters, a 90-day recency window by
  default, top-100 semantic candidates, and SQLite hydration.
- Hybrid paths combine semantic order with exact metadata filters.
- Empty query acts as filter-only search.
- Search outputs cite observations as `#<id>`, session summaries as `#S<id>`,
  and prompts as `#P<id>`.
- Context injection queries recent observations and summaries, builds a compact
  timeline, selects full observation ids, adds token-economics stats, and
  returns Markdown.

Classification:

- `preserve`: exact metadata filters, project/platform scoping, recency
  behavior, filter-only path, hybrid semantic plus exact retrieval intent, and
  compact citation ids.
- `replace`: SQLite FTS5 and Chroma with PostgreSQL full-text/trigram plus
  pgvector or equivalent server-side retrieval documents.
- `preserve`: context bundle as the primary output, not a raw result list.

Engram target:

- Authorization filters run before ranking and packing.
- Retrieval documents include memory id/version, tenant/project/repository
  scope, source observation ids, file paths, symbols, exact terms, full-text
  body, and embedding reference.
- Context bundle output includes selected memories, citations, scope evidence,
  inclusion reason, freshness state, token metadata, and audit id.

### Semantic Retrieval Gate For First E2E

Roadmap item 10 is explicitly deferred for the first parity E2E path unless the
checked-in fixture later proves semantic recall is required.

Not reproduced in the first E2E path:

- local Chroma semantic search and Chroma metadata storage;
- Chroma top-k ordering with the upstream 90-day recency window;
- SQLite hydration of semantic observation, session-summary, and prompt ids;
- hybrid semantic ordering plus SQLite metadata filters for concept/type/file
  searches;
- `/api/context/semantic` and optional prompt-submit semantic injection.

Reason:

- the first golden fixture is exact and deterministic: hook/tool evidence names
  commands, file paths, and source observations that become approved memory,
  retrieval documents, and a future session-start context bundle;
- the merged exact retrieval/context API can prove authorization-before-ranking,
  cited bundle output, replay idempotency, and wrong-project denial without
  provider secrets, embeddings, or vector storage;
- prompt-submit semantic recall is useful upstream behavior, but it is not part
  of the first session-start golden path.

Trigger to implement before the first E2E:

- if the fixture is changed so the future context request can only succeed by
  paraphrase/semantic recall rather than file path, symbol, command, ticket id,
  exact term, or full-text match; or
- if the first installed hook/client contract includes prompt-submit semantic
  injection instead of session-start context injection.

Later owner:

- semantic retrieval remains a V1 requirement after the exact CLI/hooks/API E2E
  loop is green, implemented through server-side model policy, embeddings, and
  PostgreSQL-backed vector storage or an explicitly documented equivalent.

## Context Injection Format

Source:

- `src/cli/handlers/context.ts`
- `src/cli/handlers/session-init.ts`
- `src/cli/handlers/user-message.ts`
- `src/cli/handlers/file-context.ts`
- `src/services/context/ContextBuilder.ts`
- `src/services/context/formatters/AgentFormatter.ts`
- `src/services/context/sections/*`

Upstream behavior:

- `SessionStart` calls `/api/context/inject?projects=...`.
- Claude Code gets colors for user-facing terminal output when enabled.
- Hook response includes:

  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": "..."
    }
  }
  ```

- Optional `systemMessage` can show context and live viewer link.
- Empty-state text explains that no memory exists yet.
- Optional semantic injection on `UserPromptSubmit` is gated by
  `CLAUDE_MEM_SEMANTIC_INJECT`, prompt length, and `/api/context/semantic`.
- File-read context can inject a per-file timeline for large stale files.

Classification:

- `preserve`: agent-specific response shape and `additionalContext` behavior.
- `preserve`: compact Markdown context with citations.
- `replace`: branding, local viewer links, and local worker endpoint.
- `defer`: semantic prompt injection and file-read timeline until after the
  first session-start context golden path, unless fixture parity requires them.

Engram target:

- Context bundle has a stable machine-readable form and a rendered agent text
  form.
- The hook adapter renders only fields supported by the target agent.
- Every injected memory is authorized, cited, explainable, bounded, and audited.

## MCP Commands

Source:

- `src/servers/mcp-server.ts`
- `plugin/.mcp.json`
- `.codex-plugin/plugin.json`
- `.claude-plugin/plugin.json`

Upstream behavior:

- MCP exposes worker-backed tools: `search`, `timeline`, `get_observations`,
  smart file tools, corpus tools, and onboarding/help.
- MCP also has server-beta tools: `observation_add`,
  `observation_record_event`, `observation_search`, `observation_context`,
  `observation_generation_status`, plus compatibility aliases
  `memory_add`, `memory_search`, `memory_context`.
- MCP server auto-starts the local worker unless server-beta runtime is
  selected.
- MCP stdio protects protocol output by routing console output into logs.

Classification:

- `preserve`: MCP as an agent-native interface over the same server APIs.
- `preserve`: basic tool intents: search, observe/add, context, feedback/status.
- `replace`: MCP must never auto-start a local memory worker.
- `defer`: implementing MCP bridge is deferred until after the first CLI/hooks
  parity loop unless the golden path needs it.
- `drop`: corpus and smart file tools from the first parity gate.

Engram target:

- MCP bridge is a thin client over Engram server APIs with the same RBAC and
  audit behavior as HTTP.

## User-Visible Failure Modes

Source:

- `src/cli/hook-command.ts`
- `src/shared/hook-io.ts`
- `src/shared/worker-utils.ts`
- `src/npx-cli/commands/doctor.ts`
- `src/npx-cli/commands/runtime.ts`
- `src/npx-cli/install/error-reporter.ts`
- `src/services/hooks/server-beta-client.ts`

Upstream behavior:

- Adapter rejection and missing transcript path are nonblocking.
- Worker unavailable usually exits 0 with quiet `continue:true` behavior.
- Repeated worker unreachable failures can trip fail-loud blocking feedback.
- Unknown hook errors emit blocking feedback and exit code 2.
- Doctor is read-only and exits 1 for required failures.
- Install errors are categorized and written with remediation.
- Runtime/search errors distinguish not installed, missing Bun, corrupted
  install, worker not running, HTTP failure, endpoint missing, and invalid JSON.
- Server-beta client classifies missing API key, transport, timeout,
  HTTP error, and invalid response.

Classification:

- `preserve`: nonblocking default for transient hook failures.
- `preserve`: explicit blocking path for malformed or unsafe hook execution.
- `preserve`: categorized doctor/connect errors and remediation.
- `replace`: local worker/Bun errors with Engram server/client health,
  credential, project, provider, worker, and schema errors.

Engram target failure set for the first gate:

- missing server URL;
- bad or expired API key;
- wrong project/tenant;
- server unavailable;
- malformed hook payload;
- unsupported agent version;
- missing provider secret;
- provider failure;
- duplicate event replay;
- worker/outbox retry in progress;
- hook/plugin trust mismatch.

## Worker And Local Runtime Replacement

Source:

- `src/services/worker-service.ts`
- `src/services/worker-spawner.ts`
- `src/shared/worker-utils.ts`
- `src/services/worker/SessionManager.ts`
- `src/services/worker/SessionMessageBuffer.ts`
- `src/services/worker/http/routes/*`
- `src/server/jobs/ServerJobQueue.ts`

Upstream behavior:

- Local worker daemon owns HTTP endpoints, SQLite, optional Chroma, provider
  calls, in-memory queue state, session SDK subprocesses, viewer routes, MCP
  bridge support, and health checks.
- Pending tool messages dedupe by tool-use id and idle-timeout after three
  minutes.
- Server beta says BullMQ is execution transport only and Postgres outbox is
  canonical.

Classification:

- `replace`: local worker daemon with Django API plus Celery workers.
- `replace`: in-memory pending buffer with durable outbox and idempotent jobs.
- `replace`: local provider calls with server-side provider adapters.
- `drop`: local viewer as operational source of truth.
- `preserve`: health, queue/backlog visibility, idempotency, retry, and recovery
  semantics.

Engram target:

- Accepted hook/API event writes raw event, normalized observation, and outbox
  entry in one PostgreSQL transaction.
- Celery tasks accept outbox/job ids, reload authoritative state, and tolerate
  duplicate delivery.
- Provider calls, embeddings, memory writes, retrieval updates, and context
  audits are keyed by stable idempotency keys.

## Migration Compatibility

Source:

- `src/services/sqlite/schema.sql`
- `src/services/sqlite/SessionStore.ts`
- `src/services/transcripts/processor.ts`
- `src/shared/paths.ts`
- `src/storage/postgres/schema.ts`

Upstream data sources:

- `~/.claude-mem/claude-mem.db`
- `~/.claude-mem/settings.json`
- `~/.claude-mem/transcript-watch.json`
- `~/.claude-mem/transcript-watch-state.json`
- `~/.claude-mem/corpora`
- `~/.claude-mem/vector-db` or Chroma runtime data
- Claude JSONL transcript files under the agent runtime's project storage

Classification:

- `preserve`: useful session, prompt, observation, summary, provenance,
  timestamp, project, platform, file, concept, agent, model, and citation data.
- `replace`: target storage is Engram PostgreSQL and retrieval documents.
- `drop`: Chroma/vector contents as authoritative import source; rebuild
  retrieval documents from imported durable records.
- `defer`: corpora and smart-file artifacts.
- `defer or report unsupported`: transcript replay when no stable observation
  or summary can be reconstructed.

Importer requirements for the parity gate:

- dry-run report with counts by source type;
- stable external ids so reruns do not duplicate records;
- preservation of provenance, timestamps, project/session scope, citations, and
  original source references where available;
- explicit unsupported-record report;
- idempotent fixture-backed regression test using sanitized upstream artifacts.

## First Parity Gate Implementation Surface

Required before North Star expansion:

- `engram connect`, `engram doctor`, and `engram disconnect` thin-client flow.
- Codex hook package and thin CLI event commands are implemented for
  `SessionStart`, `PostToolUse`, `Error`, and `Decision` through
  `packages/codex-plugin` and `packages/cli`.
- Claude Code native package and thin CLI response formatting are implemented
  for `SessionStart`, `PostToolUse`, `Error`, and `Decision` through
  `packages/claude-plugin` and `packages/cli`.
- Hook/session-start request to context API.
- Hook observation ingest to API.
- PostgreSQL raw event envelope, normalized observation, generated memory or
  memory candidate, retrieval document, and context-bundle audit records.
- Durable outbox and Celery worker path that creates or updates useful memory.
- Context API that returns authorized cited context.
- Docker Compose golden path proving hook/CLI to next-session context
  injection.
- Migration/import path or unsupported-record report path with idempotent
  fixture-backed test.

Explicitly out of the first parity implementation unless the fixture requires
it:

- `PreToolUse` and `Stop` runtime hook coverage;
- live transcript replay/watch; migration import handles supported static
  upstream artifacts instead of adding a runtime transcript watcher;
- custom admin UI depth;
- MCP bridge implementation;
- Cursor/Gemini/OpenAI Agents support;
- broad semantic retrieval breadth beyond the first fixture;
- rich stale/refuted/merge/supersede UX;
- production Helm;
- signed plugin release channels;
- local viewer parity.

## First Golden Fixture Proposal

The first checked-in sanitized fixture should cover:

1. Codex `SessionStart` with project/cwd/session id.
2. Empty context response for first session.
3. Codex `PostToolUse` with command or file evidence worth remembering.
4. Codex `Error` with sanitized failure evidence when the scenario needs error
   capture.
5. Codex `Decision` with the agent decision metadata needed by the fixture.
6. Worker job creates one durable memory from the event.
7. Future `SessionStart` returns a context bundle that cites that memory.
8. Duplicate `PostToolUse` replay does not create a duplicate memory.
9. Wrong project or API key is denied before retrieval.

`Stop` is deferred for runtime hook coverage in this checkpoint. If the fixture
needs last-assistant-message or transcript-derived evidence, cover it only
through migration/import artifacts and the unsupported-record report path, not
through the runtime golden path.

The first runtime fixture was Codex-led. Checkpoint `128b2afe` adds Claude Code
package and response-format coverage for the same current event set:
`SessionStart`, `PostToolUse`, `Error`, and `Decision`. Runtime fixture evidence
must still not claim `UserPromptSubmit`, `PreToolUse`, `Stop`, MCP, semantic
retrieval breadth, or plugin release-channel readiness.
