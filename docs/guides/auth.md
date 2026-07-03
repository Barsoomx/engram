# Auth Guide

Engram has two authentication surfaces:

1. **Admin/session surface** - human operators signing into the admin UI. Uses
   username + password exchanged for a DRF Token.
2. **Agent/runtime surface** - CLI, hooks, MCP bridge, and other machine clients.
   Uses a scoped Engram API key as a bearer token.

This guide covers the admin/session flow. For API keys, see
[api-keys.md](api-keys.md); for the scope model, see
[../rbac-and-scopes.md](../rbac-and-scopes.md).

## Login (username + password)

```bash
curl -X POST http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "you", "password": "<password>"}'
```

`POST /v1/auth/login` is unauthenticated (it creates the session). On success:

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

The frontend stores `token` in `localStorage` and uses it for all subsequent
admin requests as `Authorization: Token <drf-token>`.

## The `/me` endpoint

```bash
curl http://localhost:8000/v1/auth/me \
  -H "Authorization: Token <drf-token>"
```

Returns the authenticated user, identity, active organization, and resolved
capabilities. Use it to rehydrate a session (for example on page reload) and to
decide which UI affordances to show. Capabilities come from the user's role in
the active organization.

## Active organization header

Every admin request must carry the active organization id:

```
Authorization: Token <drf-token>
X-Engram-Organization: <org-id>
```

If the header is omitted and the user has exactly one active organization
membership, the server falls back to that organization silently; otherwise the
header is required and requests without it are rejected.

The `organization_id` returned by login is the oldest active organization
membership for that user. If you belong to multiple organizations, switch by
sending a different `X-Engram-Organization` header on subsequent requests; the
server resolves capabilities per active organization for each request.

## Logout

```bash
curl -X POST http://localhost:8000/v1/auth/logout \
  -H "Authorization: Token <drf-token>"
```

`POST /v1/auth/logout` invalidates the current DRF Token. The frontend clears
the local token and returns to the login page.

## Capability enforcement

Capabilities are resolved from the user's role in the active organization and
checked server-side on every admin action. Examples:

- `organizations:read` / `organizations:admin`
- `teams:read` / `teams:admin`
- `projects:read` / `projects:admin`
- `members:read` / `members:admin`
- `roles:read`
- `api_keys:read` / `api_keys:issue` / `api_keys:revoke`
- `audit:read`

Denied requests return a domain error body (`detail`, `error_code`, `code`);
the response does not include the missing capability name or a request id.
See [../api-reference.md](../api-reference.md) for the per-endpoint capability
matrix and [../rbac-and-scopes.md](../rbac-and-scopes.md) for the full model.

## Two surfaces, two headers

| Surface | Header                                              | Scope source                       |
|---------|-----------------------------------------------------|------------------------------------|
| Admin   | `Authorization: Token <drf-token>` + `X-Engram-Organization` | User role in active org |
| Runtime | `Authorization: Bearer <engram-api-key>`            | API key capabilities + project/team scope |

The `engram` CLI and hook adapter use the runtime surface (bearer key). The
admin UI uses the session surface (DRF Token). Do not mix the two on a single
request.

## OAuth / SSO

OAuth-based login (for example Google, GitHub, or an enterprise IdP) is **not**
implemented in Phase A. It is planned for a later release. This guide will be
updated when the OAuth flow lands. Until then, operators authenticate with a
username and password provisioned by an admin.

## See also

- [api-keys.md](api-keys.md)
- [admin-ui.md](admin-ui.md)
- [../api-reference.md](../api-reference.md)
- [../rbac-and-scopes.md](../rbac-and-scopes.md)
