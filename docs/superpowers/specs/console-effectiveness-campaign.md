# Console Effectiveness Campaign

Date: 2026-07-07. Base: master b3e6c572. Owner: team lead (Fable session).
Source: 25-agent page-by-page review (wf_2ed7127c-a21, 179 findings) + lead verification.

Goal: every console page lets an operator do its jobs-to-be-done in a few clicks;
no UI element displays data the backend never populates; shared patterns for
tables, filters, errors, pagination, dates; deep-linkable filter state.

## Slices

### B1 feat/console-api-parity (backend)
Additive query params / serializer fields. Contract (names are FIXED, frontend
is built against them):
- inspection memories list: `search` (title/body icontains), `status`,
  `ordering` in {created_at,-created_at}, default `-created_at` (today it is
  oldest-first — that default change is intentional).
- members list: `search` (display name/email/external id icontains), `role`,
  `active` (bool; today list is active-only — active=false must return
  deactivated members). New action: reactivate member (mirror deactivate route
  convention).
- teams, organizations lists: `search` (name/slug icontains).
- projects list: `search`, `page`, `page_size`, `ordering` in
  {-created_at,name} (today hard-capped 50, no paging).
- api-keys list: `status` in {active,expired,revoked}, `search` (name/prefix).
- memory-review list: `ordering` in {confidence,-confidence,created_at,-created_at}.
- workflow-runs list: `request_id`, `correlation_id` exact filters.
- inspection context-bundles list: `session_id`, `status`.
- roles serializer: add `description` (model field exists, serializer drops it).
- model-policies serializer: expose `base_url` on read (currently write-only).
- metrics views (overview/ingest-trend/sessions/activity): optional
  `project_id`, `team_id` params that NARROW within request.effective_scope
  (never widen; outside-scope id -> empty result, not 403).
TDD; tests next to modules per repo style.

### B2 feat/bundle-status-loop (backend)
ContextBundle.status is dead: INJECTED/SKIPPED never assigned, every bundle
stays `created` and the console status pill lies. Fix the loop:
- bundle built with >=1 item and returned to the hook/context caller -> INJECTED;
- bundle built with 0 items (or injection suppressed) -> SKIPPED.
Set at serve/build time in the context bundle usecase path; no migration
(states exist). Historical rows stay `created`. Tests for both transitions.

### B3 feat/console-honesty-fixes (backend; recon-verified 2026-07-07)
- HookDryRunSerializer: remove dead `agent_runtime`/`agent_version` fields
  (accepted but never read by HookDryRunView — hooks/serializers.py:50-51).
- ApplyPresetSerializer/View: add `replace_existing: bool` (default False).
  If active policies exist in scope for the preset's task types and
  replace_existing is False -> 409 {code:'existing_policies',
  policies_to_replace:[ids]} with NO mutations; disable+recreate only when
  True. Return `disabled_policy_ids` in the success response.
- ApplyPresetSerializer: validate scope='team' requires team_id (today a
  team-scoped apply with team_id=None disables nothing and creates a
  team_id=None policy).

### F0 feat/console-ui-kit (frontend, this spec committed here)
Shared kit; canonical patterns picked from existing best implementations:
- `ErrorState` component (message + optional retry) replacing 13 inline error
  boxes and raw <pre> dumps; `EmptyState` stays.
- `PaginationFooter` (page/pageSize/total, keepPreviousData-friendly) and
  a single load-more pattern; page-level lists must show real totals.
- `TimeStamp` component: relative time with absolute tooltip (consolidate 4
  copy-pasted absolute formatters into lib/format-time helpers).
- `useUrlFilters` hook: filter/page state synced to URLSearchParams
  (deep-linkable, shareable, survives reload).
- Filter bar convention: HeroUI Select/Input variants (bordered, sm) — the
  model-policies/memory-review style is canonical; raw hand-styled inputs are
  not.
- `StatusPill` driven by design.ts status tokens (stop re-implementing
  status->color mapping per page; extend design.ts helpers as the single map).
- API client: typed functions/params for every B1 contract line above
  (console-api.ts / admin-api.ts / metrics-api.ts), unused-by-pages until later
  slices wire them.
- Apply kit to two exemplar pages: roles (add description column, drop dead
  'Type' column, ErrorState) and organizations (show status/member_count/
  viewer_role, search box, TimeStamp) as living proof.

### F1 feat/memories-overhaul (frontend; after B1+F0)
List: server-side kind/status/search/ordering (newest-first default), real
pagination with URL state, status+stale+refuted badges (archived/refuted must
be visually distinct), TimeStamp. Detail: link source observation + session,
show version history, remove dead 'Add to context bundle' button, rebuild
'Related memories' as labeled links (drop version/doc-hash noise), ErrorState.

### F2 feat/dashboard-cockpit (frontend; after B1+F0)
Dashboard: ops-health strip via existing useOpsOverview (outbox backlog +
oldest age, dead letters, failed workflow runs, pending embeddings; warning/
danger thresholds; links to /workflow-runs), isError states on every panel
(broken != idle), 30s polling for overview/sessions/activity, pass active
project/team to metrics (B1 params) so the switcher stops being a placebo,
sessions panel: scroll cap + Active-only toggle + honest label, activity rows
deep-link by target_type. Health page: use readyz (component checks) not just
healthz, last-checked timestamp + manual refresh, ops tiles, design tokens.
Visual-audit additions (see scratchpad screens/home-desktop.png): the
'Connected agents' stat card shows a different number than the live-badge and
the sessions panel (1 vs 8) — make one truth (distinct agents vs sessions,
label whichever is shown); ingest chart says 'last 14 days' but renders only
days with data — render all days incl. zeroes, add a y-scale or value labels,
cap the card height (huge dead space below bars); Weekly digest card sticks in
'Generating…' and leaves half the card empty — show generation state honestly
and collapse dead space.

