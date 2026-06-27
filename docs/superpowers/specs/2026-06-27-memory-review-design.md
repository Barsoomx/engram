# Slice 1 — Memory Review (Design)

Date: 2026-06-27
Status: Design (autonomous)

## Context

`docs/admin-ui-requirements.md` wants an AI-curated memory review queue (not the raw observation firehose): filters by team/project/scope/confidence/conflict/age/source; old/new diff; provenance+citations; approve/edit/narrow/reject/archive/supersede; bulk archive of low-confidence noise.

Models already exist (`engram/core/models.py`): `MemoryCandidate` (status PROPOSED/PROMOTED/REJECTED, confidence, evidence, visibility_scope, source_observation, promoted_memory), `Memory` (status APPROVED/ARCHIVED/REFUTED/CONFLICT, confidence, stale), `MemoryVersion`, `MemoryLink` (link_type enum), `Observation`+`ObservationSource` (provenance). Existing services: `PromoteMemoryCandidate`, `UpdateMemoryBody`, `RecordMemoryFeedback` (stale/refuted only), `RecordMemoryLink`. Existing endpoints: `/v1/memories/{id}/{feedback,version,links}` (agent API-key), `/v1/inspection/memories` (read-only, no filters).

Gap: no curated queue with filters, no diff endpoint, actions limited to stale/refuted (no archive/narrow/supersede/reject-for-candidate), no bulk archive.

## Goal

A capability-gated admin memory-review surface under `/v1/admin/memory-review/` (admin session auth, `X-Engram-Organization`, consistent with Phase A `console/`): queue with filters, version diff, full curation actions, bulk archive — plus a frontend page.

## Architecture Decisions

### AD-1: Extend `console/` (admin session auth)
New `MemoryReviewViewSet` in `engram/console/views/memory_review.py`, mounted at `/v1/admin/memory-review/`. Uses `ActiveOrganizationPermission` + `RequireCapability`. Tenant-scoped (`request.active_organization`). Audit via existing `audit_admin_action`.

### AD-2: Queue endpoint
`GET /v1/admin/memory-review/` — paginated list of reviewable items: PROPOSED candidates + low-confidence/conflict/REFUTED memories in the active org/project scope. Query filters: `team_id`, `project_id`, `visibility_scope`, `confidence__gte`/`confidence__lte`, `status` (proposed/conflict/refuted), `age_days__gte` (created_at cutoff), `source_type`. Serializer returns: id, type (candidate/memory), title, body, status, confidence, visibility_scope, evidence (provenance: provider_call_id, provider, model), source_observation summary (files_read/modified, tool), citations (memory links). Capability `memories:review`.

### AD-3: Diff endpoint
`GET /v1/admin/memory-review/{memory_id}/diff/?from_version=&to_version=` — returns `{from: {version, body, created_at}, to: {version, body, created_at}}` from `MemoryVersion`. Client renders the diff. Capability `memories:review`.

### AD-4: Action endpoint
`POST /v1/admin/memory-review/{id}/action/` body `{action, reason, ...}` where action ∈ {approve, edit, narrow, reject, archive, supersede}. Maps to services:
- `approve` → `PromoteMemoryCandidate` (candidate→memory).
- `edit` → `UpdateMemoryBody` (new version; optional `body` in payload).
- `narrow` → `RecordMemoryLink(link_type='narrowed_by', target_id, label)` (payload `target_memory_id`).
- `supersede` → `RecordMemoryLink(link_type='superseded_by', target_id)` + mark old memory stale.
- `reject` → set `MemoryCandidate.status=REJECTED` (candidate) or `Memory.status=REFUTED` (memory).
- `archive` → `Memory.status=ARCHIVED`.
Each writes an `AuditEvent` (`MemoryReviewed`, target=memory/candidate, metadata action+reason). Capability `memories:admin` (writes), `memories:review` (read).

### AD-5: Bulk archive
`POST /v1/admin/memory-review/bulk-archive/` body `{ids: [...], reason}` OR `{confidence__lte, reason}` (selects by threshold). Archives each (status=ARCHIVED), audits each with reason. Capability `memories:admin`. Returns count + archived ids. Per `admin-ui-requirements.md:122` destructive actions need preview+reason — reason is required; preview = client confirms before call.

### AD-6: No model migration needed
archive uses existing `Memory.status=ARCHIVED`; reject uses `CandidateStatus.REJECTED` / `Memory.status=REFUTED`; narrow/supersede use existing `MemoryLink.link_type`. Confirm `link_type` choices include `narrowed_by`/`superseded_by` — if not, extend the enum via migration (small).

## Frontend

- `apps/frontend/src/app/(admin)/memory-review/page.tsx` — `CapabilityGate(memories:review)` + `PageHeader` + filter bar (team/scope/confidence range/status/age/source) + queue table (title, type, status, confidence chip, source, age) with row-select checkboxes + action menu per row + bulk-archive bar. Diff drawer (modal) showing from/to versions (lightweight inline diff or side-by-side).
- `lib/admin-api.ts` — `listMemoryReview(params)`, `memoryReviewDiff(id, from, to)`, `memoryReviewAction(id, payload)`, `bulkArchiveMemoryReview(payload)`.
- `hooks/use-memory-review.ts` — query + mutations (invalidate queue).
- Sidebar: add "Memory Review" item (`memories:review`).

## Testing

- Backend (Docker pytest): queue filters (scope/confidence/status/age/source), diff, each action transition (approve/edit/narrow/supersede/reject/archive), bulk-archive by ids + by threshold, audit events written, capability gating, tenant isolation.
- Frontend: `pnpm typecheck && pnpm build`.

## Next

`writing-plans` → tasks S1.0 (backend ViewSet+service+tests), S1.1 (frontend page+api+build).
