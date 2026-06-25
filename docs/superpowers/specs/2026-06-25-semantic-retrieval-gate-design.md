# Semantic Retrieval Gate Design

## Goal

Resolve roadmap item 10 before the CLI slice by recording whether semantic
retrieval is required for the first parity E2E path.

This slice is documentation-only. It does not add provider calls, embeddings,
pgvector migrations, semantic search APIs, retrieval ranking code, CLI behavior,
frontend screens, MCP tools, or Docker E2E fixtures.

## Current Gate

`goal.md` says to add a semantic retrieval adapter only when exact/context
parity needs it. If semantic retrieval is deferred for the first E2E path, the
parity map must record which upstream semantic behavior is not reproduced and
why exact/context retrieval is sufficient for the gate fixture.

The previous checkpoint implemented authorized exact retrieval and context
bundle APIs. The next implementation roadmap item is CLI `connect`, `doctor`,
and `disconnect`, but starting CLI before documenting the semantic retrieval
gate would silently skip item 10.

## Upstream Semantic Behavior

The upstream branch provides semantic retrieval through local Chroma:

- `src/services/worker/search/strategies/ChromaSearchStrategy.ts` queries
  Chroma, filters by project/doc type, applies a 90-day recency window, and
  hydrates observations, session summaries, and prompts from SQLite.
- `src/services/worker/search/strategies/HybridSearchStrategy.ts` combines
  semantic Chroma ordering with SQLite metadata filters for concept, type, and
  file searches.
- `src/services/worker/http/routes/SearchRoutes.ts` exposes
  `/api/context/semantic`, skips prompts shorter than 20 characters, returns
  empty context on query errors, and renders "Relevant Past Work" from matching
  observations.
- semantic prompt injection is optional and tied to prompt-submit/focused
  recall behavior, not to the first session-start context fixture.

## Decision

Defer semantic retrieval for the first parity E2E path.

The first golden fixture proposed in the parity map is exact and deterministic:
it records a hook observation with command/file evidence, creates useful
server-side memory, indexes retrieval documents, and verifies that a future
session-start request receives an authorized cited context bundle. That fixture
can use file path, symbol, command, and exact-term matches. It does not require
paraphrase recall, Chroma ordering, `/api/context/semantic`, or prompt-submit
semantic injection.

This decision preserves the V1 requirement for hybrid exact plus semantic
retrieval. It only defers provider/model-policy, embedding generation, vector
storage, and semantic prompt injection until after the first CLI/hooks/API E2E
loop is green.

## Required Parity Map Update

Add an explicit "Semantic Retrieval Gate" note to
`docs/parity/claude-mem-parity-map.md` that records:

- which upstream semantic behaviors are not reproduced in the first E2E path;
- why exact/context retrieval is enough for the first fixture;
- what must trigger implementing semantic retrieval before the first E2E path;
- which later gate owns the semantic retrieval adapter.

## Boundaries

This slice owns:

- the semantic retrieval gate decision;
- parity-map wording for roadmap item 10;
- documentation verification.

This slice defers:

- OpenAI embeddings;
- pgvector or equivalent vector storage;
- Chroma-compatible semantic ordering;
- `/v1/context/semantic` or prompt-submit semantic APIs;
- model policy and provider-secret integration;
- hybrid rank fusion beyond exact retrieval.

## Verification

Required local commands:

- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `git diff --check HEAD`
- `docker compose version`

Docker remains blocked in this WSL distro until Docker Desktop WSL integration
is available.

## Self-Review

- The decision does not weaken V1's hybrid retrieval target; it only scopes the
  first parity E2E fixture.
- The first fixture remains exact and auditable, so it can prove the
  CLI/hooks/API loop without provider or vector infrastructure.
- The parity map remains the authoritative place for upstream behavior
  classification.
