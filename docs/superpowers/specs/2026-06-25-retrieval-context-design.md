# Retrieval Context Design

## Goal

Add the first server-side context assembly slice: index approved memory into
retrieval documents, run authorization-filtered exact retrieval, and return a
persisted cited context bundle from the context API.

This slice is backend-only. It does not add semantic/vector retrieval, provider
embeddings, model reranking, memory candidate promotion, CLI connect behavior,
frontend screens, MCP tools, or Docker golden-path fixtures.

## Current Gate

The current roadmap item is "Add retrieval documents, exact search, and
context bundle API." The previous checkpoint created proposed memory candidates
from accepted observations. The next missing parity behavior is that a future
agent session can call the server and receive authorized cited memory context
from approved memory records.

The hard parity gate eventually requires a generated memory, retrieval
document, and future session context bundle in one Docker Compose E2E path.
This checkpoint creates the context API and exact retrieval contract that later
promotion, CLI, hook, semantic, and E2E slices will consume.

## Approaches Considered

### Context App With Exact Retrieval First

Create `engram.context` with one indexing service, one context assembly service,
and DRF views for `/v1/context/session-start` and `/v1/context`. The service
uses `ResolveApiKeyScope` with `memories:read`, applies scope filters before
ranking, scores existing `RetrievalDocument` rows with deterministic exact
matching, writes `ContextBundle` and `ContextBundleItem` records, and returns
machine-readable items plus compact rendered context.

Tradeoff: this does not provide semantic recall yet, but it proves the
authorization, citation, persistence, and agent response shape without provider
or vector dependencies.

### Add Semantic Retrieval Now

Add embedding references, provider policy, OpenAI embeddings, vector storage,
and hybrid fusion in the same slice.

Tradeoff: closer to final V1 retrieval, but it requires model policy, provider
secrets, worker retries, vector migrations, and security review around provider
payloads. That is too wide for the first context API checkpoint.

### Search Endpoint First

Implement `/v1/search` as a raw result list and use it later to build context.

Tradeoff: easier to test in isolation, but it conflicts with the North Star
that context bundles are the primary product output. Raw search can be added
later as a debug/admin surface over the same retrieval service.

## Decision

Create `engram.context` with:

- `IndexMemoryVersion.execute()` to create or refresh one `RetrievalDocument`
  from an approved `MemoryVersion`;
- `BuildContextBundle.execute()` to authorize, retrieve, rank, persist, audit,
  and render one context bundle;
- `POST /v1/context/session-start` for hook session-start injection;
- `POST /v1/context` for task-focused context requests using the same service;
- focused pytest coverage for authorization-before-ranking, exact matching,
  cited output, idempotent request ids, empty/filter-only context, and
  redaction-safe responses.

The implementation accepts that the promotion path is not done yet. Tests create
approved `Memory`, `MemoryVersion`, and `RetrievalDocument` rows directly or
through the indexing service. A later promotion slice will call
`IndexMemoryVersion` when candidate approval exists.

## Request Contract

Both context endpoints use bearer API-key authentication and a JSON request:

```json
{
  "project_id": "uuid",
  "team_id": "uuid-or-null",
  "agent_runtime": "codex",
  "agent_version": "0.0.0",
  "agent_external_id": "local-agent-id",
  "session_id": "external-session-id",
  "request_id": "client-request-id",
  "correlation_id": "client-correlation-id",
  "trace_id": "client-trace-id",
  "repository_url": "https://example/repo.git",
  "repository_root": "/workspace/repo",
  "branch": "main",
  "cwd": "/workspace/repo",
  "query": "task or prompt text",
  "file_paths": ["apps/backend/engram/context/services.py"],
  "symbols": ["BuildContextBundle"],
  "limit": 5,
  "token_budget": 2000
}
```

`project_id`, `team_id`, and repository fields are request hints. The service
uses the API-key scope resolver as the authority. If the key is bound to one
team and the request omits `team_id`, accepted rows use the key-bound team.

`query`, `file_paths`, and `symbols` are optional. An empty query is a
filter-only context request and returns the newest authorized approved memory up
to the limit.

## Response Contract

Successful responses return:

```json
{
  "status": "created",
  "request_id": "client-request-id",
  "context_bundle_id": "uuid",
  "purpose": "session_start",
  "rendered_context": "# Engram context\n\n- [M1] ...",
  "hook_specific_output": {
    "hookEventName": "SessionStart",
    "additionalContext": "# Engram context\n\n- [M1] ..."
  },
  "items": [
    {
      "citation": "M1",
      "memory_id": "uuid",
      "memory_version_id": "uuid",
      "retrieval_document_id": "uuid",
      "title": "Memory title",
      "body": "Memory body",
      "inclusion_reason": "exact match: services.py",
      "scope_evidence": {
        "visibility_scope": "project",
        "project_id": "uuid",
        "team_id": "uuid"
      },
      "matched_terms": ["services.py"]
    }
  ],
  "warnings": []
}
```

`/v1/context/session-start` sets `purpose` to `session_start` and includes the
Claude-compatible `hook_specific_output` shape for the thin hook adapter.
`/v1/context` sets `purpose` to `task` and returns the same stable bundle and
item fields without changing persistence semantics.

Duplicate calls with the same organization, project, and `request_id` return
the existing bundle and items. They must not create duplicate
`ContextBundleItem` or audit rows.

Error responses reuse the existing access codes:

