# Project Routing Parity: repository_url on the Memory Read/Write Surface

Date: 2026-07-03
Status: Proposed (design)
Branch: `feat/project-routing-parity`
Base: `f7ec20eb` (origin/master)

## Problem

### Symptom (verified in production setup)

In the recommended plug-and-play setup (org-wide agent key, PR #118 + #113),
`engram connect` runs without `--project`, so `~/.engram/config.json` has
`project_id: null` and nothing else ever fills it. The MCP tools
`engram_observations`, `engram_memory_link`, `engram_memory_version`, and
`engram_memory_feedback` then permanently return:

> This tool requires a connected project. Run `engram connect --project ...`
> or set ENGRAM_PROJECT_ID.

while `engram_search` and `engram_context` work. The same gap exists in the
CLI commands (`engram observations`, `engram memory version|link|links`),
which fail with an HTTP 400 instead of a client-side message.

### Root cause

PR #118 (`docs/superpowers/specs/2026-07-02-agent-key-repo-url-routing-design.md`)
introduced repository-url routing but explicitly scoped it to ingest paths:
hooks (`hooks/services.py:78-85`), context (`context/services.py:891-898`),
and search (`search/services.py:83-90`). The agent-facing memory read/write
endpoints — observations list/detail and memory feedback/version/link/diff —
were never migrated: their serializers still hard-require a `project_id` UUID
and accept no `repository_url`
(`apps/backend/engram/observations/serializers.py:9,22`,
`apps/backend/engram/memory/serializers.py:10,27,50,70,75,88,95`).

The MCP delivery slice (`docs/superpowers/specs/2026-07-03-mcp-delivery-design.md`,
"project_id becomes optional: when absent, tools fall back to repository_url
... matching CLI search/hook behavior") assumed symmetric server behavior that
does not exist. Because the server contract could not carry `repository_url`
on these endpoints, the MCP bridge shipped a hard client-side gate instead:
`_require_runtime(require_project=True)`
(`packages/cli/engram_cli/mcp_tools.py:67-77`) guarding the four handlers
(`mcp_tools.py:190,222,266,307`), and `_project_payload` sends only
`project_id` (`mcp_tools.py:356-361`). `docs/guides/mcp.md:108-116` then
documented the asymmetry as expected behavior instead of flagging it as a gap.

The golden-path e2e never catches this because it always connects the MCP
config with an explicit project id (`scripts/e2e_golden_path.py:110-129`), so
the four tools are only ever exercised in pinned-project mode.

Root cause in one sentence: **repo-url routing was implemented as an ingest
feature, not as the tenancy/addressing model for the whole agent surface, so
every endpoint added or left outside the ingest slice silently regressed to
the pre-#118 project-pinned contract — which the default setup no longer
satisfies.**

### Broken-surface inventory

| Surface | Path | project_id null behavior | Evidence |
|---|---|---|---|
| MCP `engram_search` | `POST /v1/search/` | works (repo-url fallback) | `mcp_tools.py:104-123`, `search/serializers.py:41-44` |
| MCP `engram_context` | `POST /v1/context/session-start` | works (repo-url fallback) | `mcp_tools.py:148-170`, `context/serializers.py:53` |
| MCP `engram_observations` | `GET /v1/observations/` | broken (client gate) | `mcp_tools.py:222` |
| MCP `engram_memory_link` | `POST /v1/memories/{id}/links` | broken (client gate) | `mcp_tools.py:190` |
| MCP `engram_memory_version` | `POST /v1/memories/{id}/version` | broken (client gate) | `mcp_tools.py:266` |
| MCP `engram_memory_feedback` | `POST /v1/memories/{id}/feedback` | broken (client gate) | `mcp_tools.py:307` |
| CLI `engram search` | `POST /v1/search/` | works (repo-url fallback) | `commands.py:1685-1689,1720-1722` |
| CLI `engram observations` | `GET /v1/observations/` | broken (sends `project_id=''` → HTTP 400) | `commands.py:1922-1925` |
| CLI `engram memory version` | `POST /v1/memories/{id}/version` | broken (sends `''` → HTTP 400) | `commands.py:1792-1796` |
| CLI `engram memory link` | `POST /v1/memories/{id}/links` | broken (sends `''` → HTTP 400) | `commands.py:1831-1836` |
| CLI `engram memory links` | `GET /v1/memories/{id}/links` | broken (sends `''` → HTTP 400) | `commands.py:1871` |
| Hooks ingest | `POST /v1/hooks/*` (pre/post-tool-use, session-start/end, …) | works (repo-url resolve-or-create) | `hooks/services.py:78-85`, `commands.py:1284-1296` |
| Server `GET /v1/observations/{id}` | detail | no repo-url contract | `observations/serializers.py:21-23` |
| Server `GET /v1/memories/{id}/version` | version history | no repo-url contract | `memory/serializers.py:87-89` |
| Server `DELETE /v1/memories/{id}/links` | link removal | no repo-url contract | `memory/serializers.py:74-84` |
| Server `GET /v1/memories/{id}/diff` | diff | no repo-url contract | `memory/serializers.py:92-96` |

Capabilities are NOT the blocker: the shipped connect-modal agent key already
carries `observations:read`, `memories:review`, and `projects:agent`
(`apps/frontend/src/components/connect/connect-agent-modal.tsx:36-40`, PR #134).
Only the request contract is.

### Latent security finding (discovered during this analysis)

The existing repo-url paths resolve the effective scope with
`requested_project_id=None` and never verify that the resolved project is
inside `scope.project_ids`:

- hooks ingest: `hooks/services.py:66-96` — no post-resolution membership check;
- search: `search/services.py:83-91`;
- context: `context/services.py:880-910`;
- `authorized_retrieval_documents` (`context/services.py:284-292`) authorizes
  PROJECT-visibility documents purely because they belong to the resolved
  project.

Consequence: a **project-scoped** key (bound to project A,
`access/services.py:282-288` returns `project_ids=(A,)`) that sends the
`repository_url` of in-org project B reads B's PROJECT-visibility memory via
search/context and writes observations into B via hooks. This violates
goal.md's tenancy contract ("A project-scoped key cannot read or write
another project") and must be closed by the same resolver this spec
introduces.

## Decision

**Server-side symmetry (option a), with a single shared, scope-enforcing
project resolver applied to every repo-url path — plus the thin client change
that stops gating on project_id.** No new endpoints, no client-side project
cache, no connect-time pinning.

Semantics: **resolve-only** (never auto-create) for the memory/observation
read/write endpoints. The mutating endpoints target an existing `memory_id`;
a project that does not exist cannot contain that memory, so auto-creating one
as a side effect of a failed write is pure garbage-row generation.
Observations list similarly reads existing data. Auto-create remains where it
belongs: hook ingest (first write of real data). When both `project_id` and
`repository_url` are sent, `project_id` wins and `repository_url` is ignored —
matching `hooks/services.py:78`, `search/services.py:83`,
`context/services.py:891`.

### Rejected alternatives

- **(b) `GET /v1/projects/resolve` + client-side per-repo cache.** Adds a new
  public agent surface and a cache in the global `~/.engram` that must be
  keyed per repository, invalidated when projects are created/renamed, and
  raced against hook-ingest auto-create (resolve before first hook event
  404s). Neither search/context responses expose a top-level resolved
  project_id today (`search/services.py:41-67` only embeds it per match in
  `scope_evidence`; the context bundle response has no project field,
  `context/services.py:106-129`), so this option also requires server work.
  Server symmetry makes the whole problem disappear without client state; a
  resolve endpoint can still be added later for `engram doctor` UX.
- **(c) Connect-time pinning (`engram connect` writing project_id).**
  `~/.engram/config.json` is global while developers work in many repos; a
  single pinned project is exactly the model PR #118 removed because "every
  repo writes into one project". Pinning would fix one repo and silently
  corrupt routing for all others.
- **Client-only fallback without server changes** is impossible: the server
  serializers reject requests without a `project_id` UUID.

### Why this fits the product invariants (goal.md)

- *Agent-native read+write paths*: submit observations, link memory, mark
  stale/refuted are first-class agent workflows; they must work in the default
  install, not only with a hand-pinned project.
- *Tenant isolation / authorization before ranking*: the shared resolver runs
  entirely inside the key's organization and enforces scope membership before
  any memory/observation query executes ("All tenant-owned queries must begin
  from an already resolved authorization scope").
- *Server-only runtime*: clients keep only ephemeral connection state; project
  identity stays server-derived from the repo URL.
- *LLM-agnostic*: fixes CLI, MCP, and any future runtime at the contract
  level, not per-client.

## Architecture

### 1. Shared scope-enforcing resolver (backend)

Add to `apps/backend/engram/core/repository.py` (alongside
`canonicalize_repository_url` / `resolve_or_create_project`):

```python
class ProjectNotFoundError(DomainError):
    default_error_code = 'project_not_found'


def resolve_project_for_scope(
    *,
    scope: EffectiveScope,
    project_id: uuid.UUID | None,
    repository_url: str,
    allow_create: bool = False,
    repository_root: str = '',
) -> Project
```

Behavior:

1. `project_id` given → `Project.objects.get(organization_id=scope.organization_id, id=project_id)`
   (scope narrowing already happened in `resolve_request_scope` /
   `ResolveApiKeyScope` with that id).
2. Else canonicalize `repository_url`; empty/invalid →
   `RepositoryUrlRequiredError` (`project_or_repository_required`, 400).
3. Resolve within `scope.organization_id` only. Missing project:
   `allow_create=True` → create (hooks ingest); else `ProjectNotFoundError`
   (404).
4. **Membership guard**: allow iff `project.id in scope.project_ids` or
   `'projects:agent' in scope.capabilities` (the agent branch also covers
   projects just created in step 3, which cannot be in the precomputed
   all-org tuple from `access/services.py:293-296`). Otherwise raise
   `AccessDeniedError('project_scope_denied')` and write a DENIED audit event
   carrying the resolved project id.

Consumers:

- **New**: observations list/detail views, memory feedback/version/link/diff
  views (all with `allow_create=False`).
- **Retrofit (security fix)**: `hooks/services.py` (`allow_create=True`),
  `search/services.py`, `context/services.py` (`allow_create=True`, existing
  behavior) replace their inline `if data.project_id / resolve_or_create_project`
  blocks so the membership guard applies everywhere. Org-wide agent keys see
  no behavior change (their `project_ids` covers the org, and the
  `projects:agent` branch covers auto-create); project-scoped keys sending a
  mismatched repo url are now denied — that is the fix for the latent finding.

### 2. Serializer changes (backend)

For `ObservationListQuerySerializer`, `ObservationDetailQuerySerializer`
(`observations/serializers.py`) and `MemoryFeedbackSerializer`,
`MemoryVersionSerializer`, `MemoryVersionQuerySerializer`,
`MemoryLinkSerializer`, `MemoryLinkQuerySerializer`,
`MemoryLinkDeleteSerializer`, `MemoryDiffQuerySerializer`
(`memory/serializers.py`):

- `project_id` → `UUIDField(required=False, allow_null=True)`;
- add `repository_url = CharField(required=False, allow_blank=True, default='')`
  with the same 1024-char validation used by search
  (`search/serializers.py:53-59`);
- cross-field `validate()`: at least one of `project_id`/`repository_url`
  present, else `project_or_repository_required` (mirrors hooks contract,
  spec 2026-07-02 §C).

### 3. View changes (backend)

Each affected view (`observations/views.py:27-59,66-98`,
`memory/views.py:53-74,93-144,161-247,263-289`) changes from

```python
scope = resolve_request_scope(request, ..., project_id=data['project_id'], ...)
```

to a two-step flow in the repo-url case:

1. `scope = resolve_request_scope(request, ..., project_id=data.get('project_id'), ...)`
   (passing `None` when absent — capability and org checks run org-wide,
   exactly as hooks/search/context already do);
2. `project = resolve_project_for_scope(scope=scope, project_id=data.get('project_id'), repository_url=data.get('repository_url', ''))`;
3. pass `project.id` into the existing service inputs
   (`ObservationListInput.project_id`, `MemoryFeedbackInput.project_id`, …) —
   the services themselves do not change
   (`observations/services.py:72-76`, `memory/services.py` lock/queries stay
   project-scoped).

Error mapping: `ProjectNotFoundError` → 404, `RepositoryUrlRequiredError` →
400, `AccessDeniedError('project_scope_denied')` → 403; all already flow
through `core/middlewares/drf_exception_handler.py` /
`domain_exception.py`.

Both auth modes keep working: `resolve_request_scope` handles console session
auth (`access/request_scope.py:48-103`) and Bearer keys
(`access/request_scope.py:174-198`); the membership guard uses
`scope.project_ids` uniformly, so a console user's repo-url request is bounded
by their grants the same way.

### 4. Client changes (CLI + MCP bridge)

`packages/cli/engram_cli/mcp_tools.py`:

- delete the `require_project=True` gate from the four handlers and retire
  `PROJECT_REQUIRED_MESSAGE`; `resolve_runtime` already returns a
  repo-url-bearing runtime (`mcp_tools.py:50`) and returns `None` when neither
  resolves (`mcp_tools.py:54-55`) — the `NOT_CONFIGURED_MESSAGE` path stays;
- replace `_project_payload` with `_scope_payload` for the three POST tools;
- `list_observations`: send `repository_url` as a query param when
  `runtime.project_id` is empty;
- map the new `project_not_found` error to guidance text: "No Engram project
  exists for this repository yet — it is created on the first hook ingest."

`packages/cli/engram_cli/commands.py`:

- `run_observations`, `run_memory_version`, `run_memory_link`,
  `run_memory_links`: when `config.project_id` is empty, derive
  `repository_url = git_remote_url(os.getcwd())` exactly like `run_search`
  (`commands.py:1720-1722`) and send it instead of `project_id`; when neither
  resolves, fail client-side with the existing `missing_project`-style
  CliError instead of shipping an empty UUID;
- `git_remote_url` (`commands.py:580-595`): strip URL userinfo
  (`https://user:token@host/...` → `https://host/...`) before returning, so
  embedded credentials never leave the machine in any payload or query string
  (today they would transit hooks/search POST bodies too).

Vendored plugin bundle: rerun `scripts/sync_plugin_bundle.py` so
`packages/claude-plugin/hooks/engram_cli/` matches.

### 5. Docs and contract versioning

- Additive, backwards-compatible request-contract change: `project_id` remains
  accepted everywhere (console/frontend untouched — it always sends
  project_id); `repository_url` is a new optional alternative. This matches
  the PR #118 precedent (search/context/hooks gained `repository_url` with no
  version bump). New error code `project_not_found` (404) is additive.
- Update `docs/guides/mcp.md:108-116` (remove the documented asymmetry),
  `docs/mcp-tools.md`, `docs/api-reference.md`, `docs/backend-contracts.md`.

## Security

Threat cases and how the design answers them:

1. **Cross-tenant repo-url collision.** Two orgs register the same repository
   URL. Resolution always starts from `scope.organization_id`
   (`core/repository.py:79-88` filters by organization); org A's key can never
   resolve org B's project — it gets `project_not_found` (resolve-only paths)
   or an org-A auto-create (hooks). Negative test required per endpoint.
2. **In-org cross-project escape (the latent finding).** Project-scoped key +
   another project's repo url → `project_scope_denied` (403) from the
   membership guard, with a DENIED audit event. Applies to the new endpoints
   AND retrofitted hooks/search/context. Regression tests required for
   search, context, hooks ingest, and each new endpoint.
3. **repository_url as a scope-expansion vector after explicit denial.** When
   `project_id` is present it wins unconditionally; `repository_url` is never
   consulted as a fallback after a project_id-based denial.
4. **Canonicalization edge cases.** `canonicalize_repository_url`
   (`core/repository.py:18-47`) drops userinfo (urlparse `.hostname`),
   lowercases host/path, strips `.git`/slashes, handles SCP-like forms. Ports
   are discarded (`host:8443` ≡ `host`) — accepted, documented quirk. Tests:
   credential-bearing URLs resolve identically to their clean forms and the
   stored `Project.repository_url` never contains credentials.
5. **Credential leakage via query strings.** Observations list takes
   `repository_url` as a GET param, which proxies/access logs may retain.
   Mitigated client-side by userinfo stripping in `git_remote_url` (§4) and
   server-side by canonicalization before storage/audit. Audit metadata must
   record the canonical form only.
6. **Capability gates unchanged.** `observations:read` (list/detail),
   `memories:read` (version GET, links GET, diff), `memories:review`
   (feedback, version POST, link POST/DELETE). Repo-url mode adds no new
   capability, and org-unbound keys without `projects:agent` are still denied
   by `_project_ids` returning `None` (`access/services.py:290-296`). Negative
   test: key lacking `memories:review` + valid repo-url → `missing_capability`.
7. **Replay/idempotency.** `request_id` semantics unchanged; canonicalization
   is deterministic, so a replayed request resolves the same project.
   Resolve-only paths have no creation side effects to replay. Test: repeated
   feedback with the same `request_id` in repo-url mode returns
   `already_applied` without duplicates.
8. **Auditability.** Scope-resolution audit events currently record
   `requested_project_id=None` in repo-url mode. The membership guard adds the
   resolved project id to allow/deny audit metadata so repo-url actions remain
   attributable to a concrete project.

## Testing

- **Unit (backend)**: serializer one-of validation for all nine serializers;
  `resolve_project_for_scope` matrix (project_id path, canonical match,
  not-found, membership deny, `projects:agent` allow incl. just-created,
  cross-org isolation).
- **API tests** (next to modules, `*_tests.py` convention):
  `observations_api_tests.py`, `memory_feedback_tests.py`,
  `memory_versioning_tests.py`, `memory_links_tests.py`,
  `memory_diff_tests.py` — for each endpoint: repo-url happy path with an
  agent key, unknown repo → 404 `project_not_found`, cross-org url → 404,
  project-scoped key + foreign in-org url → 403 `project_scope_denied`,
  missing both → 400 `project_or_repository_required`, project_id override
  still works, project_id+repository_url → project_id wins.
- **Retrofit regression tests**: search/context/hooks with a project-scoped
  key + foreign repo-url now denied; org-wide agent key behavior unchanged
  (existing tests keep passing).
- **CLI tests** (`packages/cli/engram_cli/*_tests.py`): the four commands
  derive repo-url from git when config project_id is null; explicit
  `missing_project` error when neither resolves; userinfo stripped from
  `git_remote_url` output.
- **MCP tests** (`mcp_tools_tests.py`): four tools send `repository_url` in
  repo-url mode and no longer return the project-required message; observation
  GET carries the query param; `project_not_found` rendered as guidance text.
- **E2E**: extend `scripts/e2e_golden_path.py` with a second
  `drive_mcp_stdio` pass using a config dir WITHOUT `project_id`
  (`scripts/e2e_golden_path.py:110-129` currently pins it) and cwd inside a
  git repo whose `origin` matches the bootstrap project's `repository_url`,
  asserting all six tools succeed — closing the blind spot that hid this bug.
- **Bundle**: assert vendored plugin copy is in sync after
  `scripts/sync_plugin_bundle.py`.

## Acceptance Criteria

1. Fresh plug-and-play install (org-wide agent key, `project_id: null`): all
   six MCP tools and `engram observations` / `engram memory version|link|links`
   succeed from a git repo whose remote maps to an existing project.
2. From a repo with no matching project, resolve-only endpoints return 404
   `project_not_found` (MCP renders guidance); hooks ingest still auto-creates.
3. Explicit `project_id` (env or config) behaves exactly as before on every
   endpoint; console/frontend flows unchanged.
4. Project-scoped key + foreign in-org repo-url is denied with
   `project_scope_denied` on hooks, search, context, and all new repo-url
   endpoints, with DENIED audit events carrying the resolved project id.
5. Cross-org url resolution is impossible by construction; negative tests
   prove 404 (not foreign data) for every new endpoint.
6. No payload or stored row contains URL-embedded credentials.
7. Golden-path e2e passes with the new repo-url-mode MCP drive; full backend,
   CLI, and MCP suites green.

## Out of scope

- A `GET /v1/projects/resolve` convenience endpoint and any client-side
  project cache (may come later for `engram doctor`).
- Changing search's resolve-or-**create** semantics (a read path creating
  projects is a pre-existing wart; behavior preserved to keep this slice
  reviewable — flagged for a follow-up decision).
- Console/session-auth UX for repo-url addressing (console always has
  project context).
- Exposing a top-level resolved `project_id` in search/context responses.
- Deferred MCP tools (`memory.observe`, curator/lead tools, `hooks.doctor`).
