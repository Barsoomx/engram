# Engram API Reference

Engram exposes a REST API under two authentication surfaces:

- **Admin surface** (`/v1/admin/*`, `/v1/auth/*`) — session DRF Token auth
  (`Authorization: Token <key>`) plus an `X-Engram-Organization` header that
  selects the active organization scope. Each admin action additionally
  requires a capability granted to the caller's role.
- **Runtime / agent surface** (`/v1/hooks/*`, `/v1/context*`, `/v1/search*`,
  `/v1/memories/*`, `/v1/observations*`, `/v1/model-policy/*`,
  `/v1/inspection/*`) — agent API-key bearer auth
  (`Authorization: Bearer <engram-key>`). Capabilities on the API key scope
  gate the call.

This document is a quick map. For request/response bodies, enumerations, and
error codes, use the live OpenAPI schema.

## Browsable OpenAPI

drf-spectacular serves the schema and UIs at three URLs:

| URL                          | Format        | Purpose                                  |
|------------------------------|---------------|------------------------------------------|
| `GET /api/schema/`           | JSON (OAS 3)  | Raw OpenAPI schema for codegen/clients   |
| `GET /api/schema/swagger/`   | Swagger UI    | Interactive browser at `/api/schema/swagger/` |
| `GET /api/schema/redoc/`     | ReDoc         | Read-only browser at `/api/schema/redoc/` |

All three are unauthenticated (the schema is derived from the view classes, not
user data).

## Authentication summary

| Surface | Header                                              | Scope source                                   |
|---------|-----------------------------------------------------|------------------------------------------------|
| Admin   | `Authorization: Token <drf-token>` + `X-Engram-Organization: <org-id>` | User role in the active organization |
| Runtime | `Authorization: Bearer <engram-api-key>`            | API key capabilities + project/team scope      |

Admin capabilities (seeded roles: `organization_owner`, `organization_admin`,
`developer`, `auditor`) include:

- `organizations:read`, `organizations:admin`
- `teams:read`, `teams:admin`
- `projects:read`, `projects:admin`
- `members:read`, `members:admin`
- `roles:read`
- `api_keys:read`, `api_keys:issue`, `api_keys:revoke`
- `audit:read`

A `{group}:*` wildcard (for example `projects:*`) satisfies any capability in
that group. See `docs/rbac-and-scopes.md` for the full role/capability matrix.

## Auth

| Method | Path               | Auth           | Capability | Description                              |
|--------|--------------------|----------------|------------|------------------------------------------|
| POST   | `/v1/auth/login`   | none (creates session) | — | Exchange credentials for a DRF Token     |
| GET    | `/v1/auth/me`      | session Token  | —          | Return the authenticated user + scope    |
| POST   | `/v1/auth/logout`  | session Token  | —          | Invalidate the current DRF Token         |

## Admin CRUD

All admin endpoints require `Authorization: Token <key>` **and**
`X-Engram-Organization: <org-id>`.

### Organizations

| Method | Path                        | Capability           | Description                       |
|--------|-----------------------------|----------------------|-----------------------------------|
| GET    | `/v1/admin/organizations/`  | `organizations:read` | List orgs the caller belongs to   |
| GET    | `/v1/admin/organizations/{id}/` | `organizations:read` | Retrieve one organization     |
| PATCH  | `/v1/admin/organizations/{id}/` | `organizations:admin` | Update organization settings  |

### Teams

| Method | Path                  | Capability     | Description                          |
|--------|-----------------------|----------------|--------------------------------------|
| GET    | `/v1/admin/teams/`    | `teams:read`   | List teams in the active org         |
| POST   | `/v1/admin/teams/`    | `teams:admin`  | Create a team                        |
| GET    | `/v1/admin/teams/{id}/` | `teams:read` | Retrieve a team                      |
| PATCH  | `/v1/admin/teams/{id}/` | `teams:admin` | Update a team                        |
| DELETE | `/v1/admin/teams/{id}/` | `teams:admin` | Archive a team                       |

### Projects