- `missing_api_key` -> HTTP 401;
- `invalid_key` -> HTTP 401;
- `inactive_key`, `revoked_key`, `expired_key`, `inactive_owner` -> HTTP 403;
- `missing_capability`, `project_scope_denied`, `team_scope_denied` -> HTTP 403;
- serializer validation errors -> HTTP 400.

## Authorization And Scope Filtering

`BuildContextBundle.execute()` resolves API-key scope with required capability
`memories:read` before any retrieval query runs. Denied requests produce no
context bundle.

Allowed retrieval candidates must satisfy all of:

- same organization as the resolved scope;
- requested project is inside `scope.project_ids`;
- memory status is `approved`;
- memory and retrieval document are not stale or refuted;
- project visibility is allowed when the project is in scope;
- team visibility is allowed only when the document team is in
  `scope.team_ids`;
- session and organization visibility are excluded in this slice unless later
  approval rules are implemented.

The service records the applied scope in `ContextBundle.authorization_scope` and
each selected item's `scope_evidence`.

## Exact Retrieval And Ranking

Exact retrieval is deterministic and sqlite-compatible for tests. It does not
depend on PostgreSQL FTS or trigram extensions in this checkpoint.

The service normalizes search terms from:

- `query`;
- `file_paths`;
- `symbols`;
- repository root, cwd, and branch metadata when present;
- retrieval document `exact_terms`, `file_paths`, `symbols`, and `full_text`.

Ranking is applied after authorization filtering:

1. file path exact or suffix match;
2. symbol exact match;
3. explicit exact term match;
4. full-text phrase or token match;
5. filter-only recency when no query terms are provided.

Candidates with no score are excluded when the request has terms. Ties are
resolved by score descending, retrieval document update time descending, memory
title, and id. The default limit is 5 and the maximum accepted limit is 10.

PostgreSQL FTS, trigram, pgvector, embeddings, and hybrid fusion remain required
V1 work, but they are separate from this exact-search checkpoint.

## Retrieval Document Indexing

`IndexMemoryVersion.execute(memory_version_id)` creates or refreshes a
`RetrievalDocument` for an approved memory version.

The document copies:

- organization, project, team, memory, memory version, and visibility scope from
  the memory;
- body and title into `full_text`;
- source observation file references when present;
- optional `memory.metadata.file_paths`, `memory.metadata.symbols`, and
  `memory.metadata.exact_terms`;
- stale/refuted flags from the memory.

The indexer rejects non-approved, archived, refuted, or cross-scope records.
It never stores embeddings or provider payloads in this slice.

## Context Bundle Persistence And Audit

For a new request id, the service creates:

- one `Agent` and `AgentSession` if they do not already exist;
- one `ContextBundle` with purpose, query, rendered text, selected count,
  token budget, and authorization scope;
- one `ContextBundleItem` per selected memory with citation `M1`, `M2`, and so
  on;
- one `AuditEvent` with `event_type="MemoryRetrieved"`,
  `capability="memories:read"`, result `allowed`, target type
  `context_bundle`, target id set to the bundle id, selected count, and
  retrieval strategy metadata.

Audit metadata contains ids, counts, scope filters, strategy names, and matched
terms. It does not contain raw API keys, bearer tokens, memory bodies, query
prompt bodies beyond existing `ContextBundle.query_text`, provider secrets, or
raw hook payloads.

## Boundaries

This slice owns:

- exact retrieval over existing retrieval documents;
- retrieval document indexing for approved memory versions;
- context API request/response serializers;
- session-start and task context endpoints;
- context bundle and item persistence;
- successful retrieval audit records;
- authorization-before-ranking tests.

This slice defers:

- memory candidate approval and promotion;
- provider calls, embeddings, pgvector, trigram, and semantic fusion;
- context summarization and token-aware packing beyond a simple item limit;
- stale/conflict UX beyond excluding stale/refuted rows;
- CLI `connect`, `doctor`, and `disconnect`;
- hook package installation and local config writes;
- frontend/admin search debugger;
- MCP tools;
- Docker Compose golden fixture;
- migration/import compatibility.

## Testing

Tests must prove behavior:

- session-start requires a bearer key and `memories:read`;
- wrong-project keys are denied before retrieval and create no bundle;
- approved project memory with an exact file/term match returns cited rendered
  context, bundle rows, item rows, and a success audit record;
- team-scoped memory outside the resolved team is filtered out before ranking;
- empty/filter-only session-start returns authorized approved project memory;
- duplicate request ids return the existing bundle without duplicate items or
  audit rows;
- `/v1/context` uses the same service with purpose `task`;
- `IndexMemoryVersion` creates a retrieval document from an approved memory
  version and rejects non-approved memory;
- responses, bundle metadata, item metadata, and audit metadata do not contain
  raw API keys or bearer tokens.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest engram/context/context_api_tests.py -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings`
- `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`
- `docker compose version`

Docker Compose smoke remains blocked until Docker is available in this WSL
distro.

## Self-Review

- Scope is one backend parity slice: no frontend, CLI, MCP, provider, semantic,
  or Docker E2E work is included.
- Authorization runs before retrieval, ranking, packing, and response building.
- The design uses existing core models and access services instead of adding a
  parallel scope model.
- Exact retrieval is intentionally simple and deterministic; semantic/hybrid
  retrieval remains a documented follow-up.
- The API produces context bundles, not raw search results.
