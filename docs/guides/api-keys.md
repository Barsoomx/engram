# API Keys Guide

API keys authenticate the agent/runtime surface of Engram: hooks, context
retrieval, search, memory mutations, and the MCP bridge. They carry a subset
of the issuing identity's capabilities, and are managed from the admin UI or
the admin API.

This guide reflects Phase A behavior. For the endpoint contract, see
[../api-reference.md](../api-reference.md); for the scope model, see
[../rbac-and-scopes.md](../rbac-and-scopes.md).

## Shape and validation

- The server does not enforce a specific key prefix, but the `egk_` prefix
  used by server-issued keys is strongly recommended for readability and
  audit. Use a long, random string.
- The server stores only a hash (`key_hash`), a prefix (`key_prefix` - the first
  12 characters of the raw key, for display/lookup), and a short
  `key_fingerprint`.
- The raw key is returned exactly once, at issue time, in the response body of
  `POST /v1/admin/api-keys/`. It is never retrievable again.

## Issue a key

From the admin UI (`/api-keys`) or via the API:

```bash
curl -X POST http://localhost:8000/v1/admin/api-keys/ \
  -H "Authorization: Token <drf-token>" \
  -H "X-Engram-Organization: <org-id>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Claude Code agent",
    "capabilities": ["memories:read", "observations:write"]
  }'
```

| Field          | Required | Description                                                 |
|----------------|----------|-------------------------------------------------------------|
| `name`         | yes      | Human label                                                 |
| `capabilities` | yes      | Subset of the issuer's capabilities (see below)             |

The requested `capabilities` **cannot widen** beyond what the issuing user's
own role grants; the server rejects the whole request (no key is created) if
any requested capability isn't covered by the issuer's own capabilities or a
`{group}:*`/`policy:admin` wildcard.

The response contains the plaintext `key` plus its `id`, `name`, `key_prefix`,
`key_fingerprint`, `capabilities`, and `created_at`. Store the plaintext
immediately.

The golden-path bootstrap (`engram_bootstrap_golden_path`) creates exactly such
a key deterministically, with capabilities `memories:read` and
`observations:write`, scoped to the bootstrapped team and project. See
[../quickstart.md](../quickstart.md).

## List keys

```bash
curl http://localhost:8000/v1/admin/api-keys/ \
  -H "Authorization: Token <drf-token>" \
  -H "X-Engram-Organization: <org-id>"
```

Requires `api_keys:read`. Returns id, name, prefix, fingerprint, owner
identity, capabilities, created/expires/last-used timestamps, active status,
and revoked_at. No plaintext.

## Revoke a key

```bash
curl -X POST http://localhost:8000/v1/admin/api-keys/<id>/revoke/ \
  -H "Authorization: Token <drf-token>" \
  -H "X-Engram-Organization: <org-id>"
```

Requires `api_keys:revoke`. Sets `revoked_at`; subsequent bearer calls fail
with `revoked_key` (HTTP 403).

## Capabilities

Available capability groups include (see [../rbac-and-scopes.md](../rbac-and-scopes.md)
for the full list):

- `memories:read`, `memories:propose`, `memories:review`, `memories:admin`
- `observations:read`, `observations:write`
- `search:query`
- `teams:*`, `projects:*`, `members:*`, `api_keys:*`, `secrets:*`,
  `model_policy:*`, `policy:admin`, `audit:read`

A `{group}:*` wildcard satisfies any capability in that group. The golden-path
key uses a minimal pair: `memories:read` + `observations:write`, which is enough
for hook ingest and memory search.

## Using a key

Agent/runtime endpoints expect the key as a bearer token:

```
Authorization: Bearer egk_...
```

This is distinct from the admin surface, which uses a DRF session Token
(`Authorization: Token <drf-token>`). The `engram` CLI and the hook adapter use
the bearer form under the hood after `engram connect`.

## Security notes

- **Plaintext once.** Save the issued key immediately. The server cannot recover
  it.
- **Scope narrowly.** Prefer `memories:read` + `observations:write` for agents;
  reserve `memories:admin`, `policy:admin`, and `api_keys:*` for operator keys.
- **One key per agent/project.** Avoid sharing a key across runtimes or
  projects; it makes rotation and audit harder.
- **Rotate regularly.** Issue a replacement, update `engram connect`, then
  revoke the old key.
- **Revoke on compromise.** Revocation is immediate and logged to audit.
- **Never embed provider secrets in a key.** API keys are scoped credentials;
  provider secrets live server-side under model policy (see
  [../secrets-and-model-config.md](../secrets-and-model-config.md)).

## See also

- [../api-reference.md](../api-reference.md)
- [../rbac-and-scopes.md](../rbac-and-scopes.md)
- [auth.md](auth.md) - the admin/session auth surface
- [cli.md](cli.md) - `engram connect` consumes a key
