# Admin Inspection API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimal read-only admin inspection API for memories, context bundles, and audit events.

**Architecture:** Implement a small `engram.inspection` backend app with serializers, service helpers, views, URLs, and focused tests. Use existing API-key scope resolution and shared metadata redaction.

**Tech Stack:** Django, DRF, pytest, existing Engram access/core/context/memory models.

## Global Constraints

- Inspection API is read-only.
- Every endpoint requires `project_id`.
- Memory and context-bundle inspection require `memories:admin`.
- Audit inspection requires `audit:read`.
- Do not add frontend, MCP, semantic retrieval, provider calls, or memory mutation.
- Redact response metadata, content-bearing fields, scope evidence, and
  client-propagated request/audit identifiers with
  `engram.core.redaction.redact_value`.

---

### Task 1: Read-Only Inspection API

**Files:**
- Create: `apps/backend/engram/inspection/__init__.py`
- Create: `apps/backend/engram/inspection/apps.py`
- Create: `apps/backend/engram/inspection/serializers.py`
- Create: `apps/backend/engram/inspection/services.py`
- Create: `apps/backend/engram/inspection/views.py`
- Create: `apps/backend/engram/inspection/urls.py`
- Create: `apps/backend/engram/inspection/inspection_api_tests.py`
- Modify: `apps/backend/settings/settings.py`
- Modify: `apps/backend/settings/urls.py`
- Modify: `scripts/repository_layout.py`
- Modify: `docs/verification-matrix.md`
- Create: `docs/security/reviews/2026-06-25-admin-inspection-api.md`

**Interfaces:**
- Produces: `GET /v1/inspection/memories`
- Produces: `GET /v1/inspection/memories/<memory_id>`
- Produces: `GET /v1/inspection/context-bundles`
- Produces: `GET /v1/inspection/context-bundles/<bundle_id>`
- Produces: `GET /v1/inspection/audit-events`

- [x] Write failing tests for authorized memory list/detail with project/team filtering and redacted metadata.
- [x] Write failing tests for context-bundle list/detail with context items and cross-team denial.
- [x] Write failing tests for audit list requiring `audit:read`.
- [x] Run `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` and confirm missing routes/app fail.
- [x] Implement serializers, services, views, URLs, app registration, and repository layout entries.
- [x] Re-run focused tests until green.
- [x] Add security-fix regression tests for `memories:admin`, identifier redaction, and audit self-noise.
- [x] Run adjacent context/memory/access tests.
- [x] Record verification and security review evidence.
- [x] Commit with `feat: add admin inspection api`.
