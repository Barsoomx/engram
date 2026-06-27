# Phase A — Admin CRUD Backend (Design)

Date: 2026-06-26
Status: Design (autonomous, decisions delegated by owner)
Owner: implementation lead
References: `*****-backend` (backend RBAC/CRUD), `*****-admin` (frontend admin UI)

## Context

The Engram backend exposes 27 endpoints, all action/read-only for the runtime
agent. There is **no CRUD surface** for the administrative entities an admin UI
needs: organizations, teams, projects, members/memberships, roles, and API keys.
The models exist (`Organization`, `Team`, `Project`, `Identity`, `Role`,
`Capability`, `ApiKey`, memberships, `ProjectGrant`) but have zero views,
serializers, and routes.

Phase A builds the backend foundation that unblocks Phase B (frontend admin UI),
Phase C (onboarding wizard that creates these entities), and the specific owner
request "API keys for organizations in the panel".

The owner flagged that the local Engram docs and code "may be messy, not
reviewed". This design therefore grounds decisions in two reviewed reference
projects (`*****-backend`, `*****-admin`) rather than in existing Engram docs.

## Goal

A capability-gated, tenant-scoped REST CRUD surface for admin entities, under
session/token auth (username/password login, already implemented and verified),
with an immutable audit trail on every mutation. Built TDD-first.

## Non-Goals (Phase A)

- Frontend UI (Phase B).
- Onboarding wizard / plug-and-play bootstrap (Phase C).
- OpenAPI schema + user docs (Phase D) — though deps are added here.
- Custom-role editor (V1 roles are presets; a custom-role builder is "Later" in
  `docs/admin-ui-requirements.md`). Roles are read + assign, not created/deleted.
- Memory review, AI workflow runs, search debugger screens — separate slices.
- SMS/phone/OAuth login — owner explicitly wants username/password now, OAuth
  later.

## Architecture Decisions

### AD-1: Auth — reuse existing DRF TokenAuthentication

Keep the already-verified username/password → DRF `Token` flow
(`access/auth_views.py` `LoginView`/`MeView`). Admin endpoints use
`TokenAuthentication` + `IsAuthenticated`, same token the frontend already
stores.

- Alternative considered: switch to `next-auth` (*****-admin) or session auth.
  Rejected: current flow is verified (200/401 proven) and minimal; introducing
  next-auth adds a JWT layer with no benefit for a single-product admin.
- API keys (agent credentials) remain a **separate** auth path (`ResolveApiKeyScope`,
  bearer `egk_...`) and are NOT used for admin UI auth. Admin = human session
  token; agent = API key. This matches *****'s split (user session vs merchant
  HMAC) and *****'s (manager token vs none).

### AD-2: RBAC — DRF permission classes keyed on capabilities

The capability model already exists (`Capability`, `RoleCapability`,
`ApiKeyCapability`, `EffectiveScope.capabilities`). Add a reusable permission
class:

```python
class RequireCapability(BasePermission):
    def __init__(self, code): self.code = code
    def has_permission(self, request, view):
        scope = resolve_user_scope(request.user, request.active_organization)
        granted = scope.capabilities
        group = self.code.split(':')[0]
        if self.code not in granted and f'{group}:*' not in granted:
            return False
        request.effective_scope = scope
        return True
```

Capabilities are wildcard-aware: `api_keys:*` grants `api_keys:read`,
`api_keys:issue`, `api_keys:revoke`. Permission classes are attached per-ViewSet
action (via `get_permissions()`), so list/read need `*:read`, create/update need
the write capability, revoke is its own capability.

This mirrors *****'s `IsActiveWhitelabelManager` (permission class, stashes
resolved role on `request`) and *****'s `hasManagerCapability` (frontend gate).

- Alternative: service-layer checks. Rejected: DRF permission classes keep
  authorization visible in the view declaration and uniform with the runtime
  path.

### AD-3: Org/tenant resolution — header `X-Engram-Organization`, validated

Admin endpoints operate within one organization at a time (the org switcher in
Phase B selects it). Resolution order:

1. `X-Engram-Organization: <org_id|slug>` header (set by org switcher).
2. Fallback: the user's single membership if they belong to exactly one org.
3. Else: 400 `organization_required` / 403 if not a member.

Resolution is done in a thin DRF `initialize_request`/permission step that
validates the user is an **active member** of that org and stashes
`request.active_organization` + `request.effective_scope`.

This mirrors *****'s brand resolution (Origin > `X-Brand-Slug` > session) but
simpler: header > single-membership fallback. All `get_queryset()` filter by
`organization=request.active_organization`.

- Alternative: path-prefix `/v1/organizations/{org}/...`. Rejected for Phase A:
  more routing churn; header + membership-validation is equivalent and lets the
  org switcher live entirely client-side.

