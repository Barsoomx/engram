# Project Routing Parity: repository_url on the Memory Read/Write Surface

Date: 2026-07-03 (amended same day per operator directive)
Status: Proposed (design)
Branch: `feat/project-routing-parity`
Base: `f7ec20eb` (origin/master)

Operator directive (decided requirement, not an option to weigh): project
resolution must happen automatically from the current repository, everywhere —
MCP tools, hooks, CLI. The credential (agent key) is user/org-scoped by
design, so "which project is this" semantics belongs to the
client/plugin/server layer, never to per-project config pinning. A single
`project_id` in `~/.engram/config.json` is structurally wrong for multi-repo
use and is demoted to an optional override at most.

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

### Latent mis-routing finding: MCP server cwd is not the workspace

`resolve_runtime` derives the repo from `git_remote_url(os.getcwd())`
(`mcp_tools.py:50`), and the plugin MCP wrapper
(`packages/claude-plugin/hooks/mcp.py`) does not chdir — the server inherits
whatever cwd Claude Code gives it. Claude Code does NOT guarantee the
workspace as cwd for stdio MCP servers; for user-scope plugin installs the
process cwd points at the **plugin cache**, a known bug
(github.com/anthropics/claude-code issue #42687). The documented mechanism is
the `CLAUDE_PROJECT_DIR` environment variable, set for spawned stdio MCP
servers since Claude Code v2.1.139 (code.claude.com/docs/en/mcp.md).

This is worse than a dead fallback: the plugin cache of a git-installed
marketplace is itself a git checkout, so `git remote get-url origin` inside it
returns the **marketplace repo's** URL — today's "working" `engram_search` /
`engram_context` fallback can silently resolve (and auto-create) a project for
the marketplace repo and route memory there. The fix below makes
`CLAUDE_PROJECT_DIR` the primary derivation source.

## Decision

Per the operator directive, automatic per-repository routing is the addressing
model for the entire agent surface. Concretely:

**Server-side symmetry across ALL agent-facing endpoints, with a single
shared, scope-enforcing project resolver — plus clients that always derive
`repository_url` from the current repository when no explicit project
override is given.** No new endpoints, no client-side project cache, no
connect-time pinning (rejected up front by the directive).

### Resolve semantics per endpoint class

| Endpoint class | Semantics | Justification |
|---|---|---|
| Hooks ingest (`/v1/hooks/*`) | resolve-or-**create** | First write of real data; a new repo legitimately becomes a Project here. Unchanged. |
| Context bundles (`/v1/context/*`) | resolve-or-create (unchanged) | Session-start may precede the first hook event of a brand-new repo; the session row needs a project to attach to. |
| Search (`/v1/search/`) | resolve-or-create (unchanged) | Pre-existing behavior preserved to keep the slice reviewable; converting to resolve-only is flagged as a follow-up decision (a read path creating projects is a wart). |
| Observations read (`/v1/observations/`, `/{id}`) | resolve-**only**, 404 `project_not_found` | Reads existing data; a repo with no project has no observations — creating an empty project as a read side effect is garbage-row generation. |
| Memory mutations (`/v1/memories/{id}/feedback\|version\|links`) | resolve-only, 404 | The target `memory_id` must already exist inside the project; a just-created project cannot contain it, so auto-create could only ever precede a 404. |
| Memory reads (`/v1/memories/{id}/version\|links\|diff` GET) | resolve-only, 404 | Same argument as observations. |

When both `project_id` and `repository_url` are sent, `project_id` wins and
`repository_url` is ignored — matching `hooks/services.py:78`,
`search/services.py:83`, `context/services.py:891`.

### One precedence ladder, everywhere (client-side)

Project selection follows a single documented ladder on every surface; the
server always re-authorizes the outcome, so no rung can expand scope:

1. **Explicit per-call argument** — new optional `project_id` argument on all
   six MCP tools; new optional `--project` flag on `engram search`,
   `engram observations`, `engram memory version|link|links`. Hooks: the
   harness-input `project_id` field (already supported).
2. **`ENGRAM_PROJECT_ID` env var** (already read by `resolve_runtime`,
   `mcp_tools.py:45-47`; hooks currently skip env — harmonized by this slice).
3. **`~/.engram/config.json` `project_id`** — optional override only;
   `engram connect` keeps working without `--project` as the default flow.
4. **Repo-derived `repository_url`** — the normal case, derived from the
   current repository (derivation source ladder in §4).

Rungs 1-3 produce a `project_id` payload/param; rung 4 produces
`repository_url`. The ladder replaces today's mixed behaviors (MCP: env >
config > cwd for two tools, hard-fail for four; CLI: config-only; hooks:
config > payload url > payload cwd).

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
  Rejected up front by the operator directive, and structurally wrong:
  `~/.engram/config.json` is global while developers work in many repos; a
  single pinned project is exactly the model PR #118 removed because "every
  repo writes into one project". Pinning would fix one repo and silently
  corrupt routing for all others.
