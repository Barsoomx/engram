# RBAC And Scopes

## Model

The access model follows the operational shape of Sentry:

- Organization: tenant boundary and billing/security owner.
- Team: day-to-day collaboration boundary.
- Project: product, service, repository group, or agent workspace.
- User: human identity with organization and team memberships.
- Service account: non-human identity owned by an organization or team.
- API key: credential with scopes derived from its owner and optionally narrowed.

Scopes inherit downward:

```text
organization grant
  -> team membership/grant
    -> user or service account grant
      -> api key scope restriction
```

An API key must never grant access that the owning user or service account does
not already have.

## Roles

V1 default roles:

- Organization Owner: full organization control.
- Organization Admin: members, teams, projects, integrations, model policy.
- Developer: read allowed memory, submit observations, suggest updates.
- Auditor: read audit and configuration, no write access.

Roles are presets over capabilities. The implementation should store capability
grants explicitly so customers can add custom roles later without a migration of
every access check.

## Capability Groups

- `members:*`
- `teams:*`
- `projects:*`
- `api_keys:*`
- `secrets:*`
- `model_policy:*`
- `observations:write`
- `observations:read`
- `memories:read`
- `memories:propose`
- `memories:review`
- `memories:admin`
- `search:query`
- `audit:read`
- `policy:admin`

Avoid a large conditional-access language in v1. Capability checks plus scope
filters are enough for the first enterprise-grade version.

## Memory Visibility

Memory scope types:

- `team`: visible to members of one team.
- `project`: visible to project participants and configured teams.
- `repository`: later refinement for path/repo-specific memory.
- `organization`: later broad-scope memory requiring human approval.
- `memory_pack`: later curated shared bundle.
- `policy_pack`: later policy guidance bundle.

Cross-team collaboration is explicit. A project can include multiple teams, or a
memory pack can be shared with multiple teams. A developer who belongs only to
one team sees only that team unless a shared project, pack, or grant includes
them.

## Effective Access Algorithm

1. Resolve actor from user session, service account, or API key.
2. Load organization membership and team memberships.
3. Load project grants and repository bindings relevant to the request.
4. Intersect owner capabilities with API key restrictions.
5. Apply resource filters before retrieval, ranking, or response construction.
6. Log the decision input and result to audit.

Authorization must run before search results are returned and before memory
content is passed into a model prompt.

## Write Authorization

Hook writes derive target scope from server-side bindings:

1. Resolve actor from API key or agent token.
2. Resolve organization, team, project, and repository binding from the key and
   server configuration.
3. Treat client-supplied scope fields as hints only; they cannot expand access.
4. Allow `observations:write` only inside the resolved team/project binding.
5. Allow `memories:propose` only for candidate memory in the resolved binding.
6. Require Admin or Owner approval for organization scope, shared packs, policy
   packs, and high-impact contradictions.
7. Audit both accepted and denied writes with the resolved scope and checked
   capability.