### AD-4: Routing — DRF `DefaultRouter` + `ModelViewSet` (per resource)

Switch from the current ad-hoc `APIView`-per-action style to `ModelViewSet` +
`Router` for the admin surface only. RESTful, compact, OpenAPI-friendly, and
matches the ***** `manager_api/v1/` shape (***** uses function paths; router is
cleaner for Sentry-style resources).

Mounted under `/v1/admin/`:

```
/v1/admin/organizations/            # current-org settings (read/update) + list
/v1/admin/teams/
/v1/admin/teams/{id}/
/v1/admin/projects/
/v1/admin/projects/{id}/
/v1/admin/members/                  # org memberships + identities
/v1/admin/members/{id}/
/v1/admin/roles/                    # list built-in roles + capabilities (read)
/v1/admin/api-keys/
/v1/admin/api-keys/{id}/
/v1/admin/api-keys/{id}/revoke/
```

Each ViewSet: capability-gated `permission_classes`/`get_permissions()`,
`get_queryset()` scoped to `request.active_organization`, `pagination_class`,
`filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]`.

### AD-5: Serializers — read/write split

Separate input (write) and output (read) serializers via `get_serializer_class()`
(***** pattern). Write serializers validate constraints and call a service;
read serializers never leak secrets. For API keys: read serializer returns
`name`, `key_prefix`, `key_fingerprint`, `created_at`, `expires_at`,
`last_used_at`, `active`, `revoked_at`, `capabilities` — **never** the raw key.
The create response returns the raw key **exactly once** in a dedicated
`ApiKeyIssueResult` serializer.

### AD-6: Audit — reuse `AuditEvent`, append-only

Every admin mutation writes an `AuditEvent` (the model and `_audit` helper
already exist) with `actor_type='user'`, `actor_id=<identity>`,
`target_type`/`target_id`, `capability`, `result`, and a `metadata` JSON bag of
the change. This matches *****'s immutable `WhitelabelBrandAuditLog` +
`write_brand_audit_log` helper, adapted to Engram's existing `AuditEvent`.

## Capability & Role Model

New capability codes (seeded via data migration), grouped per
`docs/rbac-and-scopes.md`:

- `organizations:read`, `organizations:admin`
- `teams:read`, `teams:admin`
- `projects:read`, `projects:admin`
- `members:read`, `members:admin`
- `roles:read`
- `api_keys:read`, `api_keys:issue`, `api_keys:revoke`

Built-in roles (already exist as `organization_owner`, `organization_admin`,
plus `developer`, `auditor`) get capabilities seeded:

- `organization_owner`: every `*:admin` + `*:read`.
- `organization_admin`: `teams:admin`, `projects:admin`, `members:admin`,
  `api_keys:*`, `roles:read`, and all `*:read`.
- `developer`: `projects:read`, `teams:read`, `api_keys:read` (own),
  `memories:*`, `observations:*`, `search:query`.
- `auditor`: all `*:read` + `audit:read`, no writes.

Capabilities are wildcard-expanded in the permission check (`api_keys:*` covers
`api_keys:read|issue|revoke`).

## Resource API Surface (Phase A)

### Organizations
- `GET /v1/admin/organizations/` — orgs the user is a member of (`organizations:read`).
- `GET /v1/admin/organizations/{id}/` — detail.
- `PATCH /v1/admin/organizations/{id}/` — update name/settings (`organizations:admin`).
- Create/delete of organizations is out of Phase A scope (org = tenant; tenant
  provisioning is a bootstrap/ops concern, Phase C). List + settings only.

### Teams
- `GET/POST /v1/admin/teams/`, `GET/PATCH/DELETE /v1/admin/teams/{id}/`.
- Create/update requires `teams:admin`. Delete = archive (soft) to preserve FK
  integrity.

### Projects
- `GET/POST /v1/admin/projects/`, `GET/PATCH/DELETE /v1/admin/projects/{id}/`.
- Fields: name, slug, repository_url, default_branch. `projects:admin` for writes.

### Members
- `GET/POST /v1/admin/members/` — list identities + their org membership/role;
  invite = create `Identity` (user) + `OrganizationMembership`.
- `PATCH /v1/admin/members/{id}/` — change role / active.
- `DELETE /v1/admin/members/{id}/` — deactivate membership (soft).
- `members:admin` for writes. Prevent removing the last owner.

### Roles
- `GET /v1/admin/roles/` — list roles + their capabilities (`roles:read`).
- Read-only in Phase A (built-in presets).

### API Keys
- `GET /v1/admin/api-keys/` — list keys in active org (`api_keys:read`); columns:
  name, prefix, fingerprint, owner, capabilities, created, expires, last_used,
  active, revoked.
