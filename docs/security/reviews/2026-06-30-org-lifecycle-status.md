# Security Review: organization lifecycle status + suspended-org enforcement

- **Date:** 2026-06-30
- **Reviewer:** autonomous backend agent (session 630d61c4)
- **Trigger (per `goal.md` cadence):** auth-boundary / RBAC change — new
  enforcement that denies access for non-active organizations across all auth
  realms.
- **Scope:** `Organization.status` field; `access/organization_access.py`
  enforcement helper; the three auth-boundary chokepoints (bearer
  `ResolveApiKeyScope`, session `_session_scope`, console
  `ActiveOrganizationPermission`); `engram_set_organization_status` operator
  command.

## Threat model

A suspended / pending-delete tenant must be locked out of all org-scoped
operations (the enforcement primitive a subscription/dunning layer flips). The
review checks that there is **no bypass path** and **no privilege-escalation**.

## Verification

- One enforcement helper (`organization_access_blocked`) is the single source of
  truth; `BLOCKED_STATUSES = {suspended, pending_delete}`.
- It is invoked at **every** realm's org-resolution point:
  - bearer: immediately after the API key is found (before capability/state checks);
  - session: immediately after the org is resolved from the header;
  - console: immediately after `resolve_active_organization`.
  There is no fourth path that resolves an org and skips the check (context-build
  goes through `ResolveApiKeyScope`; all inspection/model-policy/memory/admin
  surfaces go through one of the three).
- `past_due` is intentionally **not** blocked (grace window) — documented.
- Status is **operator-only** (a management command). There is no tenant-facing
  endpoint to change status, so a suspended tenant cannot un-suspend itself (no
  privilege escalation, no lock-out chicken-and-egg).
- Suspended-org members keep only org **visibility**: the org *list* endpoint uses
  `IsAuthenticated` (not `ActiveOrganizationPermission`), so a member can still see
  the org and its `status`, but every org-scoped operation is denied. Intended UX.
- The operator command writes an `OrganizationStatusChanged` `AuditEvent`
  (actor_type `system`, previous/new status), so suspensions are auditable.

## Tests (regression)

- `organization_access_tests.py` — default `active`; blocked matrix (5 statuses).
- `organization_enforcement_tests.py` — suspended → 403 `organization_suspended`
  on bearer + session; suspended → 403 on console; `pending_delete` → 403; `active`
  and `past_due` → 200.
- `engram_set_organization_status_tests.py` — sets status + audits; rejects unknown
  org / invalid status.
- Full suite: 496 passed, 6 skipped; `ruff check` / `format --check` clean;
  `makemigrations --check` clean.

## Findings

None. No bypass path or escalation identified. The `organization_suspended` code
is surfaced cleanly on the bearer/session realms (403 via `ACCESS_STATUS`); the
console realm returns a generic 403 (DRF permission), which is acceptable — the
frontend reads `status` from the org list to render a suspended state.

## Follow-up (out of scope, roadmap)

Stripe/Subscription wiring + dunning to flip the status automatically; async
soft-delete + purge for `pending_delete`; grace-window timer for `past_due` →
`suspended`.
