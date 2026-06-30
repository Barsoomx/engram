# Security Review: console session scope resolver (`resolve_request_scope`)

- **Date:** 2026-06-30
- **Reviewer:** autonomous backend agent (session 630d61c4)
- **Trigger (per `goal.md` cadence):** auth/RBAC change — PR #28 introduced
  `access/request_scope.py`, accepting the console `Token` session on the bearer
  (inspection / model-policy / memory) endpoints.
- **Method:** differential review. An independent re-implementation of the same
  parity scope (branch `feat/console-backend-parity`, built before discovering #28
  was already merged) was used as an oracle to compare behavior against the merged
  `_session_scope`. Findings were reproduced with failing tests against
  `origin/master` before fixing.

## Scope reviewed

`apps/backend/engram/access/request_scope.py` (`_session_scope` path) and its
consumers (inspection, model-policy, memory views). Tenant isolation, capability
narrowing, audit parity, and the `EffectiveScope` contract relative to the bearer
(`ResolveApiKeyScope`) path.

## Commands run

- `pytest engram/access/request_scope_tests.py -q` (added 3 failing tests → reproduced both findings on master, then green after fix)
- `pytest -q` (full suite: 484 passed, 6 skipped — no regressions)
- `ruff check .` / `ruff format --check .` (clean)

## Findings

### F1 — Session scope is not narrowed to the requested project/team (Medium)

`_session_scope` checked `project_id in scope.project_ids` / `team_id in
scope.team_ids` but then returned the **full** org-wide `EffectiveScope` (every
project/team the user can reach), whereas the bearer path narrows the returned
scope to the single requested project (`project_ids=(project_id,)`). This breaks
the `EffectiveScope` contract every other caller relies on: any consumer that
trusts `scope.project_ids` to be the authorized-and-requested set (rather than
re-deriving from the explicit `project_id`) would over-return across the user's
other projects. Current consumers re-filter by the explicit `project_id`, so this
was latent rather than directly exploitable — but it is a real isolation-contract
divergence in security-critical code.

**Fix:** return a narrowed `EffectiveScope` — `project_ids=(project_id,)` when a
project is requested (else the full org set for org-wide reads), `team_ids=(team_id,)`
when a team is requested. Now identical to the bearer contract.

### F2 — Console session access is not audited (Medium)

The bearer path writes an `AccessScopeResolved` `AuditEvent` (actor_type
`api_key`) on every allow and deny. The session path wrote **no** audit event, so
console-session reads of memories / context-bundles / audit / secrets / policies
left no audit trail — breaking the audit-parity requirement in `goal.md`'s
security cadence (and weakening tamper-evidence and incident forensics for exactly
the human-driven access path).

**Fix:** `_session_scope` now writes an `AccessScopeResolved` `AuditEvent`
(actor_type `user`, result ALLOWED/DENIED, capability, target, `reason` +
`effective_capabilities` metadata) on both allow and deny, mirroring the bearer
audit. The `project` FK is only set when the access is allowed (so a cross-org
denied `project_id` cannot trip `AuditEvent.clean()`'s org-scope validation). The
emitted rows match the existing `AccessScopeResolved/audit_event/audit:read`
exclusion in the console audit log, so they do not pollute the audit feed.

## Regression tests added

`engram/access/request_scope_tests.py`:
- `test_session_scope_narrows_to_requested_project` (F1)
- `test_session_scope_writes_audit_on_allow` (F2)
- `test_session_scope_writes_audit_on_deny` (F2)

All three fail on `origin/master` and pass after the fix; the 12 pre-existing
session/bearer tests stay green.

## Accepted risk / follow-up (not in this slice)

- **`PurgeOrganizationMemoryView`** (added by #28) irreversibly deletes all
  org memories / candidates / context bundles behind a single `memories:admin`
  capability, with no confirmation token, soft-delete, or legal-hold. It is
  tenant-scoped and audited, but the blast radius is total. Recommend a follow-up:
  require an explicit confirmation token (e.g. the org slug in the body) and/or
  route it through the retention engine (roadmap Layer 1) rather than a direct
  bulk `DELETE`. Tracked as a recommendation, not fixed here (keeps this slice to
  the resolver).