- `POST /v1/admin/api-keys/` — issue (`api_keys:issue`); generates `egk_...`,
  stores HMAC hash + fingerprint (reuse `hash_api_key`/`api_key_fingerprint`),
  returns plaintext **once**.
- `GET /v1/admin/api-keys/{id}/` — detail (no plaintext).
- `POST /v1/admin/api-keys/{id}/revoke/` — set `revoked_at` (`api_keys:revoke`).
- Capabilities on a key are a subset of the owner's effective capabilities
  (never widen). Validates `ApiKey.clean()` org/team/project scope.

## API Key Lifecycle & Security

- Raw key format `egk_<32 url-safe chars>` (matches existing prefix convention).
- Stored: `key_prefix` (first 12), `key_hash` (HMAC-SHA256 over SECRET_KEY),
  `key_fingerprint` (`prefix...digest[-12:]`). Plaintext never persisted.
- Lookup at auth time by prefix + constant-time hash compare (already in
  `ResolveApiKeyScope._find_key`).
- Issue response returns `{id, name, key_prefix, key_fingerprint, plaintext,
  capabilities, created_at}` — `plaintext` only here, never again.
- Revoke sets `revoked_at`; key cannot be used after (already enforced in
  `_state_error`).
- Phase A does NOT add Fernet at-rest encryption (***** pattern) — the existing
  HMAC-hash scheme is sound because the raw key is unrecoverable. Encryption is
  only needed if we ever need to recover/rotate plaintext, which we don't.

## Audit Events

Per mutation, write `AuditEvent` with a typed `event_type`, e.g.
`OrganizationUpdated`, `TeamCreated`, `ApiKeyIssued`, `ApiKeyRevoked`,
`MemberRoleChanged`, `MemberRemoved`. `result=ALLOWED`/`DENIED`, `metadata`
carries before/after diff (secrets/redacted). Denied capability attempts are
also audited (403 path).

## Migrations & Seed

- Data migration: add the new `Capability` rows, seed `RoleCapability` for the
  four built-in roles.
- Idempotent (use `get_or_create`).
- Bootstrap (Phase C) will create the first org + owner; Phase A only needs the
  capability/role seed and that existing dev data keeps working.

## Dependencies To Add

From the references, add to backend `pyproject.toml`:

- `django-filter` — `DjangoFilterBackend` + `FilterSet` for list filtering.
- `drf-spectacular` — OpenAPI schema (unblocks Phase D; install now, wire later).

`djangorestframework` is already present. `factory-boy` is already in dev deps.
No frontend deps change in Phase A.

pnpm/npm age gate and "no broad auto-fix" rules from `CLAUDE.md` apply; pin via
the existing lockfile flow.

## TDD Plan

Per resource, red→green:

1. Capability-gating tests: each endpoint returns 403 without the capability,
   200 with it (mock auth, per project rule #21 view tests use mocks not stubs).
2. Tenant isolation: user in org A cannot read/write org B's resources.
3. CRUD tests: create/read/update/archive happy paths + validation errors.
4. API key tests: issue returns plaintext once; list/detail never leak; revoke
   blocks auth; key capabilities cannot exceed owner's.
5. Audit tests: each mutation writes the expected `AuditEvent` with correct
   actor/target/metadata; secret redaction.
6. Last-owner protection: cannot remove/deactivate the last organization owner.

Tests run in Docker (`docker compose exec app pytest ...`), per project rules.

## Security Considerations (Phase A focus)

- Tenant isolation: every `get_queryset` filters by `request.active_organization`;
   cross-org access returns 404 (not 403, to avoid leaking existence) where
   feasible.
- Capability checks on every mutating action; denied attempts audited.
- API key plaintext never persisted or re-exposed.
- Last-owner / self-demotion guards on memberships.
- Audit integrity: append-only, actor always recorded.
- A security review pass (per `CLAUDE.md` cadence: RBAC/API-key change) precedes
   merge of Phase A.

## Open Questions / Risks

- **Org switcher contract**: header `X-Engram-Organization` is the chosen
  transport; confirm in Phase B that the frontend can set it per-call via the
  axios client (it can).
- **Identities vs Django Users**: `LoginUser` creates an `Identity` from a Django
  `User`. Members CRUD creates `Identity` rows (user type). Whether to also
  provision a Django `User` login (so a member can log in) is a Phase C
  onboarding concern; Phase A manages `Identity` + membership, not credentials.
- **Soft vs hard delete**: archive (soft) chosen for teams/projects/memberships
  to preserve FK integrity and audit history.

## Next Step

Invoke the `writing-plans` skill to turn this design into an ordered, per-resource
implementation plan with TDD steps and verify commands. Phase A will be delivered
as several small checkpoint commits (one per resource group: capabilities+seed →
organizations → teams → projects → members → roles → api-keys → audit polish),
each behind a feature branch and a draft MR.
