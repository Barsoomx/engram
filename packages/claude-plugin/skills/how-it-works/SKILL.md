---
name: how-it-works
description: Explain how Engram captures activity, distills it into memory, and injects context into future sessions. Use when the user asks "how does Engram work?", "what is this doing?", or wants to understand the memory pipeline.
---

# How Engram works

Engram is the engineering-memory layer between a codebase and AI coding agents.
It is server-backed: local hooks and CLI are thin clients, while all storage,
distillation, retrieval, and model calls happen on the Engram server.

## The loop

1. **Capture** — the installed plugin's hooks send session activity
   (`SessionStart`, `PostToolUse`, `SessionEnd`, `UserPromptSubmit`) to the Engram
   API. Each event becomes a raw event plus a normalized **observation**, scoped
   to organization/project (routed by the repo's git `repository_url`).
2. **Distill** — Celery workers (through a transaction-safe outbox) turn
   observations into durable **memory** using server-side model policy, with
   deduplication and versioning.
3. **Retrieve** — on the next `SessionStart` / `UserPromptSubmit`, the client asks
   the context API, which runs authorized hybrid (exact + semantic) retrieval and
   packs a bounded, cited **context bundle**.
4. **Inject** — that bundle is returned as `additionalContext`, so the next
   session starts primed with what the team already learned.

## What this means for you

- Reading and working in a repo passively builds its memory (via `PostToolUse`).
- Run `learn-codebase` to prime a repo up front.
- Run `mem-search` (or `engram search`) to recall before acting.
- Memory is organization/project-scoped and cited — treat it as verifiable
  evidence, not ground truth.