| Method | Path                     | Capability        | Description                       |
|--------|--------------------------|-------------------|-----------------------------------|
| GET    | `/v1/admin/projects/`    | `projects:read`   | List projects in the active org   |
| POST   | `/v1/admin/projects/`    | `projects:admin`  | Create a project                  |
| GET    | `/v1/admin/projects/{id}/` | `projects:read` | Retrieve a project                |
| PATCH  | `/v1/admin/projects/{id}/` | `projects:admin` | Update a project                  |
| DELETE | `/v1/admin/projects/{id}/` | `projects:admin` | Archive/delete a project          |

### Members

| Method | Path                     | Capability       | Description                          |
|--------|--------------------------|------------------|--------------------------------------|
| GET    | `/v1/admin/members/`     | `members:read`   | List members in the active org       |
| POST   | `/v1/admin/members/`     | `members:admin`  | Invite/add a member                  |
| GET    | `/v1/admin/members/{id}/` | `members:read` | Retrieve a member                    |
| PATCH  | `/v1/admin/members/{id}/` | `members:admin` | Update a member's role/attributes    |
| DELETE | `/v1/admin/members/{id}/` | `members:admin` | Remove a member (last-owner guarded) |

### Roles

| Method | Path                 | Capability  | Description                            |
|--------|----------------------|-------------|----------------------------------------|
| GET    | `/v1/admin/roles/`   | `roles:read` | List roles + granted capabilities      |
| GET    | `/v1/admin/roles/{id}/` | `roles:read` | Retrieve a role with capabilities    |

Roles are read-only via this API (seeded via migrations).

### API Keys

| Method | Path                                  | Capability        | Description                                             |
|--------|---------------------------------------|-------------------|---------------------------------------------------------|
| GET    | `/v1/admin/api-keys/`                 | `api_keys:read`   | List API keys in the active org                         |
| POST   | `/v1/admin/api-keys/`                 | `api_keys:issue`  | Issue a new key (caller must grant its own capabilities)|
| GET    | `/v1/admin/api-keys/{id}/`            | `api_keys:read`   | Retrieve a key (no plaintext)                           |
| POST   | `/v1/admin/api-keys/{id}/revoke/`     | `api_keys:revoke` | Revoke a key                                            |

`POST /v1/admin/api-keys/` is the only endpoint that returns the plaintext
key (once, in the response body). Requested capabilities cannot widen beyond
what the issuer's own role grants.

## Runtime (agent API-key bearer)

All runtime endpoints require `Authorization: Bearer <engram-api-key>`. The
key's capabilities and project/team scope govern what may be ingested or read.