- **MCP `roots` capability as the repo-derivation source.** The
  protocol-native option (client-provided workspace roots) was evaluated and
  rejected for now: Claude Code advertises `roots` in its initialize
  capabilities but does not implement it — server-initiated `roots/list`
  requests time out (claude-code issues #3315, #31893) and
  `notifications/roots/list_changed` is never sent (#26663). Our stdio server
  is also a passive request/reply loop (`mcp_server.py:162-180`) with no
  server-initiated request machinery. Deferred: revisit when Claude Code
  implements `roots`; the derivation ladder in §4 leaves room to slot it in
  above the cwd fallback.
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
4. **Membership guard** — binding wins over capability:

   ```python
   allowed = project.id in scope.project_ids or (
       scope.actor_type == 'api_key'
       and not scope.project_bound
       and 'projects:agent' in scope.capabilities
   )
   ```

   Otherwise raise `AccessDeniedError('project_scope_denied')` and write a
   DENIED audit event carrying the resolved project id.

   The capability branch exists only to admit projects auto-created in
   step 3, which cannot be in the precomputed all-org tuple
   (`access/services.py:293-296`) — and it applies ONLY to org-wide
   (unbound) API-key scopes. A project-BOUND key never takes the branch:
   its binding is the sole rule, even if the key was (mis)granted
   `projects:agent`. `EffectiveScope` (`access/services.py:43-51`) does not
   currently expose the binding, and `project_ids` shape is ambiguous (an
   org-wide key in a one-project org also yields a length-1 tuple), so this
   slice adds an explicit `project_bound: bool` field: set from
   `bool(key.project_id)` where `ResolveApiKeyScope.execute` constructs the
   scope (`access/services.py:204-213`; the bound branch at
   `access/services.py:282-288` already forces
   `project_ids == (key.project_id,)` regardless of capabilities), and
   `False` in `_session_scope` (`access/request_scope.py:94-103`) — where it
   is inert anyway, because the `actor_type == 'api_key'` conjunct excludes
   session scopes from the capability branch (console users are governed by
   membership/grants alone).

Consumers:

- **New**: observations list/detail views, memory feedback/version/link/diff
  views (all with `allow_create=False`).
- **Retrofit (security fix)**: `hooks/services.py` (`allow_create=True`),
  `search/services.py`, `context/services.py` (`allow_create=True`, existing
  behavior) replace their inline `if data.project_id / resolve_or_create_project`
  blocks so the membership guard applies everywhere. Org-wide agent keys see
  no behavior change (their `project_ids` covers the org, and the unbound
  capability branch covers auto-create); project-BOUND keys sending a
  mismatched repo url are now denied — with or without `projects:agent` —
  that is the fix for the latent finding.

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

**Repo-derivation source ladder** (which directory the repository URL is read
from), shared by a new helper (e.g. `workspace_repository_url()` next to
`git_remote_url`):

1. MCP bridge: `CLAUDE_PROJECT_DIR` env var when set — the documented
   workspace pointer Claude Code gives stdio MCP servers (v2.1.139+); fixes
   the plugin-cache mis-routing finding for `engram_search`/`engram_context`
   too;
2. fallback: `os.getcwd()` (correct for `engram mcp serve` launched manually
   and for other runtimes);
3. CLI commands: `os.getcwd()` (the user runs them inside the repo);
4. hooks: unchanged — harness input `repository_url`, else
   `repository_root`/`cwd` from the hook payload (`commands.py:1290-1293`),
   which Claude Code populates with the session directory;
5. deferred rung: MCP `roots/list` above the cwd fallback, once Claude Code
   implements it (see Rejected alternatives).

`packages/cli/engram_cli/mcp_tools.py`:

- delete the `require_project=True` gate from the four handlers and retire
  `PROJECT_REQUIRED_MESSAGE`; `resolve_runtime` keeps returning `None` only
  when neither a project override nor a repo URL resolves
  (`mcp_tools.py:54-55`) — the `NOT_CONFIGURED_MESSAGE` path stays;
- `resolve_runtime` derives `repository_url` via the source ladder above
  instead of bare `os.getcwd()` (`mcp_tools.py:50`);
- add optional `project_id` to all six tool input schemas
  (`mcp_server.py:list_tools`), consumed as ladder rung 1;
- replace `_project_payload` with `_scope_payload` for the three POST tools;
- `list_observations`: send `repository_url` as a query param when no project
  override resolves;
- map the new `project_not_found` error to guidance text: "No Engram project
  exists for this repository yet — it is created on the first hook ingest."

`packages/cli/engram_cli/commands.py`:

- `run_observations`, `run_memory_version`, `run_memory_link`,
  `run_memory_links`: apply the precedence ladder — new `--project` flag, then
  `ENGRAM_PROJECT_ID`, then config `project_id`, then
  `repository_url = git_remote_url(os.getcwd())` exactly like `run_search`
  (`commands.py:1720-1722`); when nothing resolves, fail client-side with the
  existing `missing_project`-style CliError instead of shipping an empty UUID;
- `run_search` gains the same `--project`/env rungs so the ladder is uniform;
- hook payload builders (`base_hook_payload`, `commands.py:1284-1286`): insert
  the `ENGRAM_PROJECT_ID` rung between harness-input `project_id` and config;
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
- Document the precedence ladder once (in `docs/guides/mcp.md` +
  `docs/client-installation.md`) and reference it from the tool/command docs
  instead of restating per surface.

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
9. **Per-call `project_id` tool argument is selection, not authorization.**
   The documented stance "tool arguments cannot expand organization/team/
   project scope" (`docs/mcp-tools.md:88-89`) stays true: the argument only
   chooses which project the client asks for; the server's scope narrowing
   (`ResolveApiKeyScope` with `requested_project_id`) and the membership guard
   decide. Negative test: tool-arg project_id outside the key's scope →
   `project_scope_denied`.
10. **Wrong-workspace routing.** Deriving the repo from an unrelated cwd
   (plugin cache, home dir) routes memory to the wrong project silently. The
   `CLAUDE_PROJECT_DIR`-first derivation ladder plus the resolve-only 404 on
   read/write endpoints bound the blast radius; hooks/context auto-create
   remains the only place a wrong derivation can mint a project, unchanged
   from today but now always in-org and membership-guarded.
11. **Capability-grant foot-gun (bound key with `projects:agent`).** A
   project-bound key that was (mis)granted `projects:agent` at issue time
   must NOT be able to use `repository_url` to route into other in-org
   projects — that would recreate, via a capability grant, the exact hole
   this spec closes. The guard's `not scope.project_bound` conjunct makes the
   binding win over the capability on every repo-url path. Required pinning
   negative test: project-bound key WITH `projects:agent` + foreign in-org
   repo url → `project_scope_denied` (and the same key + its OWN project's
   repo url → allowed), on hooks, search, context, and each new endpoint.

## Testing

- **Unit (backend)**: serializer one-of validation for all nine serializers;
  `resolve_project_for_scope` matrix (project_id path, canonical match,
  not-found, membership deny, unbound `projects:agent` allow incl.
  just-created, bound key WITH `projects:agent` denied for a foreign project
  and allowed for its own, session scope never takes the capability branch,
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
  key + foreign repo-url now denied — including the pinning case where that
  key also carries `projects:agent`; org-wide agent key behavior unchanged
  (existing tests keep passing).
- **CLI tests** (`packages/cli/engram_cli/*_tests.py`): the four commands
  derive repo-url from git when config project_id is null; explicit
  `missing_project` error when neither resolves; userinfo stripped from
  `git_remote_url` output; precedence ladder order proven
  (`--project` > `ENGRAM_PROJECT_ID` > config > repo-derived) for search,
  observations, and memory commands; hooks honor `ENGRAM_PROJECT_ID`.
- **MCP tests** (`mcp_tools_tests.py`): four tools send `repository_url` in
  repo-url mode and no longer return the project-required message; observation
  GET carries the query param; `project_not_found` rendered as guidance text;
  `project_id` tool argument wins over env/config/repo; `CLAUDE_PROJECT_DIR`
  wins over cwd when both point at different git repos (plugin-cache
  mis-routing regression).
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
3. The precedence ladder (tool arg/`--project` > `ENGRAM_PROJECT_ID` > config
   `project_id` > repo-derived) holds identically on MCP, CLI, and hooks, and
   is documented in one place; explicit `project_id` behaves exactly as before
   on every endpoint; console/frontend flows unchanged.
3a. Under Claude Code, the plugin MCP server derives the repo from
   `CLAUDE_PROJECT_DIR`, not from its process cwd; a plugin-cache cwd can no
   longer influence routing.
4. Project-scoped key + foreign in-org repo-url is denied with
   `project_scope_denied` on hooks, search, context, and all new repo-url
   endpoints, with DENIED audit events carrying the resolved project id —
   including when the bound key also carries `projects:agent` (binding wins
   over capability).
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
