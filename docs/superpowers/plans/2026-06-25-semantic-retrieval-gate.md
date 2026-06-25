# Semantic Retrieval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the semantic retrieval deferral required by roadmap item 10 so
the next implementation slice can proceed to CLI without silently skipping the
semantic gate.

**Architecture:** Documentation-only checkpoint. Update the parity map from
upstream source evidence and commit the design/plan that explains why exact
retrieval is sufficient for the first parity E2E fixture.

**Tech Stack:** Markdown docs, repository quality checks, Python unittest
repository checks.

## Global Constraints

- Work on branch `chore/parity-10-semantic-deferral`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Do not add backend code, migrations, provider calls, embeddings, vector
  storage, CLI behavior, frontend files, MCP tools, or Docker E2E fixtures.
- Preserve the V1 target that semantic retrieval is required after the first
  exact CLI/hooks/API E2E loop.
- The parity map must name upstream semantic behavior and the reason exact
  retrieval is sufficient for the first fixture.

---

### Task 1: Semantic Retrieval Gate Decision

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-semantic-retrieval-gate-design.md`
- Create: `docs/superpowers/plans/2026-06-25-semantic-retrieval-gate.md`
- Modify: `docs/parity/claude-mem-parity-map.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/parity/claude-mem-parity-map.md`,
  `docs/search-and-retrieval.md`, upstream
  `src/services/worker/search/strategies/ChromaSearchStrategy.ts`, upstream
  `src/services/worker/search/strategies/HybridSearchStrategy.ts`, upstream
  `src/services/worker/http/routes/SearchRoutes.ts`, and the merged exact
  retrieval/context API checkpoint.
- Produces: committed semantic retrieval deferral decision.

- [ ] **Step 1: Record upstream semantic behavior**

Add parity-map text naming local Chroma semantic search, hybrid metadata
filters, the 90-day recency window, SQLite hydration, `/api/context/semantic`,
and optional prompt-submit semantic injection.

- [ ] **Step 2: Record first-fixture sufficiency**

Document that the first golden fixture is exact: command/file evidence becomes
server-side memory and retrieval documents, then future session-start context
matches by file path, symbol, command, or exact term. It does not require
paraphrase recall or prompt-submit semantic injection.

- [ ] **Step 3: Record implementation trigger**

Document that semantic retrieval must be implemented before the first E2E only
if the checked-in fixture depends on paraphrase recall or `/v1/context` prompt
submit behavior that exact retrieval cannot satisfy.

- [ ] **Step 4: Run docs verification**

Run:

```bash
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
git diff --check HEAD
docker compose version
```

Expected:

- first three commands exit 0;
- Docker exits 1 in this WSL distro because Docker is unavailable.

- [ ] **Step 5: Commit**

Commit:

```bash
git add docs/superpowers/specs/2026-06-25-semantic-retrieval-gate-design.md docs/superpowers/plans/2026-06-25-semantic-retrieval-gate.md docs/parity/claude-mem-parity-map.md
git commit -m "chore: record semantic retrieval gate"
```

### Task 2: PR And Merge Checkpoint

**Files:**

- No additional source files.

**Interfaces:**

- Consumes: Task 1 commit.
- Produces: merged docs checkpoint and recorded CI status.

- [ ] **Step 1: Open PR**

Open a PR against `master` summarizing the semantic retrieval deferral and local
verification.

- [ ] **Step 2: Wait for CI**

Record Backend and Repository Quality job statuses.

- [ ] **Step 3: Merge**

When CI passes, merge with the normal non-force flow and update local `master`.
