# Provider Memory Worker Design

## Goal

Use the model-policy/provider foundation in the live memory worker path so
captured observations are converted into memory candidates through a
server-side provider boundary instead of a local deterministic copy.

## Scope

This slice is backend-only. It adds a provider-backed generation step to
`ProcessObservationRecorded`, using the existing fake provider gateway for
deterministic tests. It keeps exact retrieval, promotion, and context bundle
behavior unchanged.

In scope:

- resolve a `generation` model policy from the observation organization,
  project, and team;
- call the fake provider gateway without putting prompt bodies or provider
  secrets in queued task payloads;
- persist provider-call provenance on the memory candidate and promoted memory;
- keep duplicate worker delivery idempotent for candidate, memory, retrieval
  document, and provider call records;
- fail cleanly when no usable model policy or secret exists;
- prove redaction through the worker path.

Out of scope:

- real Anthropic/OpenAI network calls;
- embeddings, pgvector, semantic retrieval, and hybrid ranking;
- daily digest scheduling;
- frontend/admin UI and MCP tools;
- new Celery queues or a new transport model.

## Current Behavior

`ProcessObservationRecorded` locks an observation, builds a proposed memory
candidate by copying and redacting the observation title/body, immediately
promotes that candidate to approved memory, and indexes a retrieval document.
This is useful for the first exact parity loop, but it does not prove the V1
server-side provider workflow.

`ModelPolicy` and `FakeProviderGateway` now exist. The gateway records
provider/model/policy version/request metadata and never stores raw secrets or
prompt bodies. The memory worker should consume that boundary.

## Design

Add a narrow provider generation helper in `engram.memory.services`.

The helper receives a locked `Observation`, resolves `ResolveModelPolicy` with
`task_type='generation'`, builds a redacted prompt from observation title, body,
files, and source metadata, calls `FakeProviderGateway`, and returns candidate
title/body/provenance. The first fake provider output can stay deterministic:
it should derive candidate text from redacted observation fields while recording
the provider call. This proves the server-side boundary without pretending to
have real LLM output.

Candidate idempotency stays anchored to the observation id and content hash.
Provider call idempotency is anchored to a stable worker request id:
`memory-worker:<observation_id>:generation`. If a duplicate worker delivery
finds an existing candidate, it must not create a second provider call record.

Provider provenance is stored in candidate evidence and memory metadata:

- provider call id;
- provider;
- model;
- policy id;
- policy version;
- task type;
- redaction state.

No provider secrets, API keys, prompt body, or raw tool output may be stored in
candidate evidence, memory metadata, audit metadata, logs, or task payloads.

If no active generation policy exists, the worker raises before creating a
candidate, memory, version, retrieval document, or provider call record. This
keeps missing provider configuration visible instead of silently falling back to
the old deterministic copy path.

## Alternatives Considered

### Embedding Lifecycle First

This would formalize `embedding_reference` and prepare for semantic retrieval.
It is valuable, but it does not exercise memory generation, which is the first
provider-backed AI workflow requirement after model-policy exists.

### Full Semantic Retrieval Now

This would add embeddings, vector storage, and hybrid fusion in one slice. It
would move toward V1 search faster, but it widens migrations, Postgres extension
ops, ranking behavior, and security review before the provider workflow is
actually consumed by a worker.

### Recommended: Provider Memory Worker First

This is the smallest behavior slice that makes model-policy useful in the live
worker path. It keeps retrieval stable and sets up the next embedding/semantic
slice with real provider-call provenance already flowing through memory.

## Test Strategy

Use TDD in `apps/backend/engram/memory/memory_worker_tests.py`:

- provider-backed observation processing resolves policy, records one provider
  call, creates candidate/memory/version/retrieval document, and stores
  provenance;
- duplicate worker delivery does not create a second provider call;
- missing generation policy fails before writes;
- provider prompt/output redaction prevents token-shaped values from reaching
  candidate body, memory body, metadata, evidence, or provider call record.

Run adjacent model-policy, hook-ingest, context, feedback, Celery, repository,
and Compose verification after implementation.

## Security Notes

This slice touches provider-call trust boundaries and must get focused
independent security review before commit. The review must check secret
redaction, prompt retention, task payload secrecy, team/project policy
resolution, duplicate delivery side effects, and fail-clean behavior.