### F3 feat/admin-pages-pack (frontend; after B1+F0)
api-keys: real pagination (server total), capabilities shown in full (chips,
not the word 'multiple'), owner+expiry columns, expiry input in issue modal iff
backend accepts it (verify; else drop), status filter + search, ErrorState.
secrets: fix org-scope mutations blocked when team selected (scope the request
explicitly, guard modal), provider/scope/active filters, timestamps, error
state, pager. members: search/role/status filters incl. deactivated view +
reactivate, remove dead Suspended branches, keepPreviousData, URL state.
teams: search, pagination, archive failure surfaced, drill-in links (members
filtered by team, projects). projects: search, pagination + total, links to
memories/observations filtered by project, honest archive copy (or restore if
trivially exposed). organizations/roles: covered in F0 exemplars; extend if
gaps remain.

### F4 feat/pipeline-pages-a (frontend; after B1+F0)
audit: actor/project/team/target_type filters, result as enum Select,
until-date inclusive (end-of-day), URL deep-link incl. selected event, actor/
target entity links, use shared audit color helper, placeholderData.
workflow-runs: run-type list matches backend enum (add observation_processing,
drop nonexistent 'session'), project names not UUIDs (names already loaded),
rerun button only for rerunnable types, request/correlation search, until-date
inclusive, auto-poll while any visible run is running, human run-type labels,
placeholderData. observations: detail modal renders distilled fields from
already-loaded row (no refetch), correlation_id filter, date pickers instead of
raw ISO inputs, observation_type Select, session_id links to filtered
observations view, TimeStamp, URL state, totals.

### F5 feat/pipeline-pages-b (frontend; after B1+B2+F0)
memory-review: project filter, restore-refuted action (backend exists),
bulk-archive-below-threshold UI, team Select instead of raw UUID box, fix
error+empty simultaneous render, keepPreviousData, confidence sort, version
diff via version picker (list real versions), links to memory/observation, URL
state. context-bundles: honest status pill (post-B2) + status/session/date
filters, bundle items link to memory detail, show retrieval latency + scope
evidence on detail, ErrorState parity with list. digests: week navigation
(window params exist server-side), changelog actually newest-first, rows link
to memories, PrimaryButton, honest 'last completed week' header. search-debug:
memory links, reset stale results on scope switch, excluded list grouped by
reason with identifiers, human enum labels, lexical enablement indicator, URL
state. settings: wire the 3 unreachable retrieval/curation toggles, surface
save failures, advisory visible on load, shared ConfirmDialog for purge,
resolve UUIDs to names. hook-debug (recon-verified): remove the page-level
CapabilityGate 'observations:write' (page.tsx:277) — the server enforces the
capability on the PRESENTED credential (hooks/views.py:38), so the gate only
locks out operators; default the handshake to the key's FULL reach (send
project_id: null — serializer already allows it) with a toggle 'narrow to
active project'; drop the dead runtime/version form fields (B3 removes them
server-side); copyable full identifiers; show which project/team is being
tested.

### F6 feat/console-ia-nav (frontend; after F0)
Sidebar regroup: Pipeline (dashboard, memories, observations, memory review,
context bundles, digests, workflow runs) / Debug (search debugger, hook
debugger) / Models (model policies, model setup — cross-linked; setup apply
passes scope/project_id/team_id — ApplyPresetSerializer already accepts them,
the page hardcodes scope:'organization' (page.tsx:150-156); apply flows
through the B3 replace_existing contract: ConfirmDialog lists
policies_to_replace from the 409 before retrying with replace_existing=true;
preset cards show the model assignments they will write; drop phantom
task types rerank/admin_assistant from console-api.ts:377-383,478-485 —
backend TaskType has 4) / Access (organizations, teams, members, roles,
api keys, secrets) / System (audit, settings, health). Align nav labels, page
titles, routes. Org-scoped pages that ignore the project switcher get an
explicit 'Org-wide' badge. Sidebar group headers render broken tracking
('ADMINIST RATION' — screens/home-desktop.png) — fix letter-spacing/wrap in
the rebuilt nav. login: support ?next= return URL, autofocus
username, password show/hide, restyle with design-system components (drop
magic pixel values), remove dead GitHub SSO button.

## Explicit non-goals (decided against, this campaign)
- Full DataTable extraction/rewrite (risk > value now; kit components suffice).
- Merging model-policies + model-setup into one route (cross-links + nav
  grouping instead).
- Daily-digest console surface, sessions list page, saved-view presets:
  deferred as follow-ups.
- Client-side-only search boxes are REMOVED where server search now exists,
  never kept alongside.

## Process
One opus implementer per slice, isolated worktree per backend slice, main
checkout serialized for frontend slices. TDD for backend + testable frontend
logic. Evidence per slice: commands + exit codes (backend pytest in container;
frontend pnpm typecheck && pnpm lint), before/after screenshots on the live
visaudit stack for UI slices. Draft PR per slice; merge order B1 -> F0 -> B2 ->
F1..F6 (F-slices rebase on master after F0).
