# Admin UI Guide

The Engram admin UI is a Next.js app served at `http://localhost:3000/` by the
Compose `frontend` service. It authenticates against the Django + DRF backend
and exposes the operator surface of the product.

This guide reflects the Phase B UI as shipped. The full admin surface
(organizations, teams, members, roles) is also available via the admin API; see
[../api-reference.md](../api-reference.md).

## Sign in

1. Open `http://localhost:3000/`.
2. Sign in with a **username and password** for an identity that belongs to the
   target organization.
3. On success the frontend stores a DRF Token in `localStorage` and redirects
   to the dashboard.

The login call hits `POST /v1/auth/login`, which returns:

```json
{
  "token": "<drf-token>",
  "user_id": 1,
  "username": "you",
  "identity_id": "<uuid>",
  "organization_id": "<uuid>",
  "capabilities": ["organizations:read", "teams:admin", "..."]
}
```

See [auth.md](auth.md) for the full auth flow.

## Organization scope

The active organization is established at login from the authenticated user's
membership. Every admin request carries both the DRF Token (as
`Authorization: Token <key>`) and the active organization id (as
`X-Engram-Organization: <org-id>`). Capabilities are resolved from the user's
role in that organization.

If you belong to multiple organizations, sign in scoped to the one you intend to
operate on; the server authorizes each call against the active organization's
role.

## Sidebar

The left sidebar exposes seven destinations:

| Nav item      | Route        | Purpose                                              |
|---------------|--------------|------------------------------------------------------|
| Dashboard     | `/`          | Overview landing                                     |
| Memories      | `/memories`  | Browse and inspect memory items                      |
| Observations  | `/observations` | Browse raw observations                           |
| API Keys      | `/api-keys`  | Issue, list, revoke API keys (see [api-keys.md](api-keys.md)) |
| Projects      | `/projects`  | Browse projects in the active org                    |
| Audit         | `/audit`     | Audit log reader                                     |
| Health        | `/health`    | Service health dashboard                             |

A **Sign out** button at the bottom of the sidebar calls
`POST /v1/auth/logout` and clears the local token.

> **Capability-gated surface:** items shown in the sidebar correspond to the
> pages the UI renders. Capability enforcement itself happens server-side on
> every API call, so even if a route is reachable, the backend denies any
> action the caller's role does not grant. The capability model
> (`organizations:read`, `teams:admin`, `api_keys:issue`, etc.) is documented in
> [../rbac-and-scopes.md](../rbac-and-scopes.md).

## Pages

### Dashboard

Landing page after sign-in. Summarizes the active organization and links into
the operational pages.

### Memories

Browse memory items visible within the active scope, drill into a single memory
(`/memories/[id]`) to inspect its body, versions, links, and provenance.

### Observations

Browse raw observations that have been ingested via hooks or the API. Each row
shows the observation type and title.

### API Keys

Issue, list, and revoke API keys. The plaintext key is shown exactly once at
issue time. See [api-keys.md](api-keys.md).

### Projects

Browse projects in the active organization. Project CRUD (create/update/archive)
is performed via the admin API (`/v1/admin/projects/`).

### Audit

Read-only view of audit events (writes, admin actions, denials). Requires
`audit:read`.

### Health

Service health dashboard backed by `/-/healthz/` and related checks.

## What is API-only

The following admin resources are exposed via the API in Phase A/B but do not
have dedicated UI pages in Phase B. Manage them through the API or the
respective settings surface:

| Resource      | API path                    | Notes                              |
|---------------|-----------------------------|------------------------------------|
| Organizations | `/v1/admin/organizations/`  | Read + update settings             |
| Teams         | `/v1/admin/teams/`          | Full CRUD                          |
| Members       | `/v1/admin/members/`        | CRUD + last-owner guard            |
| Roles         | `/v1/admin/roles/`          | Read-only (seeded via migrations)  |

Refer to [../api-reference.md](../api-reference.md) for methods, capabilities,
and payloads.

## See also

- [auth.md](auth.md)
- [api-keys.md](api-keys.md)
- [../api-reference.md](../api-reference.md)
- [../rbac-and-scopes.md](../rbac-and-scopes.md)
- [../admin-ui-requirements.md](../admin-ui-requirements.md)
