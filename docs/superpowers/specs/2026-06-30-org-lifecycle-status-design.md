# Design: Organization lifecycle status + suspended-org enforcement

> Net-new roadmap slice (`engram-saas-roadmap.md` Layer 2 P0 / Спринт A #1 —
> "примитив энфорсмента подписки"). Base: `origin/master` @ `733ac8a3` (after the
> backend-parity PR #28). Branch: `feat/org-lifecycle-status`. Independent of the
> merged parity work and of the security-hardening PR #29.

## 1. Why

`Organization` is `name` + `slug` only — it cannot express "suspended". There is
**zero** subscription-enforcement primitive: a non-paying / abusive tenant cannot
be locked out. This is the first thing a billing layer needs ("взять деньги
безопасно"). This slice adds the lifecycle status and enforces it at the auth
boundary, so later billing/dunning code only has to flip a field.

## 2. Scope

**IN:**
- `Organization.status` lifecycle field (`active | trialing | past_due |
  suspended | pending_delete`, default `active`) + migration.
- Auth-boundary enforcement: `suspended` and `pending_delete` orgs are denied
  (403 `organization_suspended`) across **all three realms** — console session,
  bearer API-key, and the unified session path. `active`/`trialing`/`past_due`
  are allowed (`past_due` = grace window before suspension).
- Operator tool: `engram_set_organization_status <slug> <status>` management
  command (audited) — status is operator-driven, not tenant self-service (avoids
  the lock-out chicken-and-egg).

**OUT (later roadmap):** Stripe/Subscription wiring, dunning, async soft-delete +
purge for `pending_delete`, self-serve signup, grace-window timers. This slice is
the field + the gate only.

## 3. Design

### 3.1 Model
`core/models.py`: `class OrganizationStatus(models.TextChoices)` with the five
values; `Organization.status = CharField(max_length=20,
choices=OrganizationStatus.choices, default=OrganizationStatus.ACTIVE)`. Migration
backfills existing rows to `active` (safe default).

### 3.2 Enforcement helper
`engram/access/organization_access.py`:
```python
BLOCKED_STATUSES = frozenset({OrganizationStatus.SUSPENDED, OrganizationStatus.PENDING_DELETE})

def organization_access_blocked(organization) -> bool:
    return organization.status in BLOCKED_STATUSES
```
One source of truth for "which statuses lock out". (`past_due` deliberately NOT
blocked — grace.)

### 3.3 Chokepoints (one call each, after the org is resolved)
- **Bearer** — `ResolveApiKeyScope.execute` (`access/services.py`): right after the
  key is found, `if organization_access_blocked(key.organization): raise
  AccessDeniedError('organization_suspended', ...)`.
- **Session** — `_session_scope` (`access/request_scope.py`): right after
  `_organization_by_header`, same raise. (Distinct hunk from PR #29's changes — no
  real conflict.)
- **Console** — `ActiveOrganizationPermission.has_permission`
  (`console/org_resolution.py`): after `resolve_active_organization`, `if
  organization_access_blocked(organization): return False` (→ 403).
- `ACCESS_STATUS` (`context/views.py`) gains `'organization_suspended': 403`.

Note: the org **list** endpoint uses `IsAuthenticated` only (not
`ActiveOrganizationPermission`), so a member of a suspended org can still list
their orgs and see the `status` — the gate blocks org-scoped operations, not
visibility. That is the intended UX (frontend shows a "suspended" state).

### 3.4 Operator command
`core/management/commands/engram_set_organization_status.py`: positional `slug`
and `status`; validates the status against the choices; sets and saves; writes an
`AuditEvent(event_type='OrganizationStatusChanged', actor_type='system',
result=RECORDED, metadata={previous, new})`. Mirrors the existing
`engram_bootstrap_admin` command + test pattern.

## 4. Testing (TDD, sqlite harness)
- model default = `active`; migration `makemigrations --check` clean.
- `organization_access_blocked`: suspended/pending_delete → True; active/trialing/past_due → False.
- enforcement: suspended org → 403 on a console endpoint, a bearer endpoint, and a session endpoint; active org unaffected (regression).
- command: sets status, rejects an invalid status, writes the audit event.
- full suite green; ruff clean.

## 5. Security review (auth boundary → mandatory)
- The gate is applied at every realm's org-resolution point (no bypass path).
- `past_due` intentionally not blocked — documented (grace).
- Status is operator-only (management command); no tenant self-service path can
  un-suspend (no privilege-escalation).
- Suspended-org members retain only org *visibility* (list), not org-scoped access.

## 6. Commit structure (one MR)
1. model field + migration + `organization_access_blocked` helper + tests.
2. wire the three chokepoints + ACCESS_STATUS + enforcement tests.
3. operator management command + test.
4. security review artifact + MR description.
