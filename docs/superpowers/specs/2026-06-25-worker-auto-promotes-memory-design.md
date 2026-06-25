# Worker Auto-Promotes Memory Design

## Decision

The package-relayed observation worker must produce approved, searchable memory
for the first parity loop without requiring `scripts/e2e_golden_path.py` to call
the manual `engram_promote_memory_candidate` command.

Hook ingest still owns only event normalization and `django-celery-outbox`
enqueueing. It persists the hook domain rows, calls
`process_observation_recorded.delay(str(observation.id))` in the same database
transaction, and lets the package relay deliver the task. The worker then reloads
the `Observation` by id, creates or reuses a `MemoryCandidate`, promotes that
candidate idempotently to approved `Memory`, writes `MemoryVersion`, and indexes
the `RetrievalDocument`.

The manual promotion command stays available for later curation/admin workflows,
but the Compose parity golden path must not depend on it.

## Alternatives Considered

1. Keep manual promotion in the golden path. This preserves the current code but
   leaves the hard-gate proof weak because the worker creates only a candidate,
   not useful injected memory.
2. Build a separate curation queue before promotion. This is closer to the
   long-term memory-quality workflow but expands the checkpoint into provider,
   UI, or human-review work.
3. Auto-promote deterministic V1 candidates in the existing worker. This is the
   smallest change that proves the rewritten loop end to end while keeping the
   future curation surface explicit.

Use option 3 for this checkpoint.

## Scope

This checkpoint changes only the backend worker parity loop and its evidence:

- `ProcessObservationRecorded` returns the promoted memory version and retrieval
  document as part of the worker result;
- duplicate task delivery remains idempotent for candidate, memory, version, and
  retrieval document creation;
- the Celery task returns the approved memory id, not the candidate id;
- the existing `PromoteMemoryCandidate` service remains the single promotion
  implementation;
- `scripts/e2e_golden_path.py` waits for worker-created retrieval state instead
  of shelling into `engram_promote_memory_candidate`;
- E2E verifies persisted `ContextBundleItem` and `MemoryRetrieved` audit
  evidence after future session context injection;
- repository contract tests reject reintroducing manual promotion into the
  Compose golden path.

## Non-Goals

- Do not add semantic/vector retrieval.
- Do not add provider/model-policy calls.
- Do not add frontend, MCP, or Claude Code plugin work.
- Do not remove the manual promotion command.
- Do not build the broader memory curation queue, review UI, daily digest, or
  stale/refuted supersession workflow.

## Worker Behavior

`ProcessObservationRecorded.execute()` must run one database transaction for
observation lock, candidate creation/reuse, candidate promotion, version write,
and retrieval indexing.

For a new observation:

1. lock `Observation` with `select_for_update(of=('self',))`;
2. create a proposed `MemoryCandidate` with redacted title, body, and evidence;
3. promote the candidate with `PromoteMemoryCandidate`;
4. mark the candidate `PROMOTED`;
5. return candidate, memory, memory version, retrieval document, and
   `duplicate=False`.

For duplicate delivery:

1. reuse the existing candidate by project/content hash;
2. call `PromoteMemoryCandidate` for the reused candidate;
3. return the existing memory/version/retrieval document and `duplicate=True`;
4. create no duplicate durable rows.

`process_observation_recorded(observation_id)` returns the approved memory id as
a string. Malformed and missing observation ids keep the existing redacted
`MemoryWorkerError` behavior.

## E2E Behavior

The Compose golden path should:

1. start Compose services;
2. bootstrap organization/project/team/API key;
3. run `engram connect`;
4. submit a Codex `post-tool-use` hook;
5. wait for `RetrievalDocument` state created by the relayed worker;
6. request a future `session-start` context;
7. verify the context response contains the expected memory;
8. verify database evidence for a persisted `ContextBundleItem` and
   `MemoryRetrieved` audit event.

No step may call `engram_promote_memory_candidate`.

## Security And Data Boundaries

- Task payloads remain id-only.
- Candidate evidence, memory body/title, retrieval text, E2E output, and audit
  metadata must not contain generated API keys or bearer-token shapes.
- Authorization remains enforced by existing hook/context API paths.
- The worker reloads tenant/project scope from the locked `Observation`; it must
  not trust tenant/project data from Celery task arguments.

## Verification

Required verification:

- focused worker tests prove auto-promotion, duplicate delivery idempotency,
  task return value, and secret redaction;
- repository tests prove the golden path no longer calls manual promotion;
- backend test suite passes;
- migration freshness passes;
- Compose golden path passes and verifies worker-created retrieval plus context
  audit evidence;
- repository quality checks pass.
