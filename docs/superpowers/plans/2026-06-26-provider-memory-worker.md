# Provider Memory Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the memory worker use model-policy-resolved provider generation before creating memory candidates.

**Architecture:** Keep `ProcessObservationRecorded` as the worker entrypoint. Add a provider-generation helper inside `engram.memory.services` that resolves the `generation` model policy, calls `FakeProviderGateway`, and stores redacted provider provenance on the candidate and promoted memory. Keep exact retrieval and context bundle ranking unchanged.

**Tech Stack:** Django, DRF, pytest, Celery, django-celery-outbox, existing `engram.model_policy` fake provider gateway.

## Global Constraints

- No real provider network calls in this slice.
- No embeddings, pgvector, semantic retrieval, hybrid ranking, frontend, MCP, or digest scheduler.
- Celery task payloads must remain stable ids only.
- Raw provider secrets, API keys, prompt bodies, and raw token-shaped tool output must not be persisted in candidates, memories, provider call records, audit metadata, logs, or task payloads.
- Duplicate worker delivery must not create duplicate memory rows or duplicate provider call records.
- Missing or disabled generation policy must fail before candidate, memory, retrieval document, or provider call writes.
- Use existing `ResolveModelPolicy` and `FakeProviderGateway`; do not invent a parallel provider abstraction.

---

### Task 1: Provider-Backed Memory Worker

**Files:**
- Modify: `apps/backend/engram/memory/services.py`
- Modify: `apps/backend/engram/memory/memory_worker_tests.py`
- Modify: `apps/backend/engram/model_policy/services.py`
- Modify: `apps/backend/engram/model_policy/model_policy_tests.py`
- Modify: `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- Modify: `apps/backend/engram/core/golden_path_tests.py`
- Modify: `apps/backend/engram/celeryconfig.py`
- Modify: `apps/backend/engram/core/celery_foundation_tests.py`
- Modify: `scripts/e2e_golden_path.py`
- Modify: `docs/security/reviews/2026-06-26-provider-memory-worker.md`
- Modify: `docs/verification-matrix.md`

**Interfaces:**
- Consumes: `ResolveModelPolicy.execute(ResolveModelPolicyInput(..., task_type='generation'))`
- Consumes: `FakeProviderGateway.call(ProviderCallInput(...))`
- Produces: provider-backed `ProcessObservationRecorded.execute(MemoryCandidateWorkerInput(...))`
- Produces: candidate evidence and memory metadata provider provenance fields:
  `provider_call_id`, `provider`, `model`, `policy_id`, `policy_version`,
  `task_type`, `redaction_state`

- [x] Write design spec.
- [x] Write implementation plan.
- [x] Write failing test: provider-backed worker creates one provider call and stores provenance.
- [x] Run focused test and verify RED.
- [x] Write failing test: duplicate worker delivery reuses candidate/memory/retrieval document and provider call.
- [x] Run focused test and verify RED.
- [x] Write failing test: missing generation policy fails before writes.
- [x] Run focused test and verify RED.
- [x] Write failing test: token-shaped values are redacted through provider-backed candidate and memory.
- [x] Run focused test and verify RED.
- [x] Write failing test: fake provider output drives candidate title/body.
- [x] Run focused test and verify RED.
- [x] Write failing test: existing candidate missing provenance is updated before promotion.
- [x] Run focused test and verify RED.
- [x] Implement fake provider call idempotency by stable request id.
- [x] Implement memory worker provider-generation helper.
- [x] Persist provider provenance in candidate evidence and memory metadata.
- [x] Reuse reference-backend Celery Sentinel result backend pattern while preserving Engram queues and outbox transport.
- [x] Run focused memory worker tests until green.
- [x] Run focused model-policy tests.
- [x] Run adjacent hook/context/memory feedback/Celery tests.
- [x] Run repository checks and migration drift check.
- [x] Update Compose E2E assertions for provider-generated memory title/body and verify green.
- [x] Run independent security review.
- [x] Run Karpathy simplicity/scope review and fix `CHANGES_REQUIRED` findings.
- [x] Run final Compose backend gate.
- [x] Record verification evidence.
- [x] Commit with `feat: add provider memory worker`.
