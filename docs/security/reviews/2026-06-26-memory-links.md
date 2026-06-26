# Security Review: Memory Links

**Branch:** `feat/memory-links`
**Date:** 2026-06-26
**Reviewer:** implementation lead self-review (additive slice reusing proven
primitives; reuses the versioning/feedback lock, scope, audit, and redaction
helpers).

## Scope

- `apps/backend/engram/core/models.py` — `MemoryLink`, `LinkType`.
- `apps/backend/engram/core/migrations/0005_memorylink.py`.
- `apps/backend/engram/memory/services.py` — `RecordMemoryLink`, dataclasses.
- `apps/backend/engram/memory/views.py` — `MemoryLinksView`.
- `apps/backend/engram/memory/serializers.py` — `MemoryLinkSerializer`,
  `MemoryLinkQuerySerializer`.

## Checks

- **Authorization.** POST requires `memories:review`; GET requires
  `memories:read`. Both via `ResolveApiKeyScope`. Covered.
- **Tenant isolation.** `lock_memory_for_update` + `ensure_memory_team_scope`
  (shared helpers) enforce project + team scope; other-project memory returns
  `memory_not_found`; team-visible memory outside scope denied. GET filters by
  resolved organization/project. Covered.
- **Idempotency.** `MemoryLink.objects.get_or_create(memory, link_type, target)`
  plus the `core_memory_link_unique_target` constraint make replay return the
  existing link with `created=false`. Covered.
- **Redaction.** `target`/`label` redacted in response, audit, and list. Raw key
  never persisted/logged. Covered.
- **Audit.** `MemoryLinkRecorded` records actor, capability, link id, memory id,
  link_type, created flag, redacted target. Covered.
- **Input limits.** `target` ≤1024, `label` ≤255, `request_id`/`correlation_id`
  ≤255. Covered.

## Findings

None Critical or Important. The slice adds one read+write path over existing
authorization, locking, idempotency, and audit primitives.

## Accepted Risks

- Link targets are free-form strings; no validation that a `file`/`commit`
  target actually exists in a repository (deferred to link-driven retrieval and
  future repo integration).
- No link-driven retrieval ranking in this slice (deferred).

## Verdict

**SECURITY APPROVED.** Authorized, tenant-isolated, replay-protected, redacted,
audited read/write over proven primitives.
