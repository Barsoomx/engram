# Agent Key + Repository-URL Memory Routing

Date: 2026-07-02
Status: Approved (design), implementation in progress
Branch: `feat/agent-key-repo-routing`

## Problem

Today one `engram install` bakes a single `project_id` into `~/.engram/config.json`,
and the server hard-requires it (`Project.objects.get(organization, id=project_id)`
in `hooks/services.py` and `context/services.py`). A developer working across many
git repos would have to reconfigure per repo, or every repo writes into one
project. That breaks plug-and-play.

## Model (locked)

- **One org-wide agent key**, not project-scoped. Installed once, works across all
  of a developer's git repos.
- **The server routes memory by git `repository_url`**: on ingest it resolves the
  `Project` whose normalized `repository_url` matches, **auto-creating** the
  Project when none exists in that org. A git repo becomes a server Project object
  automatically.
- `project_id` becomes an optional explicit override/hint, no longer required.
- Cross-organization isolation is unchanged: routing and auto-create are always
  scoped to the key's organization.

## Components

### A. `projects:agent` capability

New `Capability` (code `projects:agent`) via a data migration. Semantics: an
org-scoped key carrying it may resolve/auto-create and operate on **any project in
its own organization** (paired with operational caps like `observations:write`,
`memories:read`, `search:query`). The connect/agent-key issue path grants it;
`_issuer_can_grant` already lets an issuer with `projects:*` (org owner) grant it.

### B. Repository-URL canonicalization + resolve-or-create

New helper `engram/core/repository.py`:

- `canonicalize_repository_url(url) -> str`: collapse `https://`, `http://`,
  `ssh://`, and `git@host:path` forms to a **single canonical git form**
  `git@<host>:<owner>/<repo>.git` (lowercase host, strip credentials/ports where
  irrelevant, ensure single `.git` suffix, drop trailing slashes). Empty/invalid →
  `''`. Clients may send any format; the server always stores/compares the
  canonical form.
- **Apply canonicalization at EVERY project write site** so stored
  `Project.repository_url` is always canonical:
  - `console/services.py` `create_project` / project update,
  - `console/views/projects.py`,
  - `engram_bootstrap_admin`, `engram_bootstrap_golden_path`,
  - ingest auto-create (below).
  A data migration canonicalizes existing non-empty `Project.repository_url`.
- `resolve_or_create_project(*, organization, repository_url, repository_root='') -> Project`:
  canonicalize the incoming url, match an existing `Project` in the org whose
  (canonicalized) `repository_url` equals it; if none, create one (name/slug
  derived from `<owner>/<repo>`, slug unique within org), storing the canonical
  url. `get_or_create` keyed on `organization` + canonical url for idempotency
  under concurrent first-writes. Matching canonicalizes both sides, so legacy rows
  and any sent format resolve correctly.

### C. Ingest routing (hooks + context)

- Serializers: `project_id` optional; `repository_url` required when `project_id`
  is absent (validation error `project_or_repository_required` otherwise).
- `hooks/services.py` + `context/services.py`: resolve the project as
  `Project` from `project_id` when given, else `resolve_or_create_project(...)` by
  `repository_url`. Run authorization against the **resolved** project id, then
  store observation/memory/context under it.

### D. Authorization

- `_project_ids` (access/services.py): in the `key.project_id is None` branch,
  allow when effective capabilities include `projects:agent` (new
  `_has_agent_scope`) in addition to `projects:*`/`policy:admin`, resolving the
  requested (already in-org, possibly just-created) project. Keys WITHOUT
  `projects:agent` and without project admin stay denied for `project=None`
  (existing negative tests preserved). Cross-org requested project → denied.

### E. CLI

- `engram install` / `engram connect`: `--project` optional. `write_local_state`
  writes `project_id` only when provided.
- Hook payload: always include `repository_url`. When the harness input lacks it,
  derive it via `git -C <repository_root|cwd> remote get-url origin` (best-effort;
  empty on failure). `base_hook_payload` no longer raises `missing_project` when
  `project_id` is absent but `repository_url` is present.

### F. Frontend

- `ConnectAgentModal`: drop the project selector; issue an org-wide agent key
  (`observations:write`, `memories:read`, `search:query`, `projects:agent`), name
  `claude-code · <org-slug>`. `buildConnectCommand` omits `--project`.

## Tests

- Unit: `normalize_repository_url` cases (ssh/https/.git/trailing slash/case).
- Service: `resolve_or_create_project` match, create, and cross-org isolation.
- Ingest: hook + context route by `repository_url` (match + auto-create), and
  `project_id` still works as override.
- Authz: org-scoped `projects:agent` key allowed for an in-org (incl. just-created)
  project; denied cross-org; non-agent `project=None` still denied.
- CLI: `install`/`connect` without `--project`; hook derives `repository_url`;
  payload omits `project_id`.
- Frontend: `pnpm typecheck` + `pnpm lint`.

## Boundaries

- Existing project-scoped keys and explicit `project_id` continue to work.
- Auto-create is per-organization only; never crosses org boundaries.
- Codex client parity remains a later slice.
