# Auth Scope Design

## Goal

Add the first database-backed authorization surface required before hook ingest:
identities, roles, capabilities, API keys, project/team bindings, and an
effective scope resolver that denies cross-scope access before retrieval or
writes can run.

This slice is backend-only. It does not add hook endpoints, DRF serializers,
client commands, token minting UI, session cookies, provider secrets, or
frontend screens.

## Current Gate

The current roadmap item after core models is "Add auth, API keys, and
effective RBAC scope." The next hook ingest slice needs a service boundary that
can authenticate a project-scoped credential, intersect it with the owner's
stored capabilities, produce scope filters, and audit both allowed and denied
decisions.

The hard parity gate requires API keys with explicit capabilities and
authorization filters before ingest, retrieval, context packing, or worker
processing.

## Approaches Considered

### Dedicated Access App

Create `engram.access` for authorization models and services, depending on
`engram.core` for tenant/project/team/audit records.

Tradeoff: one more app and migration dependency. It keeps credential and RBAC
logic out of memory/core persistence and gives future APIs a clear service
boundary.

### Add Access Models To Core

Put identities, roles, grants, and API keys beside the existing core models.

Tradeoff: fewer migrations today, but it mixes authorization and memory-domain
records and makes later service boundaries harder to review.

### Use Django Auth Only

Reuse `django.contrib.auth.User`, groups, and permissions.

Tradeoff: useful for human sessions later, but it does not model service
accounts, project-scoped agent credentials, capability narrowing, or Engram's
tenant/project/team filters directly enough for the parity gate.

## Decision

Create `engram.access` with explicit, boring records:

- `Identity`
- `Capability`
- `Role`
- `RoleCapability`
- `OrganizationMembership`
- `TeamMembership`
- `ProjectGrant`
- `ApiKey`
- `ApiKeyCapability`

Seed V1 capabilities and default roles through migrations. Store API key hashes
and fingerprints only; never store raw keys. Keep a single optional project and
team binding on `ApiKey` for the first hook path. Multi-project key bindings,
device flow, key rotation endpoints, and managed hook token minting are deferred
until APIs exist.

Add one domain service, `ResolveApiKeyScope`, with one public `execute()`
boundary. It accepts a raw key, required capability, optional requested
project/team ids, request metadata, and target metadata. It returns an
`EffectiveScope` DTO or raises `AccessDenied`. It writes `AuditEvent` rows for
existing-key allow and deny decisions.

## Data Model

### Identities

`Identity` belongs to exactly one organization and has a type of `user` or
`service_account`. It stores external id, display name, optional email, and
active state. Organization plus type plus external id is unique.

V1 hook credentials should normally be owned by a service account. Human user
session auth is deferred.

### Roles And Capabilities

`Capability` stores a unique capability code, for example
`observations:write`, `memories:read`, and `audit:read`.

`Role` stores default role presets. `RoleCapability` stores the explicit
capability grants for each role. The first seeded roles are:

- `organization_owner`
- `organization_admin`
- `developer`
- `auditor`

This keeps access checks capability-based while preserving role presets for
operator setup.

### Scope Grants

`OrganizationMembership`, `TeamMembership`, and `ProjectGrant` bind an identity
to a role at an organization, team, or project boundary. Cross-organization
foreign key combinations must be rejected through normal `objects.create()`
paths, matching the core model save-time validation pattern.

### API Keys

`ApiKey` belongs to an organization and owner identity. It stores:

- name;
- key prefix;
- HMAC-SHA256 key hash;
- redacted fingerprint;
- optional team and project binding;
- active/revoked/expiry timestamps;
- last-used timestamp.

`ApiKeyCapability` stores explicit API-key capability restrictions. The
effective capability set is:

```text
owner granted capabilities intersect api key capabilities
```

If the key requests a capability its owner does not have, the capability is not
effective.

## Resolver Behavior

`ResolveApiKeyScope.execute()` must:

1. derive prefix/hash/fingerprint from the raw key without persisting the raw
   key;
2. find and constant-time verify the stored hash;
3. reject inactive, revoked, expired, or owner-inactive keys;
4. collect owner capabilities from active organization/team/project grants;
5. intersect owner capabilities with the key's explicit capabilities;
6. resolve requested project/team only inside the key's organization;
7. deny requested projects outside the key binding or owner grants;
8. deny missing required capability;
9. update `last_used_at` only on success;
10. audit existing-key allowed and denied decisions without leaking the raw key.

Project-scoped keys can access only their bound project. Organization-wide or
cross-project access is allowed only when both the owner grants and key
capabilities contain an explicit admin/project capability.

## Boundaries

This slice owns persistence, hashing helpers, seed migrations, and the
effective-scope service. It does not own:

- HTTP authentication classes;
- DRF permissions;
- hook ingest serializers/views;
- `connect`, `doctor`, or client-side credential storage;
- agent token minting;
- provider secret access;
- retrieval filtering implementation;
- frontend user management screens;
- custom role editing APIs.

Those later slices must consume `ResolveApiKeyScope` or a follow-up service
with the same actor/scope/capability semantics.

## Testing

Tests must prove behavior, not just imports:

- access app is installed and migrations are present;
- default capabilities and role grants are seeded by migrations;
- API keys store hash/fingerprint/prefix only and never raw keys;
- duplicate API-key hashes cannot be inserted;
- API key capabilities are explicit and key capabilities cannot expand owner
  access;
- project-scoped keys deny another project even inside the same organization;
- cross-organization project requests are denied before retrieval filters;
- inactive, revoked, expired, and owner-inactive keys are denied;
- successful and denied existing-key decisions write audit events;
- audit metadata never includes the raw API key;
- cross-scope foreign key combinations are rejected through `objects.create()`.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest engram/access/access_scope_tests.py -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings`
- `cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings`
- `cd apps/backend && poetry check`
- `git diff --check HEAD`

Docker Compose smoke remains blocked until Docker is available in this WSL
distro.

## Self-Review

- No North Star expansion: frontend, MCP, custom admin UI, provider secrets, and
  retrieval ranking remain deferred.
- No local worker regression: credentials authenticate server calls only.
- No broad policy language: V1 uses capability checks plus scope filters.
- No raw credential persistence: tests must cover hash-only storage and audit
  redaction.
- No hidden global retrieval: resolver returns project/team filters before
  future retrieval or context packing can run.
