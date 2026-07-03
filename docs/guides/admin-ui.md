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

The left sidebar is capability-gated and grouped into two sections, **Workspace**
and **Administration**. An item is shown only if the signed-in role grants the
item's capability.

**Workspace**

| Nav item        | Route              | Purpose                                              |
|-----------------|--------------------|-------------------------------------------------------|
| Dashboard       | `/`                | Overview landing                                     |
| Memories        | `/memories`        | Browse and inspect memory items                      |
| Observations    | `/observations`    | Browse raw observations                              |
| Memory Review   | `/memory-review`   | Review memory candidates                             |
| Projects        | `/projects`        | Browse and manage projects in the active org         |
| Search Debugger | `/search-debug`    | Debug retrieval/search queries                       |
| Hook Debugger   | `/hook-debug`      | Debug hook/observation ingestion                     |
| Context Bundles | `/context-bundles` | Browse packed context bundles                        |
| Weekly Digest   | `/digests`         | Browse weekly digest runs                            |
| Workflow Runs   | `/workflow-runs`   | Browse workflow run history                          |

**Administration**

| Nav item       | Route             | Purpose                                              |
|----------------|-------------------|-------------------------------------------------------|
| Secrets        | `/secrets`        | Manage provider secrets                              |
| Model Policies | `/model-policies` | Configure model routing policies                     |
| Model Setup    | `/model-setup`    | Model setup wizard                                   |
| Organizations  | `/organizations`  | Manage the active organization                       |
| Teams          | `/teams`          | Team CRUD                                            |
| Members        | `/members`        | Member CRUD and role assignment                      |
| Roles          | `/roles`          | Read-only role list                                  |
| API Keys       | `/api-keys`       | Issue, list, revoke API keys (see [api-keys.md](api-keys.md)) |
| Audit log      | `/audit`          | Audit log reader                                     |
| Settings       | `/settings`       | Application settings                                 |
| Health         | `/health`         | Service health dashboard                             |

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

Browse projects in the active organization. Project create, update, and archive
are performed directly in the UI (see [../api-reference.md](../api-reference.md)
for the underlying admin API).

### Audit

Read-only view of audit events (writes, admin actions, denials). Requires
`audit:read`.

### Health

Service health dashboard backed by `/-/healthz/` and related checks.

### Organizations

Manage the active organization: view and update organization settings.

### Teams

Full team CRUD: create, update, and archive teams in the active organization.

### Members

Manage organization members: invite, deactivate, and change member roles. A
last-owner guard prevents removing the final owner.

### Roles

Read-only view of the roles available in the active organization (roles are
seeded via migrations).

## See also

- [auth.md](auth.md)
- [api-keys.md](api-keys.md)
- [../api-reference.md](../api-reference.md)
- [../rbac-and-scopes.md](../rbac-and-scopes.md)
- [../admin-ui-requirements.md](../admin-ui-requirements.md)
