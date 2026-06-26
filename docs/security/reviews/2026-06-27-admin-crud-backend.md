# Security Review — Phase A Admin CRUD Backend

- **Date:** 2026-06-27
- **Reviewer:** independent audit subagent (opus), reconciled by implementation lead
- **Scope:** `feat/admin-crud-backend` vs `master` — new `engram.console` app, `/v1/admin/` CRUD for organizations/teams/projects/members/roles/api-keys, RBAC capabilities + roles seed, API-key lifecycle, audit wiring, drf-spectacular schema.
- **Commands:** `git diff master...feat/admin-crud-backend`; `pytest -q` → 336 passed, 6 skipped (pre-existing).
- **Verdict:** **SHIP** — critical controls verified; two non-blocking accepted risks.

## Verified controls

1. **Tenant isolation** — every admin `get_queryset()` filters by `request.active_organization`; cross-org retrieve/update/delete → 404 (no existence oracle). Tests: `test_retrieve_returns_404_for_other_org_*`.
2. **RBAC** — every action gated by `RequireCapability` (wildcard-aware: `api_keys:*` grants `api_keys:read|issue|revoke`; no escalation path). Mutating actions need write caps, reads need `:read`.
3. **API-key secrecy (critical)** — raw key returned ONLY from `ApiKeyIssueResultSerializer` via context, never the model; DB stores HMAC-SHA256 hash + prefix + fingerprint only; revoke sets `revoked_at` and `ResolveApiKeyScope` rejects revoked keys (`revoked_key`); requested capabilities ⊆ issuer's effective (widening → 400).
4. **Org resolution** — `ActiveOrganizationPermission` validates active membership before use; Identity lookup scoped to resolved org; no targeting non-member orgs.
5. **Last-owner guard** — cannot remove/deactivate the last active `organization_owner` (409); within transaction.
6. **Audit integrity** — every mutation + denied attempts write `AuditEvent(actor_type='user', ...)` with non-empty metadata.
7. **Auth separation** — admin = DRF session token only; agent API-key auth not wired to `/v1/admin/`; anonymous/bare-key → 401.

## Findings

| Severity | Finding | Disposition |
|---|---|---|
| MEDIUM-1 | `RoleViewSet.get_queryset()` returns all roles (roles are global today); future per-org custom roles could leak. | **Accepted risk (v1).** Roles are built-in presets (global by design per `docs/rbac-and-scopes.md`); custom roles are "Later". Add org-scope when custom roles land. |
| MEDIUM-2 | Single-membership users can omit `X-Engram-Organization`; org resolved silently. | **Accepted risk (v1).** Membership is still validated; the Phase B frontend org-switcher always sends the header, so the fallback is dev-convenience only. |
| LOW-1 | `_organization_by_header` returns same response for "not found" vs "not a member" — intentional (no existence oracle). | **Confirmed control.** No fix. |
| LOW-2 | API-key issue serializer marks team/project `read_only` → scoped keys unreachable via API today. | **Accepted.** Dead-safe path; revisit when scoped keys are needed. |
| LOW-3 | `MemberWriteSerializer` hardcodes allowed role codes. | **Tech debt.** Fail-closed; align with Role source later. |
| INFO-1 | Last-owner guard lacks `select_for_update` (theoretical TOCTOU under concurrent admins). | **Accepted risk (v1).** Admin actions are rare; add row lock if multi-admin concurrency becomes real. |

## Fixes applied this review
None required for ship. All critical controls verified by tests with negative cases.