Hooks, context, search, observations, and memory endpoints all accept project
scope as either a `project_id` (UUID) or a `repository_url` (matched against
a project's canonicalized repository URL inside the caller's own
organization); `project_id` wins when both are present. Hooks, context, and
search may auto-create a project for an unmatched `repository_url` (org-wide
agent keys only); observations and memory endpoints are resolve-only and
return `404 project_not_found` instead. See
[backend-contracts.md](backend-contracts.md#project-routing-contract) for the
full resolver and authorization contract.

### Hooks

| Method | Path                              | Description                                       |
|--------|-----------------------------------|---------------------------------------------------|
| POST   | `/v1/hooks/post-tool-use`         | Ingest a PostToolUse hook event                   |
| POST   | `/v1/hooks/session-start`         | Ingest a SessionStart hook event                  |
| POST   | `/v1/hooks/error`                 | Ingest an Error hook event                        |
| POST   | `/v1/hooks/decision`              | Ingest a Decision hook event                      |
| POST   | `/v1/hooks/session-end`           | Ingest a SessionEnd hook event                    |
| POST   | `/v1/hooks/dry-run`               | Verify a key + resolve scope without persisting   |

`dry-run` does not require ingestion capability; it resolves the bearer's
actor and scope and returns server health. The five ingest hooks require a
capability appropriate to the event (typically an `observations:*` /
`hooks:*` grant on the key).

### Context

| Method | Path                              | Description                                       |
|--------|-----------------------------------|---------------------------------------------------|
| POST   | `/v1/context`                     | Build a task context bundle for the agent         |
| POST   | `/v1/context/session-start`       | Build a session-start context bundle              |

### Search

| Method | Path          | Description                                       |
|--------|---------------|---------------------------------------------------|
| POST   | `/v1/search/` | Semantic/full-text memory search within scope     |

### Memories

| Method | Path                                    | Description                                       |
|--------|-----------------------------------------|---------------------------------------------------|
| POST   | `/v1/memories/{memory_id}/feedback`     | Record agent/user feedback on a memory            |
| POST   | `/v1/memories/{memory_id}/version`      | Create or retrieve a memory version               |
| GET    | `/v1/memories/{memory_id}/links`        | List links for a memory                           |
| POST   | `/v1/memories/{memory_id}/links`        | Create a link between memories                    |

### Observations

| Method | Path                | Description                                       |
|--------|---------------------|---------------------------------------------------|
| GET    | `/v1/observations/` | List observations visible to the key's scope      |

### Model policy

Provider secrets and model policies are managed per organization; the bearer
key must carry `policy:admin` (or a matching admin grant) for write actions.

| Method | Path                                          | Description                                       |
|--------|-----------------------------------------------|---------------------------------------------------|
| GET    | `/v1/model-policy/secrets`                    | List provider secrets                             |
| POST   | `/v1/model-policy/secrets`                    | Register a provider secret                        |
| GET    | `/v1/model-policy/secrets/{secret_id}`        | Retrieve a secret metadata (value redacted)       |
| PATCH  | `/v1/model-policy/secrets/{secret_id}`        | Update a secret                                   |
| POST   | `/v1/model-policy/secrets/{secret_id}/rotate` | Rotate a secret's value                           |
| POST   | `/v1/model-policy/secrets/{secret_id}/disable`| Disable a secret                                  |
| GET    | `/v1/model-policy/policies`                   | List model routing policies                       |
| POST   | `/v1/model-policy/policies`                   | Create / update a policy                          |
| POST   | `/v1/model-policy/resolve`                    | Resolve the effective model + provider for a call |

### Inspection

Read-only inspection endpoints for debugging memory, context bundles, and
audit trails. The key must carry a read capability for the relevant resource.

| Method | Path                                                  | Description                                  |
|--------|-------------------------------------------------------|----------------------------------------------|
| GET    | `/v1/inspection/memories`                             | List memories within scope                   |
| GET    | `/v1/inspection/memories/{memory_id}`                 | Retrieve a memory                            |
| GET    | `/v1/inspection/context-bundles`                      | List context bundles within scope            |
| GET    | `/v1/inspection/context-bundles/{bundle_id}`          | Retrieve a context bundle                    |
| GET    | `/v1/inspection/audit-events`                         | List audit events (admin: `audit:read`)      |

## Health and metrics

| Method | Path            | Auth                          | Description                          |
|--------|-----------------|-------------------------------|--------------------------------------|
| GET    | `/-/healthz/`   | none                          | Liveness probe                       |
| GET    | `/-/readyz/`    | none                          | Readiness probe (DB/cache/queue)     |
| GET    | `/-/startup/`   | none                          | Startup probe                        |
| GET    | `/-/metrics`    | `Bearer <ENGRAM_METRICS_TOKEN>` (optional) | Prometheus metrics exposition |

The metrics endpoint is gated by `ENGRAM_METRICS_TOKEN` only when that env var
is set; otherwise it is open.

## Error responses

Runtime endpoints that fail key resolution return a JSON body:

```json
{ "code": "<error-code>", "detail": "<human-readable>" }
```

Common codes: `missing_api_key`, `invalid_key`, `inactive_key`,
`revoked_key`, `expired_key`, `inactive_owner`, `missing_capability`,
`project_scope_denied`, `team_scope_denied`, `project_not_found`,
`project_or_repository_required`.

Admin endpoints denied by capability return HTTP 403 and write an
`AccessDenied` audit event with `metadata.required_capability`.
