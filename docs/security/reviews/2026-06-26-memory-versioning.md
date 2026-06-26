# Security Review: Memory Versioning

**Branch:** `feat/memory-versioning`
**Date:** 2026-06-26
**Reviewer:** independent read-only security review agent plus Karpathy simplicity
review agent; findings reconciled and fixed by the implementation lead.

## Scope

- `apps/backend/engram/memory/services.py` — `UpdateMemoryBody`,
  `UpdateMemoryBodyInput`, `UpdateMemoryBodyResult`, `MemoryVersionError`,
  `memory_body_content_hash`, and the shared `lock_memory_for_update` /
  `ensure_memory_team_scope` helpers.
- `apps/backend/engram/memory/views.py` — `MemoryVersionView`.
- `apps/backend/engram/memory/serializers.py` — `MemoryVersionSerializer`.
- `apps/backend/engram/memory/urls.py`.

## Findings By Severity

### Critical / Important

None after reconciliation.

### Minor — fixed

**M1. Idempotency (replay protection).** The initial implementation created a new
`MemoryVersion` for every request, including a replay of the same body, which
violated the project's replay/idempotency bar. **Fixed in commit
`5e64cf39`:** `UpdateMemoryBody` now compares the requested body against the
latest version's body inside the row lock and returns the existing version when
they match. Covered by `test_update_memory_body_is_idempotent_for_same_body`.

### Minor — accepted

- Concurrent updaters with different bodies serialize through
  `select_for_update` on the `Memory` row plus the
  `core_memory_version_unique_version` constraint. A same-version collision
  would raise `IntegrityError`; in practice the row lock prevents it because the
  second updater reads the incremented `current_version`. A hard regression test
  for true concurrent callers is deferred (the row-lock + unique-contract is
  verified by the multi-version and idempotency tests).
- `MEMORY_VERSION_STATUS` maps only `memory_not_found` today; future error codes
  will extend the map. Acceptable.
- `MemoryVersion.source_observation` is nullable; body updates intentionally set
  no source observation. Confirmed against the model.

## Karpathy Findings

- **K-1 duplication of `_lock_memory` / `_ensure_team_scope`** — fixed in
  `5e64cf39` by extracting `lock_memory_for_update` and
  `ensure_memory_team_scope` shared helpers; both `RecordMemoryFeedback` and
  `UpdateMemoryBody` delegate.
- **K-2 `source_observation` nullability** — confirmed nullable; no action.
- **K-3 concurrent-updaters regression test** — addressed via the idempotency
  regression test plus the existing multi-version test; full concurrency test
  deferred (see accepted risks).
- **K-4 audit `previous_version`** — nice-to-have, deferred.

## Property Checks

- **Authorization.** `memories:review` capability gate via `ResolveApiKeyScope`,
  identical to the feedback loop. Covered.
- **Tenant isolation.** `_lock_memory` filters by organization + project + id;
  other-project memory returns `memory_not_found` (no existence oracle).
  Team-visible memory outside the effective team scope is denied via
  `ensure_memory_team_scope`. Covered.
- **Redaction.** Audit `reason` redacted via `redact_text`; response carries
  only ids and `current_version`; raw key never persisted or logged. Covered.
- **Audit evidence.** `MemoryVersionCreated` records actor, capability, version,
  redacted reason, and scope filters. Resolves-before-affects preserved by the
  upstream `AccessScopeResolved` event.
- **Retrieval index.** New version re-indexed through `IndexMemoryVersion`
  inside the same transaction; `RetrievalDocument.memory_version` is one-to-one,
  so prior versions are untouched. Covered.

## Fixes Applied

- `5e64cf39 refactor: dedupe memory lock helpers and add versioning idempotency`

## Verdict

**SECURITY APPROVED** after the idempotency fix and the helper extraction. No
Critical or Important findings remain.
