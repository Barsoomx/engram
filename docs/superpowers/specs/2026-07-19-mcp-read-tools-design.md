# S2 Read-Tools Design — `engram_memory_get` + `engram_audit`

Slice S2. Two new MCP tools wrapping already-Bearer-reachable endpoints, plus
CLI parity commands. Mostly client work, but a small family of backend guards is
REQUIRED as prerequisites (see **Backend Prerequisites** — the authoritative,
complete list is P1, P2a, P2c, P3, P3-index, and P5) because the read paths this
slice surfaces have real cross-team disclosure gaps and cannot answer "what are
this memory's own recorded events?" without a `target_id` audit filter. Shipping
the client tools without these guards would expose another team's memory
bodies/links and would make `engram_audit` return unrelated events.

**Scope cut (teamlead decision S2, 2026-07-20).** `engram_memory_get` does NOT
use the inspection memory DETAIL view or any inspection-detail-derived data. Its
ONLY backend sources are the by-id memory reads
`GET /v1/memories/<id>/version` (latest item = the current full body — the
400-char-truncation defeat), `GET /v1/memories/<id>/links`, and the optional
`GET /v1/memories/<id>/diff`, each hardened to fail-closed team scope (P1/P2a).
Rich status fields (`status`, `confidence`, `kind`, stale/refuted validity,
`authorized_for_injection`, related memories, retrieval documents, source
provenance) are explicitly out of scope: the agent already gets `kind` +
`confidence` from `engram_search` and validity from the
`conflict_excluded`/`stale_match`/`refuted_match` warnings (slice S3). This
removes the inspection-detail dependency and the entire inspection-view
team-scope hardening effort (the former P4 and the whole P6 family) from this
slice; pre-existing inspection-DETAIL team-scope/visibility leaks are real but
predate this slice and are tracked as a separate inspection-hardening follow-up
(see **Out of Scope**). `engram_audit` still reads the inspection audit-events
list (it needs a resolved `project_id`).

## Problem and Evidence

1. **No tool can read a memory in full.** Session-start injection truncates
   every memory body at 400 chars:
   `SESSION_START_BODY_TRUNCATE_LIMIT = 400`
   (`packages/cli/engram_cli/commands.py:1324`), applied at
   `commands.py:1340-1343` (`f'{body[:SESSION_START_BODY_TRUNCATE_LIMIT]}…'`).
   The 6 existing MCP tools (`mcp_tools.build_tools`,
   `packages/cli/engram_cli/mcp_tools.py:125-132`) expose search, context,
   link, observations, version-update, feedback — none returns a full body,
   status, confidence, or the version history for a single known
   `memory_id`. `engram_search` returns `title` + the full redacted
   `memory.body` (NOT truncated — `redact_text(memory.body)` at
   `apps/backend/engram/search/services.py:53-63`), plus IDs/confidence/kind,
   and the MCP client prints that body unchanged (`mcp_tools.py:169-173`). The
   400-char truncation applies ONLY to session-start injection rendering
   (`commands.py:1340-1358`), not to search results. What is still missing is a
   by-`memory_id` read that returns the full untruncated body + version history
   for a single known memory — search is ranked multi-result retrieval keyed on a
   query, not a single-record lookup.

2. **No tool answers "why is this memory in this state?"** Audit events
   (who refuted / revised / curated a memory, with `actor_display`,
   `result`, `capability`) are only reachable via
   `AuditEventInspectionListView` (`apps/backend/engram/inspection/views.py:150-171`),
   which no client surface calls.

3. **The endpoints already exist and are Bearer-reachable**:
   - `GET /v1/inspection/audit-events` — no trailing slash
     (`inspection/urls.py:23`), `required_capability = 'audit:read'`
     (`views.py:151`). Item shape from `audit_event_response`
     (`views.py:373-405`): `created_at`, `event_type`, `actor_display`,
     `actor_type`, `actor_id`, `target_type`, `target_id`, `target_display`,
     `capability`, `result`, `correlation_id`, `request_id`.
   - `GET /v1/memories/<uuid:memory_id>/version`
     (`apps/backend/engram/memory/urls.py:8`), `MemoryVersionView.get`
     (`memory/views.py:104-144`), `memories:read` (`views.py:114`).
     Response `{'count', 'items': [...]}` ordered `-version`
     (`views.py:139`); each item has `version`, `body`, `content_hash`,
     `source_observation_id`, `source_metadata`, `created_at`
     (`views.py:181-189`). **`items[0]` is the highest-`version` row** (queryset
     `order_by('-version')`, `views.py:139`). Under the version invariant this
     equals `Memory.current_version` / `Memory.body`, but NO database constraint
     enforces that tie (`core/models.py:713,793`) and the invariant subsystem
     explicitly detects missing/mismatched current versions
     (`memory/invariant_queries.py:1147`). So `items[0]` is "the newest stored
     body" — correct for healthy rows, potentially stale on inconsistent legacy
     rows. The fallback render therefore reports it as
     `current_version=<items[0].version>` (the row's own version number), not as
     an authoritative `Memory.current_version`. Routable by `project_id` OR
     `repository_url` (`resolve_project_for_scope`, `views.py:120-124`).
   - `GET /v1/memories/<uuid:memory_id>/links`
     (`memory/urls.py:9`), `MemoryLinksView.get` (`memory/views.py:196-224`),
     `memories:read` (`views.py:204`). Response
     `{'count', 'items': [{link_id, link_type, target, label, created_at}]}`
     (`views.py:303-310`). Routable by `project_id` OR `repository_url`.
   - `GET /v1/memories/<uuid:memory_id>/diff?from_version=&to_version=`
     (`memory/urls.py:6`), `MemoryDiffView.get` (`memory/views.py:313-358`),
     `memories:read` (`views.py:324`). Response
     `{'from': {version, body, created_at}, 'to': {version, body, created_at}}`
     (`memory/services.py:946-963`). Routable by `project_id` OR
     `repository_url`.

4. **`InspectionQuerySerializer` REQUIRES `project_id`** — verified: its
   `validate()` raises `inspection_project_required` when `project_id is None`
   (`apps/backend/engram/inspection/serializers.py:23-34`). So the inspection
   audit-events endpoint (the only inspection endpoint this slice uses, for
   `engram_audit`) is NOT `repository_url`-routable; it needs a resolved
   `project_id`. The by-id memory `version`/`links`/`diff` reads that
   `engram_memory_get` uses ARE `repository_url`-routable
   (`resolve_project_for_scope`), so `engram_memory_get` has no such
   restriction.

5. **Key capability AND project/team-resolution requirements (corrected).**
   `resolve_request_scope` evaluates THREE independent gates, and in this
   order (`access/services.py:135,151,168`): project-resolution →
   **team-resolution** → capability. The team gate fires BEFORE the capability
   gate, so a request can be denied for team scope even when the key holds the
   capability:
   - *Capability*: `engram_memory_get` needs `memories:read`;
     `engram_audit` needs `audit:read`. The interactive-wizard key holds
     `WIZARD_API_KEY_CAPABILITIES = ('memories:read', 'observations:write',
     'search:query')` (`commands.py:57-61`) — so it has `memories:read` but
     NOT `audit:read`.
   - *Project resolution*: the wizard key is issued **unbound** (no
     `project_id`; `issue_wizard_api_key` sends only name + capabilities,
     `commands.py:418-435`; the admin issue view never binds a project,
     `console/views/api_keys.py:157-163`). An unbound key can resolve a
     project ONLY if its effective capabilities include `projects:*`,
     `policy:admin`, or `projects:agent`; otherwise `_project_ids` returns
     `None` and scope resolution raises `project_scope_denied` (403)
     (`access/services.py:293-309,135-149`). The same gate blocks the
     `repository_url` fallback: `resolve_project_for_scope` →
     `_authorize_resolved_project` denies unless the resolved project is in
     `scope.project_ids` or the key is an unbound agent
     (`core/repository.py:120-158`).
   - *Team resolution (the third gate)*: `_team_ids`
     (`access/services.py:344-374`) runs after project resolution and BEFORE
     the capability check (`:151`). It returns `None` — raising
     `team_scope_denied` (403, `:166`) — when the request carries a
     `team_id` (which both tools forward whenever `runtime.team_id` is set,
     Design request params) that the key cannot grant: a key bound to a
     DIFFERENT team, or an unbound key requesting a team without team-admin
     (`teams:*`/`policy:admin`, `_has_team_admin`). With NO requested team it
     returns `()` (empty), which is not a denial but means the effective
     `team_ids` is empty, so the inspection `team_filter`
     (`team__isnull OR team_id__in=()`, `inspection/services.py:52-53`) hides
     every team-scoped row — a team-only memory then reads as
     `memory_not_found`, not `team_scope_denied`. P1 deliberately makes the
     version/links GET raise `team_scope_denied` in the same cross-team case
     (Backend Prerequisites), so the client MUST handle that code too.

   **Consequence — the earlier "works out of the box" claim was FALSE.** The
   plain wizard key (unbound, no `projects:agent`) gets `403
   project_scope_denied` on the version/links reads (and, for `engram_audit`, on
   the inspection audit-events read), not success. The tools work with either
   (a) a **project-bound** key, or (b) the **agent key** issued by the
   Connect-agent modal (NOT `engram install` — round-5 finding 7: `run_install`
   only *consumes* the `--api-key` you supply via `run_connect_flags`,
   `commands.py:634-644`, and never issues or augments a key), which carries
   `projects:agent` and resolves projects by explicit `project_id`
   (`_has_agent_scope`,
   `access/services.py:304-307`) or by `repository_url` routing. For
   `engram_audit` the operator must additionally re-issue with `audit:read`;
   that alone does NOT fix an unbound non-agent key's `project_scope_denied`.
   Both `project_scope_denied` and `missing_capability` are handled in Error
   Handling.

6. **403 denial wire shapes** (verified). `build_domain_error_payload`
   (`apps/backend/engram/core/middlewares/drf_exception_handler.py:116-127`)
   emits `detail` plus **both** `error_code` AND `code` set to the same value
   (lines 120-122), not just `code`. So the two relevant denials render as:
   - `missing_capability`: `{'detail': 'API key lacks required capability',
     'error_code': 'missing_capability', 'code': 'missing_capability'}`
     (`access/services.py:185`; status map `services.py:61` →
     `HTTP_403_FORBIDDEN`).
   - `project_scope_denied`: `{'detail': ..., 'error_code':
     'project_scope_denied', 'code': 'project_scope_denied'}`
     (`access/services.py:149`, `core/repository.py:158`; status map
     `services.py:62` → 403).
   - `team_scope_denied`: `{'detail': ..., 'error_code': 'team_scope_denied',
     'code': 'team_scope_denied'}` (`access/services.py:166`; and raised by
     P1 on version/links via `ensure_memory_team_scope`; status map → 403).

   Client detection keys off `body.get('code')`, which is present in all three
   shapes; `error_code` is an additional field, not a substitute.

## Design

**Two new client tools built on existing endpoints, plus a small set of backend
guards (Backend Prerequisites below).** Compose existing endpoints; the guards
close cross-team disclosure on the by-id read paths and add the `target_id`
audit filter the trace tool needs.

- **`engram_memory_get`** — read one memory's full current body, version
  history, and links (single by-id path; no inspection detail).
  - *Body + versions*: call **version**
    (`GET /v1/memories/{memory_id}/version`; `items[0]` = the highest-version
    row, the current full stored body — see evidence §3, not
    constraint-guaranteed to equal `Memory.current_version`). Render that body
    untruncated plus a `versions:` line listing every returned version.
  - *Links*: call **links** (`GET /v1/memories/{memory_id}/links`); render the
    `links:` line (best-effort — a non-2xx links fetch is surfaced as a warning
    line, not a hard failure; Error Handling).
  - *Diff addendum* (both `from_version` and `to_version` given): also call
    **diff** (`GET /v1/memories/{memory_id}/diff`); render both labeled bodies.
  - All three endpoints are routable by `project_id` OR `repository_url`
    (`resolve_project_for_scope`, evidence §3), so `engram_memory_get` has ONE
    code path — no inspection-requires-`project_id` special case, no
    primary/fallback branch.
  - Rich status fields (`status`, `confidence`, `kind`, stale/refuted validity,
    `authorized_for_injection`, related memories, retrieval documents, source
    provenance) are OUT OF SCOPE (teamlead decision S2): `kind` + `confidence`
    come from `engram_search`, validity from its
    `conflict_excluded`/`stale_match`/`refuted_match` warnings (slice S3). The
    render carries a one-line pointer to `engram_search` for those fields.
  - Bodies rendered WITHOUT truncation.
  - Capability: `memories:read` (already on wizard keys).

- **`engram_audit`** — surface a memory's recorded audit events (the events
  whose audit `target` IS that memory).
  - Call `GET /v1/inspection/audit-events?project_id=...` with optional
    `target_id` (usually the `memory_id` being traced), `target_type`
    (defaulted to `memory` when a `memory_id`/`target_id` is supplied — see
    below), `event_type`, `correlation_id`, `since`, `until`, `limit`.
  - **`target_id` (+ `target_type`) is what scopes this to one subject.**
    Without it the endpoint returns project-wide events oldest-first sliced by
    `limit` (`inspection/services.py:250-272`, `inspection/views.py:154-171`)
    — the first N generally unrelated events, NOT the history of one memory.
    The audit filter set does not currently accept `target_id`
    (`inspection/filters.py:32-38`); Backend Prerequisite P3 adds it.
  - **`target_id` alone is ambiguous — filter must pair it with
    `target_type`.** `AuditEvent` stores `target_type` and `target_id` as two
    independent columns (`core/models.py:1064-1065`) and target-display
    resolution keys them as a `(target_type, target_id)` pair
    (`inspection/views.py:385`). Equal ids across target types (e.g. a memory
    id and a link id that happen to collide, or the same UUID appearing as a
    project target) would otherwise mix into one trace. P3 therefore adds
    BOTH `target_id` and `target_type` filters, and the tool defaults
    `target_type='memory'` whenever the caller passed a `memory_id`/bare
    `target_id`, so a memory trace never picks up non-memory rows. A caller
    tracing a non-memory target passes `target_type` explicitly.
  - Ordering: for a lifecycle read the NEWEST events (closest to current state)
    matter most, and a single memory's transition events are **not** bounded
    (every revise/refute/stale/restore adds a row, `memory/transitions.py:1248-1262`),
    so an oldest-first `limit` slice would drop the newest events. The tool
    therefore requests newest-first ordering directly: it sends
    `ordering=-created_at` on the single audit-events GET. Backend Prerequisite
    **P5** adds a whitelisted `ordering` param to the audit-events inspection
    list (`created_at` / `-created_at`, mirroring the memory-list
    `MEMORY_ORDERING_FIELDS` + `_ordering` pattern at
    `inspection/services.py:56,76-78`; default stays `created_at` for console
    compatibility), and the endpoint returns the newest `limit` rows first with a
    stable id tiebreaker whose sign matches the ordering direction. The client
    makes ONE request and renders `items` in the order received (newest first) —
    no second request, no `offset`, no count/slice windowing, no race-window math.
  - Truncation note: if the response exposes `count` and `count > limit`, the
    tool appends a one-line note
    `(showing most recent <limit> of <count> events; <count - limit> older
    omitted — narrow with since/until/event_type)`. **Accepted risk:** the
    endpoint reads `count` (`qs.count()`) and the page in two separate statements
    (`inspection/views.py:158,161`), so under an in-flight write the `count` may
    skew by a few relative to the returned page — immaterial for a display note
    that only signals "more exist". See Data Flow and Out of Scope.
  - **Scope of the trace (limitation — read before relying on it).** An
    exact `(target_type='memory', target_id=<id>)` filter returns EVERY audit
    event whose audit target IS that memory. In practice this is **every
    `MemoryTransitionCommitted` row for the memory** — `_commit_transition`
    always writes `target_type='memory', target_id=str(memory.id)`
    (`memory/transitions.py:1248-1262`) regardless of transition kind — so the
    trace includes promotion, revise, refute, stale, restore, **supersede,
    archive**, a **candidate merged INTO this memory** (the candidate-merge
    path commits with `memory=result_memory=<this memory>`,
    `_execute_candidate_revision` at `transitions.py:1961-1976`), and a
    **direct two-memory merge where this memory is the SOURCE** (committed with
    `target_id=<source>`, `memory/transitions.py:2060-2076`). The enumerated
    list in the tool description is therefore illustrative, NOT exhaustive —
    any future transition kind is captured automatically because it shares the
    same `target_id=memory.id`.
    - **The audit list applies inspection's pre-existing `team_filter`
      (`team IS NULL OR team_id ∈ scope.team_ids`, `inspection/services.py:257`).**
      A memory tagged to a team the key cannot see therefore yields a filtered
      — possibly empty — trace, even for a `visibility_scope=PROJECT` memory the
      key can read in full via `engram_memory_get` (version/links). Aligning the
      single-memory audit read to the broader memory-visibility rule was part of
      the inspection-view team-scope hardening descoped by teamlead decision S2
      (the former P6-audit); it is a separate follow-up, Out of Scope. In the
      common dogfood case the key resolving the project also grants the memory's
      team, so the full trace returns.
    - **Some DENIED access attempts are in-scope, but the rules are subtle
      (round-5 findings 4 & 5).** `resolve_request_scope` records an
      `AccessScopeResolved` audit event with the request's
      `(target_type, target_id)` — for a memory read that is
      `('memory', <memory_id>)` — but ONLY when the result is not ALLOWED:
      `_audit` returns immediately on `AuditResult.ALLOWED` and creates no row
      (`access/services.py:394-395`). So a **successful** memory-detail /
      version / links read writes **NO** audit event; only a *denied* read
      (project-scope / team-scope / missing-capability failure, the three
      `AuditResult.DENIED` calls at `access/services.py:135-185`) writes a row.
      **Two consequences the earlier draft got wrong:**
      1. **Which denial rows are even RETURNED by the trace (finding 5).** The
         row's `project` FK is set to `resolved_project_ids[0]` only when exactly
         ONE project resolved, else to `key.project_id`
         (`access/services.py:415`) — it is NOT the *requested* project (that is
         kept only in `metadata.requested_project_id`, `:400`). But the audit
         query hard-filters `project=inspection_scope.project`
         (`inspection/services.py:255`). So:
         - `project_scope_denied` passes NO `resolved_project_ids`
           (`access/services.py:135-149`) → the row is stored with
           `project=key.project_id`, which for the emphasized **unbound wizard
           key is `NULL`**. A `NULL`-project row can never match a
           project-required inspection query, so **the unbound-key
           `project_scope_denied` denial does NOT appear in any trace** — the
           earlier blanket "project/team/capability denials appear in the trace"
           claim was false for exactly the case the spec highlighted.
         - `missing_capability` passes `resolved_project_ids=project_ids`
           (`:182`); when the key resolved exactly the requested project the row
           is stored with `project=<that project>` and DOES appear. `team_scope_denied`
           passes `resolved_project_ids=project_ids or ()` (`:164`) and appears
           only when a single project resolved.
         So the in-scope denial rows are precisely those where the key resolved
         the queried project but was then denied on team or capability.
      2. **Those in-scope denial rows are UNBOUNDED and CAN crowd the default
         window (finding 4).** There is no dedup or per-target bound: every
         denied capability/team check for `('memory', <id>)` writes another row.
         A key that resolves the project but repeatedly hits a memory it lacks
         `memories:read` for accumulates `AccessScopeResolved`/`result=denied`
         rows without limit. Because the tool's default `limit` is 20 and these
         rows are the newest, a burst of denials **can push transition rows out
         of the default newest-20 window** — the earlier "low-volume and cannot
         evict the transition history" reassurance was wrong. The real
         mitigations are: (a) allowed reads still emit nothing, so *ordinary*
         usage adds no rows; (b) the render tags them `event_type=AccessScopeResolved`
         `result=denied` so they are visibly distinct from state changes; and
         (c) `event_type=MemoryTransitionCommitted` narrowing (or `since`/`until`)
         recovers the transition-only history when denials dominate. The scope
         header and note do NOT promise a denial-free window.

    It does NOT return events recorded under a DIFFERENT target identity:
    - the **RESULT side of a direct two-memory merge** — that merge is
      committed once with `target_id=<source>` (`memory=source`,
      `result_memory=result`, `transitions.py:2065-2068`), so on the result
      memory's trace the merge that produced its current body is invisible.
      (This is distinct from a candidate-merge-into-a-memory, which IS
      recorded on that memory and shows up above.)
    - the **WINNER (result) side of a candidate supersession that CREATES a new
      winner memory (round-15 finding 2).** The `SUPERSEDE_MEMORY` curation
      outcome creates the winner via `_create_candidate_memory` but commits its
      SOLE audit event with `memory=loser` (`target_id=str(loser.id)`,
      `transition_type=SUPERSEDE`), only advancing the new winner's pointer to
      that same transition row — no audit row is written against the winner
      (`transitions.py:2136-2181`, single `_commit_transition` at `:2163`). So
      the newly-created winner's trace shows NO supersede event and reads empty
      until a later transition touches it. (Contrast the loser/source, whose
      trace DOES carry the supersede, and the candidate-merge-into-an-existing
      memory REVISE path `transitions.py:1961-1976`, which commits with
      `memory=result_memory=<this memory>` and shows up above.) The direct
      `SupersedeMemories` result side is the same class of gap and is the
      first bullet.
    - **confidence decay** — emitted with NO `target_id`, memory ids only in
      `metadata.memory_ids` (`memory/confidence_decay.py:84-101`);
    - **link add/remove** — `target_type='memory_link'`, `target_id=<link
      uuid>`, memory id only in `metadata.memory_id`
      (`memory/services.py:1059-1082,1170-1192`).

    These cross-identity events are OUT OF SCOPE for v1 (a complete trace
    needs metadata-JSON matching + an index and is deferred, see Out of
    Scope). The tool description and render header state the scope plainly so
    the agent does not read "no events" as "nothing ever happened."
  - Needs a resolvable `project_id` (evidence §4) AND a key that can resolve
    it (evidence §5). If only `repository_url` is available, render a friendly
    "this tool needs project_id" message (no call).
  - Needs `audit:read`; on `403 missing_capability` render a friendly
    message telling the operator to re-issue the key with `audit:read`. On
    `403 project_scope_denied` (unbound non-agent key) render the
    project-scope message from Error Handling.

**CLI parity**: `engram memory get <memory_id>` and `engram audit`, mirroring
the tool logic (existing pattern: `run_memory_links` at `commands.py:2182`,
`run_observations` at `commands.py:2224`).

**Tool descriptions**: these are reference/read tools, NOT steps in the
numbered capture→corroborate→close-the-loop workflow. Only three of the
existing six carry a "Step N" prefix — `engram_search` (Step 1),
`engram_observations` (Step 2), `engram_memory_feedback` (Step 3)
(`mcp_server.py:82,150,185`); `engram_context`, `engram_memory_link`,
`engram_memory_version` are unnumbered reference tools. The two new tools
follow that unnumbered reference style (do NOT invent new step numbers,
which would collide with the workflow sequence). Exact strings (asserted by
the description test, order-item 1a):
- `engram_memory_get` = `'Read one memory in full by memory_id — the complete
  untruncated current body, version history, and links, not the 400-char
  session-start preview. Use before revising, linking, or giving feedback so you
  act on the full stored text. Kind, confidence, and conflict/stale/refuted
  validity come from engram_search, not this tool.'`
- `engram_audit` = `'Show a memory\'s own recorded audit events — every
  transition committed against it (promotion, revise, refute, stale, restore,
  supersede, archive, a candidate merged into it, and a merge where it is the
  source), most recent first. Use to explain why a memory is in its current
  state. Not returned: the winner side of a supersession (a direct merge is
  recorded under the source memory; a candidate supersession that creates a new
  winner is recorded under the superseded loser), confidence-decay, and link
  add/remove events — those are keyed to a different audit target.'`

### Backend Prerequisites (required, small, guarded by tests)

The read paths this slice surfaces have real gaps that no client code can
close. P1/P2a mirror guards that already exist elsewhere in the same views
(`ensure_memory_team_scope`, `digest_visibility_failure`/`_quarantine`); P2c
scopes the audit `target_display` title lookup; P3 adds two optional filters,
P3-index adds one composite filter/order index, and P5 adds a whitelisted
`ordering` param to the audit list.

- **P1 — team-scope whitelist guard on `MemoryVersionView.get`,
  `MemoryLinksView.get`, AND the diff path
  (`ResolveMemoryDiff.execute` / `MemoryDiffView.get`).** All three are surfaced
  by `engram_memory_get` and MUST fail closed to the SAME whitelist (teamlead
  decision S2, 2026-07-20: version/links/diff "each hardened to fail-closed team
  scope"). The version/links GETs currently filter only by
  `organization_id`/`project_id`/`memory_id` (`memory/views.py:126-144`,
  `196-224`) and never enforce the memory's team, so a team-bound key
  (`scope.team_ids`, `access/services.py:344-374`) can read another team's
  `visibility_scope=TEAM` memory body/versions and links. The diff path DOES call
  `ensure_memory_team_scope(memory, scope)` (`memory/services.py:942`), but that
  shared helper is the FAIL-OPEN shortcut (`services.py:838-844`) — it denies
  only `TEAM` with a non-null unauthorized team and therefore ADMITS
  `TEAM`/null-team, `SESSION`, and `ORGANIZATION` (the whitelist subrule below),
  so the diff `body` leaks exactly the schema-valid fall-through the version/links
  GETs do. Add/upgrade the guard so all three paths raise `team_scope_denied`
  (403) unless the memory is admissible under retrieval's visibility whitelist —
  see the whitelist subrule below for the exact predicate (`PROJECT` or authorized
  `TEAM` only; `SESSION`/`ORGANIZATION`/null-team/foreign `TEAM` all denied), which
  is stricter than the shared `ensure_memory_team_scope` TEAM-only deny. For
  version/links, fetch the `Memory` row after resolving scope
  (`MemoryLinksView.get` currently has no `Memory` fetch, so add one); for diff,
  REPLACE the `ensure_memory_team_scope(memory, scope)` call at
  `memory/services.py:942` with the whitelist predicate.
  - **The P1 guard MUST also fail CLOSED when the scoped parent memory does not
    exist (round-12 finding 2).** After the
    `Memory.objects.filter(organization_id=scope.organization_id,
    project_id=project.id, id=memory_id).first()` lookup, a `None` result means
    no memory with that id lives in the caller's org/project. But
    `MemoryVersion`/`MemoryLink` parent-scope consistency is
    `clean()`-only, with NO database constraint (`core/models.py:793,809-812`
    constrains only `(memory, version)`; `818`), so a child row whose OWN
    `organization_id`/`project_id` equals the caller scope while its parent
    `Memory` lives in a FOREIGN project is a reachable state. The current
    `MemoryVersionView.get` runs its child query
    (`MemoryVersion.objects.filter(org, project, memory_id)`,
    `memory/views.py:134-140`) and returns those rows EVEN WHEN the parent lookup
    returned `None` — the guard `if memory is not None and
    digest_visibility_failure(...)` short-circuits on `None` and falls through
    (`memory/views.py:131-132`), so such a mis-projected child body/link would
    leak, breaking P1's invariant that a memory not in the caller's scope can
    never be read through these GETs. Fix: in BOTH GET handlers, when the parent fetch returns
    `None`, return the empty quarantine shape `{'count': 0, 'items': []}`
    (HTTP 200 — identical to the existing digest-visibility branch and to what
    the client already treats as "not found or not yet visible", Data Flow
    step 3) BEFORE issuing any child version/link query — i.e. widen the existing
    guard to `if memory is None or digest_visibility_failure(memory) is not
    None:`. Order the handler: fetch parent → `None` → empty → team-scope guard →
    child query. `MemoryLinksView.get` (which currently has NO parent fetch at
    all and runs its link query unconditionally, `memory/views.py:214-220`) gains
    the same fetch-then-guard so a mis-projected link row cannot be served for a
    foreign parent. Test 16 is extended with a mis-projected `MemoryVersion` and
    `MemoryLink` row (own `project` = caller scope, parent `Memory` in another
    project) asserting BOTH GETs return the empty shape, never the foreign child.
  - **The P1 guard MUST be a WHITELIST that admits ONLY what retrieval admits —
    fail CLOSED on every other `visibility_scope`, including `TEAM, team_id IS
    NULL`, `SESSION`, and `ORGANIZATION` (round-11 finding 1; round-15 finding
    1).** Retrieval's team-visibility rule
    (`filter_documents_by_team_visibility`, `context/services.py:287-290`) is a
    WHITELIST: it admits a document ONLY when
    `visibility_scope == PROJECT`, OR `visibility_scope == TEAM and team_id ∈
    scope.team_ids`; every other value — `SESSION`, `ORGANIZATION`, and `TEAM`
    with an unauthorized or NULL team — is REJECTED (a null `team_id` is in no
    key's `team_ids`; `SESSION`/`ORGANIZATION` never match either branch). The
    shared `ensure_memory_team_scope` (`memory/services.py:838-844`) is NOT that
    predicate: it denies ONLY when `visibility_scope == TEAM and team_id is not
    None and team_id ∉ scope.team_ids`, so it fails OPEN for `TEAM`/null-team AND
    for `SESSION`/`ORGANIZATION` rows — every one of which a
    project-authorized key would then read in full via version/links while
    retrieval rejects it. `VisibilityScope` genuinely permits all four values
    (`core/models.py:52-56`) and `Memory.visibility_scope`/`Memory.team` carry NO
    DB constraint tying them (`core/models.py:716,720,761`), so version/links must
    not depend on the fail-open shortcut nor on a mere `TEAM`-only deny. P1
    therefore applies retrieval's whitelist byte-for-byte: **serve the
    version/links read ONLY when `memory.visibility_scope ==
    VisibilityScope.PROJECT`, OR (`memory.visibility_scope ==
    VisibilityScope.TEAM and memory.team_id in scope.team_ids`); raise
    `team_scope_denied` (403) for EVERY other value** — `SESSION`,
    `ORGANIZATION`, `TEAM`/null-team, and `TEAM` with an unauthorized team. (No
    live promotion path creates `SESSION`/`ORGANIZATION` memories — curation
    requires PROJECT or TEAM, `core/models.py:2913-2914` — and retrieval never
    injects them, so denying them via version/links is consistent and loses no
    reachable read; the whitelist simply closes the schema-valid fall-through
    rather than trusting an unenforced invariant.) (The shared
    `ensure_memory_team_scope`'s fail-open shortcut on the POST WRITE paths is
    unchanged by this slice; tightening it org-wide is a noted future hardening,
    Out of Scope here. The DIFF read path, however, IS surfaced by
    `engram_memory_get`, so per teamlead decision S2 it is brought under this same
    whitelist here — this slice fixes the three read paths it surfaces: version,
    links, and diff.) Test 16 is extended with `visibility_scope=TEAM,
    team_id=NULL`, `visibility_scope=SESSION`, and `visibility_scope=ORGANIZATION`
    cases, each asserting version, links, AND diff return 403 `team_scope_denied`
    for a project-scoped key, plus a `visibility_scope=PROJECT` and an
    authorized-`TEAM` case asserting 200.
  - **The diff path's per-version lookup MUST ALSO constrain to the parent
    memory's own org/project (round-12 finding 1, re-confirmed — this is
    steady-state disclosure, NOT waived by the operator directive).**
    `ResolveMemoryDiff._get_version` resolves each side with
    `MemoryVersion.objects.filter(memory=memory, version=version_number)`
    (`memory/services.py:951-956`) — NO `organization_id`/`project_id` term.
    `MemoryVersion` parent-scope consistency is `clean()`-only, the only DB
    constraint being `(memory, version)` uniqueness
    (`core/models.py:809-812,818`), so a `MemoryVersion` row attached to an
    in-scope memory but carrying a FOREIGN `organization_id`/`project_id` (a
    schema-valid, P7-tracked invariant-violation state,
    `memory/invariant_queries.py:1156`) would have its `body` disclosed through
    the diff addendum this slice newly surfaces — a cross-scope read the
    team-scope whitelist above does NOT catch (the parent memory is in scope; the
    mis-projected CHILD is not). This is a steady-state disclosure defect, not a
    backward-compat / deployment-choreography concern, so the operator directive
    does not waive it. Constrain the `_get_version` queryset to the parent
    memory's own `organization_id`/`project_id` (equal to the request scope, since
    `ResolveMemoryDiff.execute` already fetches `memory` by org/project at
    `:934-940`), so a mis-projected version 404s as `version_not_found` (rendered
    as the addendum's "diff unavailable" note) instead of leaking its body. Test
    16 is extended with a mis-projected `MemoryVersion` (own scope = caller,
    parent memory in caller scope but the version's own `project` in ANOTHER
    project) asserting the diff side 404s, never the foreign body.

- **P2 — digest-visibility quarantine on the surfaced read paths (two places).**
  The quarantine that `MemoryVersionView.get` applies (returns
  `{'count': 0, 'items': []}` when `digest_visibility_failure(memory)` is set,
  `memory/views.py:131-132`) is missing from the links GET, and the audit
  `target_display` title lookup is unscoped. The two paths leak DIFFERENT data —
  the links path exposes the hidden memory's own link records
  (`link_type`/`target`/`label`), while the audit-target path exposes OTHER
  memories' titles — so each needs its own guard. (The inspection `related[]`
  path — the former P2b — is inspection-DETAIL-derived and is no longer surfaced
  by `engram_memory_get`; it is removed from this slice with the rest of the
  inspection-detail hardening, teamlead decision S2.) The two guards:
  - **P2a — `MemoryLinksView.get`.** `_link_response` returns
    `link_id`/`link_type`/`target`/`label`/`created_at` (`memory/views.py:303-310`)
    — it does NOT resolve any linked memory's title (finding 17 corrects the
    earlier "renders another memory's title" claim). The leak here is that an
    unproven digest's OWN link records (its `target`/`label` strings) are
    served at all. Add the identical `digest_visibility_failure(memory)` check
    (return empty `items`) so a hidden digest exposes no link data. This makes
    the client's empty-version early return (Data Flow step 3) defense-in-depth
    rather than the sole protection.
  - **P2c — audit target-display (`_resolve_memory_targets`,
    `inspection/views.py:453-463`).** This resolves memory titles for the
    audit `target_display` field filtered by `organization_id` ONLY — it is
    NOT scoped to the request's project/team and applies NO quarantine. Two
    disclosures follow, and BOTH must be closed (excluding unproven digests
    alone is insufficient — finding 1):
    1. **Cross-scope title leak.** `AuditEvent.target_type`/`target_id` are
       unconstrained `CharField`s (`core/models.py:1064-1065`); an event that
       is IN the caller's project (so it survives the project filter at
       `inspection/services.py:255`) may carry a `target_id` pointing at a
       memory in a DIFFERENT project or team. The org-wide title lookup would
       resolve and expose that out-of-scope memory's title. The memory-target
       title lookup MUST therefore be constrained to the SAME effective scope
       under which the caller could read that memory directly via
       `engram_memory_get` — `organization_id` +
       `project=inspection_scope.project` + the P1 visibility WHITELIST
       `Q(visibility_scope=PROJECT) | Q(visibility_scope=TEAM,
       team_id__in=scope.team_ids)`. It MUST NOT use the looser inspection
       `team_filter` (`team__isnull OR team_id__in=scope.team_ids`,
       `inspection/services.py:52-53`): that predicate ignores `visibility_scope`
       and admits `TEAM`/null-team, `SESSION`, and `ORGANIZATION` rows (any row
       with a null team) whose DIRECT version/links read P1 DENIES (finding 3), so
       constraining P2c by `team_filter` would still disclose the title of a
       memory the caller cannot read. Only the P1 whitelist makes true the claim
       that "a title is resolved only for a memory the caller could already read
       directly." Out-of-scope ids fall back to the raw `target_id`.
    2. **Unproven-digest title leak (same scope).** Even in-scope, an unproven
       digest's title must not appear; also exclude
       `unproven_digest_memory_ids` (scoped to that same org/project/team).
       Excluded rows fall back to the raw `target_id`.

    (Project/team/identity target-name resolution — `_resolve_project_targets`
    `:466`, `_resolve_team_targets` `:479`, `_resolve_identity_targets` `:492`
    — stay org-scoped: those are organization-level configuration names, not
    per-memory content, and are already broadly visible to any inspection
    caller in the org. Only the memory path exposes memory bodies/titles and is
    the one this guard tightens; widening the other three to project/team scope
    is a noted future hardening, Out of Scope for this slice.)

- **P3 — `target_id` + `target_type` filter on
  `InspectionAuditEventFilterSet`.** Add
  `target_id = django_filters.CharFilter(field_name='target_id')` AND
  `target_type = django_filters.CharFilter(field_name='target_type')` to
  `inspection/filters.py:32-38`, expose both on `InspectionQuerySerializer`
  (`inspection/serializers.py:8-21`, each a `CharField(required=False,
  allow_blank=True, default=None)`), and thread them through the inspection
  scope into the audit filter data (`inspection/services.py:265-270`).
  `target_id` alone is NOT an identity (evidence in the `engram_audit` design
  note: `target_type`/`target_id` are independent columns keyed as a pair at
  `inspection/views.py:385`), so the tool always sends `target_type='memory'`
  alongside a `memory_id`. This is what lets `engram_audit` scope to a single
  memory instead of returning the first N project-wide events.

- **P3-index — composite filter/order index + migration (corrects the earlier
  "no migration" claim).** `AuditEvent` has no index containing `target_id`;
  its indexes cover `(org, project, event_type)`, `(org, result)`,
  `(org, created_at)`, `(org, project, created_at)`
  (`core/models.py:1072-1084`). The audit view runs `qs.count()` before
  slicing (`inspection/views.py:158`), so a per-target filter without an
  index scans the whole project audit log (twice, count + page) — unsafe on a
  large project. Add a composite index
  `models.Index(fields=['organization', 'project', 'target_type',
  'target_id', 'created_at'])`. This is a filtering/ordering index over the
  new predicate, **not** a covering index (the row payload is not included);
  the earlier "covering index" wording was wrong. Ship it the way the repo
  actually ships audit indexes — an ordinary `migrations.AddIndex` (that is
  what the existing audit indexes use, `core/migrations/0020_*.py:31-61`; the
  repo has NO `AddIndexConcurrently` precedent, so the earlier "shipped
  concurrently, consistent with repo practice" claim was false). Two hard
  requirements:
  1. **Declare it in `AuditEvent.Meta.indexes`** (`core/models.py:1072-1084`)
     AND generate the migration from that model state, so migration state and
     model state stay in sync (a bare `AddIndex` operation with no `Meta`
     change drifts and `makemigrations --check` would flag it).
  2. A plain `AddIndex` takes a write lock while building; this matches the
     existing audit-index migrations and is acceptable for this slice. (If the
     production audit table is later judged too large for an in-transaction
     build, switching to `AddIndexConcurrently` is a mechanical follow-up — but
     it then REQUIRES `Migration.atomic = False`, the pattern the repo already
     uses when it needs it, `core/migrations/0033_*.py:120`. This slice does
     not need it.)

  This is the ONLY schema change in the slice. Test 18 asserts the index is
  present in the migrated schema (see Backend tests).

- **P5 — audit-events inspection list honors a whitelisted `ordering` param.**
  `ListInspectionAuditEvents.execute` hardcodes `.order_by('created_at', 'id')`
  (`inspection/services.py:263`), so the audit list can only return events
  oldest-first — a client `limit` slice would drop the newest (current-state)
  events. `InspectionQuerySerializer` ALREADY carries an `ordering` field
  (`serializers.py:16`) threaded into `InspectionScope`
  (`services.py:44`, set at `views.py:71`); the memory and context-bundle lists
  already honor it through a whitelist + `_ordering` helper
  (`MEMORY_ORDERING_FIELDS = ('created_at', '-created_at')` +
  `_ordering`, `inspection/services.py:56,76-78`), so NO serializer/scope change
  is needed — only the audit list must adopt the same pattern. Mirror it:
  add `AUDIT_ORDERING_FIELDS = ('created_at', '-created_at')` with default
  `'created_at'`, and an `_ordering(ordering)` method on
  `ListInspectionAuditEvents` that returns the requested value when whitelisted
  and the `'created_at'` default otherwise (default UNCHANGED, so console callers
  that pass no `ordering` keep today's oldest-first behavior). Replace the
  hardcoded `.order_by('created_at', 'id')` (`inspection/services.py:263`) with
  `.order_by(ordering, id-tiebreaker)`, where the `id` tiebreaker sign MATCHES
  the ordering direction (`'id'` for ascending `created_at`, `'-id'` for
  descending `-created_at`) so equal-`created_at` rows sort stably in the same
  direction as the primary key. This is the whole backend change `engram_audit`
  needs: it sends `ordering=-created_at` and the endpoint returns the newest
  `limit` rows first. There is deliberately NO count/slice windowing, NO `offset`
  math, and NO isolation/snapshot envelope — the tool makes ONE request. The
  existing two-statement `count()`+slice in the view (`views.py:158,161`) is left
  as-is; the `count` it reports feeds only the "more exist" truncation note,
  where an off-by-few skew under an in-flight write is an accepted, immaterial
  risk (Design / truncation note). Backend test 19 covers it.

These are additive: P1/P2a are stricter guards that only deny cross-team /
unproven-digest disclosure on the version/links/diff reads (P1 applies the same
visibility whitelist to all three, and constrains the diff per-version lookup to
the parent memory's org/project); P2c scopes the audit `target_display` title
lookup to the request's org/project + P1 visibility whitelist; P3 adds two
optional audit filters; P3-index adds one composite filter/order index; P5 adds
a whitelisted `ordering` param to the audit list so it can return newest-first
(default `created_at` unchanged, so no behavior change for existing console
callers). Each ships with the backend test named in the Test Plan.
`engram_audit` reaches the newest events by making ONE request with
`ordering=-created_at` and `limit` and rendering the returned page as-is (newest
first) — a pure client change atop the P5 ordering param, no second request and
no offset paging.

### Alternatives rejected (one line each)

- Add `repository_url` routing to inspection views — out of scope (backend
  change); noted as a future improvement in the brief.
- One merged "inspect" tool doing both memory + audit — conflates two
  capabilities (`memories:read` vs `audit:read`) and two failure modes;
  keep them separate.
- Extend `engram_search` to return full bodies — search is ranked
  multi-result retrieval; full single-record read is a distinct concern.
- Use inspection detail for a rich single-record read — descoped (teamlead
  decision S2); `engram_memory_get` reads only the by-id version/links/diff
  endpoints, and status/confidence/validity come from `engram_search` (slice S3).

## API and Schema Changes

Client additions plus the backend guards in **Backend Prerequisites**: P1
team-scope on the version/links/diff reads (fail-closed on cross-team, null-team,
missing/mis-projected parent, and — on diff — a mis-projected per-version body),
P2a digest quarantine on the links GET, P2c
scoping of the audit `target_display` title lookup, P3 additive `target_id` +
`target_type` audit filters, **P3-index — one composite index + migration** so
the new per-target filter is production-safe, and P5 — a whitelisted `ordering`
param on the audit list (`created_at` / `-created_at`, default `created_at`
unchanged) so `engram_audit` fetches newest-first in ONE request
(`ordering=-created_at`). P1/P2a/P2c add/adjust authorization checks only; P3
filters existing columns; P5 adds the audit-list `ordering` param (default
unchanged, so no behavior change for existing console callers); P3-index is the
single schema/migration change in the slice (an ordinary `migrations.AddIndex`
declared in `AuditEvent.Meta.indexes`, no data migration, no column change).

### `engram_memory_get` MCP tool

`inputSchema` (added to `mcp_server.list_tools`, `mcp_server.py:79-202`):

```json
{
  "type": "object",
  "properties": {
    "memory_id": {"type": "string"},
    "project_id": {"type": "string"},
    "from_version": {"type": "integer"},
    "to_version": {"type": "integer"}
  },
  "required": ["memory_id"]
}
```

Handler `memory_get(arguments, config_dir, transport)` in `mcp_tools.py`,
registered in `build_tools` (`mcp_tools.py:125-132`) as
`'engram_memory_get': bind(memory_get)`.

Requests issued (via `get_json`, `packages/cli/engram_cli/http.py:213`):

- Body + versions: `GET {server}/v1/memories/{memory_id}/version` with params
  `project_id=<pid>` OR `repository_url=<url>` [`&team_id`]
- Links: `GET {server}/v1/memories/{memory_id}/links` (same params)
- Diff addendum: `GET {server}/v1/memories/{memory_id}/diff?from_version=<n>&to_version=<m>`
  plus `project_id` OR `repository_url` [`&team_id`]

Note: `/version`, `/links`, `/diff` have no trailing slash. There is NO
inspection-detail request — `engram_memory_get` never calls
`/v1/inspection/memories/<id>` (teamlead decision S2).

**Render** (exact template — one code path, no primary/fallback branch):

```
memory_id=<id> current_version=<items[0].version>
(status, confidence, kind, and conflict/stale/refuted validity come from engram_search, not this tool)

<items[0].body, untruncated>

versions: v<items[0].version> (<created_at>)[, v<items[1].version> (<created_at>)...]
links: <link_type>: <target>[ (<label>)][; ...]
```

The `links:` line renders `<link_type>: <target>` for each link and appends
` (<label>)` after `<target>` ONLY when the link `label` is a non-empty string
(after control-char collapsing, sanitization rule below); when `label` is
empty/None the annotation and its parentheses are omitted entirely (no trailing
` ()`), mirroring the audit render's `actor=<id> (<display>)` convention. This
resolves the earlier template ambiguity: `label` is a rendered, sanitized field
(not silently discarded), so the label-injection guard in test 9b exercises a
value that actually reaches the line.

`items[0]` is the highest-`version` row (queryset `order_by('-version')`,
`memory/views.py:139`) — the current full stored body, defeating the 400-char
session-start truncation. The `/version` endpoint returns EVERY version row
(`memory/views.py:134-144`), so the render MUST emit a `versions:` line listing
ALL returned versions (newest first, matching the endpoint order), not just
`items[0]`. (Test 4 asserts the `versions:` line lists more than one version
when the stub returns several.)

Nullable / omission rules:

- Omit the `links:` line when the links `items` list is empty AND the links
  fetch succeeded (2xx) — a confirmed no-links record. If the links GET returned
  a non-2xx status, emit the `links: unavailable (HTTP <status>) — ...` warning
  line instead of omitting it (finding 2 / Error Handling).
- **Single-line-field sanitization.** The `links:` line interpolates each
  link's `target`/`label` (secret-redacted upstream via `redact_value`,
  `memory/views.py:307-308`, but NOT newline-escaped) and `link_type` (a bounded
  `LinkType` enum, `core/models.py:1099-1106`, no free text). Redaction does not
  strip control characters, so collapse `\r`, `\n`, other C0 controls, and `\t`
  to a single space in each interpolated `target`/`label` before rendering, so a
  newline in a link target cannot forge an extra `links:`/`versions:` line. The
  untruncated BODY block is exempt (it is intentionally multi-line, delimited by
  blank lines). Covered by test 9b's shared render helper.

**Diff addendum** (appended when both versions given and diff succeeds):

```

diff v<from.version> -> v<to.version>
--- v<from.version> (<from.created_at>)
<from.body>
--- v<to.version> (<to.created_at>)
<to.body>
```

### `engram_audit` MCP tool

`inputSchema`:

```json
{
  "type": "object",
  "properties": {
    "memory_id": {"type": "string"},
    "target_id": {"type": "string"},
    "target_type": {"type": "string"},
    "event_type": {"type": "string"},
    "correlation_id": {"type": "string"},
    "since": {"type": "string"},
    "until": {"type": "string"},
    "limit": {"type": "integer"},
    "project_id": {"type": "string"}
  },
  "required": []
}
```

`memory_id` and `target_id` both map to the server `target_id` filter (a bare
`memory_id` is the common case; `target_id` allows tracing non-memory
targets). When both are given, `target_id` wins. `target_type` pairs with the
id filter (P3): the tool DEFAULTS `target_type='memory'` when the caller
passed a `memory_id` (or a bare `target_id` with no explicit `target_type`),
and passes an explicit `target_type` through unchanged for non-memory traces.
This prevents id collisions across target types from producing a mixed trace
(evidence: `(target_type, target_id)` pairing at `inspection/views.py:385`).

Handler `audit(arguments, config_dir, transport)`, registered as
`'engram_audit': bind(audit)`.

Request: `GET {server}/v1/inspection/audit-events` with params built as
strings: `project_id=<pid>` (required — from `runtime.project_id`),
`ordering=-created_at` (newest-first, Backend Prerequisite P5),
`limit=<limit or 20>`, `target_id=<target_id or memory_id>` when either is
non-empty, `target_type=<target_type or 'memory'>` whenever the id filter is
set, and each of `event_type`, `correlation_id`, `since`, `until`, `team_id`
only when non-empty. Server enforces `limit` 1..200, default 50
(`inspection/serializers.py:11`); client default 20. The `target_id` /
`target_type` filters are added by Backend Prerequisite P3; the `ordering`
whitelist is added by P5.

**Single newest-first request.** Because Backend Prerequisite P5 adds a
whitelisted `ordering` param, the handler issues ONE request with
`ordering=-created_at` (plus `limit` and the filters above). The endpoint
returns the newest `limit` events first with a stable id tiebreaker, so the
handler renders `items` in the order received (newest at top), matching the
"most recent first" description. There is NO second request, NO `offset`, and NO
count/slice windowing.

Then, when the response exposes `count` and `count > limit` (older rows really
are omitted), the handler appends one final note line
`(showing most recent <limit> of <count> events; <count - limit> older
omitted — narrow with since/until/event_type)`. **Accepted risk:** the endpoint
reads `count` and the page in two separate statements
(`inspection/views.py:158,161`), so under a concurrent write the reported
`count` may skew by a few relative to the rendered page — immaterial for a note
that only signals "more exist".

`since`/`until` pass through as ISO-8601 strings (server field is
`DateTimeField`, `inspection/serializers.py:20-21`).

**Render** (one line per event, from `audit_event_response`,
`inspection/views.py:373-405`). Many state changes (revise / refute / stale /
restore) share the generic `event_type=MemoryTransitionCommitted`
(`memory/transitions.py:376,1248`); the specific transition and its reason live
in `metadata.transition_type` / `metadata.reason` (returned at
`inspection/views.py:402`). The render MUST surface them so the line explains
*what changed and why*:

```
<created_at> <event_type>[ (<metadata.transition_type>)] actor=<actor_id>[ (<actor_display>)] result=<result> target=<target_id>[ (<target_display>)] target_type=<target_type> capability=<capability>[ reason=<metadata.reason>]
```

- **Always emit `target_type=<target_type>` (round-5 finding 2).** The API
  returns `target_type` and `target_id` as two independent fields
  (`inspection/views.py:395`), and `target=<target_display or target_id>` alone
  is ambiguous: two rows whose ids collide across types, or a target whose
  display is intentionally suppressed (raw-id fallback), render identically. In
  a memory-only trace every row is `target_type=memory`, but a project-wide
  read (no id filter) mixes `memory` / `memory_link` / `project` / `team`
  targets and the type is the only disambiguator. Emit it unconditionally so
  the audit evidence is never ambiguous; it is one of the sanitized fields
  below.
- **Always anchor `actor=` and `target=` on the stable ID, with the display as
  an optional annotation (round-8 finding 3).** `audit_event_response` returns
  BOTH the id AND the display for actor and target
  (`actor_id`+`actor_display`, `target_id`+`target_display`,
  `inspection/views.py:387-405`). The displays are NOT unique identities:
  `_batch_resolve_actor_names` maps every API key to its
  `owner_identity.display_name` (`inspection/views.py:421`), so N distinct keys
  owned by one identity all render the SAME `actor_display`; and memory titles
  are non-unique (`Memory.title` is a plain `CharField`, `core/models.py:717`),
  so two different memories in a project-wide read can share one
  `target_display`. Rendering `actor=<actor_display or actor_id>` /
  `target=<target_display or target_id>` — dropping the id the moment a display
  resolves — therefore collapses distinct actors/targets into one indist­inguishable
  line and destroys the forensic attribution the tool exists to provide. So the
  render ALWAYS prints the id (`actor=<actor_id>`, `target=<target_id>`) and
  appends the human-readable display in parentheses only when it resolved
  (`actor=<actor_id> (<actor_display>)`), never substituting one for the other.
  Both `actor_id`/`actor_display` and `target_id`/`target_display` are in the
  sanitized-field list below. (In a `target_type=memory` trace every row shares
  the same target id — harmless — but the actor id is the disambiguator there,
  and in a project-wide read the target id is load-bearing too.) When a display
  is absent (raw-id fallback), only `actor=<actor_id>` / `target=<target_id>`
  is emitted (no empty `()`).
- Append ` (<transition_type>)` after `<event_type>` only when
  `metadata.transition_type` is a non-empty string.
- Append ` reason=<reason>` only when `metadata.reason` is a non-empty string.
- Omit both suffixes for events whose metadata lacks those keys.
- **One physical line per event (spoofing guard).** NONE of the interpolated
  fields is newline-escaped upstream, and the "redacted" ones are only
  secret-substituted, not control-char-stripped:
  - `reason` is `redact_value` + `[:1024]` only (`memory/transitions.py:379-386`);
  - `actor_id`, `target_id`, `capability`, `request_id`, `correlation_id`,
    `event_type` and the `metadata.*` values pass through `redacted_text` /
    `redacted` (`inspection/views.py:391-402`), which runs
    `core/redaction.py:62-74` — that substitutes secrets **without touching
    control characters**;
  - **`actor_display` and `target_display` are NOT redacted at all for the
    fallback-bearing cases** — API-key owner / identity display names
    (`inspection/views.py:421,430`) and project/team/identity target names
    (`:476,489,505`) are returned RAW; only memory `target_display` is
    `redacted_text` (`:463`). So "redacted/truncated" was an overstatement:
    display names are raw operator-set strings.

  A literal newline in ANY of these — including the `actor_id`/`target_id`
  fallbacks the render substitutes when a display name is absent, and the
  `metadata.transition_type` suffix — would render as several apparent audit
  records. Before interpolation the render MUST collapse control characters
  (replace `\r\n`, `\n`, `\r`, other C0 controls incl. `\t`, with a single
  space, or `\n` → `\\n` visible escape) in **every** interpolated field on
  the line: `event_type`, `transition_type`, `actor_display`, `actor_id`,
  `result`, `target_display`, `target_id`, `target_type`, `capability`, and
  `reason`. So each event is exactly one line regardless of which fallback
  fires. Covered by test 9b (which injects newlines into `reason`, `actor_id`,
  `target_id`, and `target_type`).
  Secret-pattern redaction of the raw display names is a pre-existing,
  org-wide inspection behavior shared with the console (which renders the same
  fields) and is Out of Scope for this client slice — the higher-risk
  agent/system-supplied field, `reason`, is already secret-redacted upstream.
- The render is preceded by a one-line scope header. The header is
  **conditional on what was actually requested** (the schema permits a
  memory trace, a non-memory-target trace, and a project-wide read — finding
  4 / finding 5):
  - id filter set with `target_type='memory'` (the common `memory_id` case):
    `audit trace for memory <id> (own events only: promotion/revise/refute/stale/restore/supersede/archive/candidate-merge-in/merge-as-source; result-side-of-direct-merge, decay, and link events not shown)`;
  - id filter set with a non-memory `target_type`:
    `audit trace for <target_type> <id>`;
  - no id filter (project-wide read): `project-wide audit events (most recent
    <limit>)` — no per-subject "own events" claim.

  so an empty or short list is not misread as a complete lifecycle, and a
  project-wide or non-memory read is never mislabeled as a memory trace.
- **The scope header is caller-controlled and MUST be sanitized too (round-5
  finding 3).** The two non-project-wide header forms interpolate the caller's
  `target_id` and `target_type` arguments (`<id>`, `<target_type>`), which are
  unrestricted request strings backed by unconstrained `CharField`s
  (`core/models.py:1064-1065`) — a newline in either would forge apparent
  output lines above the events (and would fire even when zero events match,
  where the per-event guard never runs). The header MUST run the SAME
  control-character collapse as the event line over its interpolated `<id>` and
  `<target_type>` before emitting. Test 9b is extended to inject a newline into
  the `target_id`/`target_type` HEADER arguments (not only into returned
  event/body fields) and assert the header stays one physical line.

### CLI parity

New argparse subcommands in `packages/cli/engram_cli/main.py`
(`build_parser`, near the `memory`/`observations` parsers, `main.py:171-204`)
and dispatch (`main.py:60-71`):

- `engram memory get <memory_id> [--project P] [--from-version N] [--to-version M] [--config-dir DIR]`
  → `run_memory_get` (new, in `commands.py`).
  - `--from-version` / `--to-version` map to `dest='from_version'` /
    `dest='to_version'`, `type=int`, `default=0` (0 = unset).
- `engram audit [--memory-id M] [--target-id T] [--target-type TT] [--event-type T] [--correlation-id C] [--since S] [--until U] [--limit N] [--project P] [--config-dir DIR]`
  → `run_audit` (new). `--limit type=int default=20`. `--memory-id` /
  `--target-id` map to the server `target_id` filter (target wins when both).
  `--target-type` maps to the server `target_type` filter; it DEFAULTS to
  `memory` whenever an id filter is set (matching the tool), and an explicit
  `--target-type` is passed through unchanged so the CLI can express the same
  non-memory trace the MCP tool advertises (finding 5). Without it the CLI
  could only ever trace `memory` targets, breaking parity with the tool.

CLI scope resolution reuses `_load_cli_scope` (`commands.py:2075`) +
`_resolve_repository_scope` (`commands.py:1977`) for `memory get`
(project OR repo), and `_require_repository_scope`-style logic for `audit`
but requiring a **`project_id` specifically** (repo-only → friendly error,
mirroring the tool).

**Shared logic + render (no divergence — finding 9).** The MCP handlers
(`memory_get`/`audit` in `mcp_tools.py`) and the CLI handlers
(`run_memory_get`/`run_audit` in `commands.py`) MUST NOT re-implement the
request-building, null-token/newline sanitizing, and line rendering. Factor those into pure,
transport-agnostic helpers (input dict + fetched JSON → rendered text /
structured result) that BOTH surfaces call, so behavior cannot drift. The
only surface-specific part is the outer contract. MCP returns friendly text for
every branch (`mcp_tools.py:400-407`). The CLI follows the **established
`run_*` pattern**: the shared helper (and inner scope/HTTP logic) RAISE
`CliError` on hard failures, but the public `run_memory_get` / `run_audit`
wrappers WRAP their body in `try/except CliError: emit_error(stderr, ...);
return 1`, exactly like `run_memory_links` (`commands.py:2218-2221`) —
`main.py` has NO outer `CliError` catch (only `SystemExit`, `main.py:39`), so a
wrapper that let `CliError` escape would leak a traceback instead of exiting 1.
The CLI tests therefore assert the wrapper's **return code (`== 1`) and stderr
text**, NOT that `run_memory_get`/`run_audit` themselves raise (finding 10).
Success paths write rendered text to stdout and return 0. The Test Plan asserts
BOTH surfaces against the same shared helper (tests 3–12 cover the helper via
MCP; tests 13/13a–13h cover the CLI wrappers' version/links read, diff
(`--from-version`/`--to-version`, test 13g), not-found (version-empty),
missing-project, capability-denial, project-scope-denial (test 13h), and
alias/target-type mapping — each asserting exit code + stream contents, not a
raised exception).

### Bundle byte-sync (mandatory)

`mcp_tools.py`, `mcp_server.py`, `commands.py`, `main.py` are byte-synced into
both plugin bundles (`packages/claude-plugin/hooks/engram_cli/`,
`packages/codex-plugin/hooks/engram_cli/`) by
`scripts/sync_plugin_bundle.py` (`SOURCE_DIR`/`BUNDLE_DIRS`, script lines
9-14). Implementation checklist MUST run:

1. `python scripts/sync_plugin_bundle.py`
2. `python scripts/sync_plugin_bundle.py --check`
3. Confirm `packages/claude-plugin/bundle_sync_tests.py` and
   `packages/codex-plugin/bundle_sync_tests.py` pass.

## Data Flow

`engram_memory_get`:

1. `_require_runtime_for_arguments(config_dir, arguments)`
   (`mcp_tools.py:97`) resolves `McpRuntime` (server, key, `project_id` OR
   `repository_url`, `team_id`). If `None` → `NOT_CONFIGURED_MESSAGE`.
2. Validate `memory_id` non-empty (else friendly usage string).
3. Version GET (routable by `project_id` OR `repository_url`). If `items` is
   empty → friendly "not found or not yet visible" and **return immediately
   without issuing the links GET** (an empty version response means the memory
   is missing/hidden/unproven; issuing links after it must not leak link
   targets — this early return is the client-side complement to Backend
   Prerequisite P2a, which also quarantines digests in the links view).
   Otherwise take `items[0]` as the current full body.
4. Links GET (best-effort, but a fetch FAILURE must be disclosed — finding 2):
   do not fail the whole tool, but distinguish "no links" from "could not read
   links":
   - **2xx** → render the `links:` line from `items` (omit the line only when
     `items` is genuinely empty — that is a confirmed no-links record);
   - **non-2xx** (403/5xx/etc.) → render an explicit
     `links: unavailable (HTTP <status>) — could not confirm links; this record
     may have links not shown here` warning line INSTEAD of silently dropping
     the `links:` line. A silent omission is indistinguishable from a confirmed
     empty-link record and would let an agent create a duplicate link believing
     it saw the complete record (the tool description promises "links" as part
     of "the complete record"). `run_memory_links` already surfaces non-2xx as
     an error (`commands.py:2200`); this tool downgrades it to a visible warning
     rather than a hard failure so the main body still renders.
   Reached only after a visible record in step 3.
5. If both `from_version` and `to_version` present and each `>= 1`: diff GET;
   append addendum on 200, append the unavailable note on 404 (see Error
   Handling). If only one is given, or either is `< 1`: no diff call.
6. Assemble and return rendered text.

`engram_audit`:

1. `_require_runtime_for_arguments`; if `None` → `NOT_CONFIGURED_MESSAGE`.
2. If `not runtime.project_id` → friendly "needs project_id" message
   (no HTTP call).
3. Build params, including `ordering=-created_at` (P5) and, when the id filter
   is set (P3), `target_id=<target_id or memory_id>` and
   `target_type=<target_type or 'memory'>`; issue the audit-events GET (ONE
   request, no offset/windowing).
4. On 200: read the returned page `items` and (if present) `count`. Render the
   **sanitized conditional scope header** (memory-trace / non-memory-target /
   project-wide per what was requested — Design; the caller `target_id`/`target_type`
   in the header are control-char-collapsed too, finding 3), then one sanitized
   line per item in the order returned (the endpoint sorts newest-first via
   `ordering=-created_at`, P5; empty items → header + "No audit events found.").
   When `count` is exposed and `count > limit`, append the truncation note
   `(showing most recent <limit> of <count> events; <count - limit> older
   omitted ...)`. Accepted risk: `count` and the page come from two statements
   and may skew by in-flight writes — immaterial for a display note.
5. On 403 `missing_capability` → friendly `audit:read` message; on 403
   `project_scope_denied` → friendly project-scope message; on 403
   `team_scope_denied` → friendly team-scope message (Error Handling).

Scope params mirror the existing helpers: `_scope_payload`
(`mcp_tools.py:382`) for project/team, and the `params` pattern in
`list_observations` (`mcp_tools.py:263-269`) for GET query building.

## Error Handling

- **Runtime unresolved** → `NOT_CONFIGURED_MESSAGE` (`mcp_tools.py:18-21`).
- **Missing `memory_id`** → `'engram_memory_get requires memory_id.'`
  (mirrors `mcp_tools.py:225`).
- **Empty version read (the not-found / not-visible path)** —
  `MemoryVersionView` returns `{'count': 0, 'items': []}` (HTTP 200) both for a
  memory that does not exist in the caller's scope (P1 missing-parent
  fail-closed) and for a digest-visibility failure (`memory/views.py:131-132`).
  Empty `items` → terminal friendly
  `'Memory <id> was not found (or not visible with this key/project).'` and NO
  links GET (Data Flow step 3). NOTE: diff is NOT the primary fetch — its 404 is
  handled by the addendum note below, never by the terminal path, so the main
  body still renders.
- **Diff 404** (`memory_not_found` / `version_not_found`,
  `memory/views.py:352-356`) → append a one-line note
  `'(diff unavailable: version <n> or <m> not found)'`; still return the
  main body.
- **Links non-2xx** → render an explicit
  `links: unavailable (HTTP <status>) — could not confirm links; this record may
  have links not shown here` warning line (NOT a silent omission — finding 2),
  and continue rendering the rest of the record. Only a 2xx-with-empty-`items`
  response omits the `links:` line (a confirmed no-links record).
  **Precedence over the 403 terminal rule (round-7 finding 4):** the links GET
  runs its OWN authorization (`memory/views.py:200`), so it can 403 with
  `project_scope_denied`/`team_scope_denied` (P1 adds the team check). The links
  warning rule ALWAYS wins for the links fetch — a links 403 (or any non-2xx)
  degrades to this warning line and is NEVER terminal. The terminal 403 rule
  below governs ONLY the primary version GET and `engram_audit`'s own request.
  This is safe because the version GET and the links GET apply the SAME
  `ensure_memory_team_scope` predicate (P1) — both admit a memory iff
  `visibility_scope=PROJECT` OR (`visibility_scope=TEAM` AND
  `team_id ∈ scope.team_ids`) — so once the version body renders, the caller is
  already authorized for that record and a links-only 403 (rare, an internal
  inconsistency) must not nuke the whole render.
- **`engram_audit` without `project_id`** →
  `'engram_audit needs a project_id — pass project_id or connect a project.'`
- **`engram_audit` 403 `missing_capability`** →
  `'This key cannot read audit events. Re-issue the API key with the
  audit:read capability from the Engram console, then retry.'`
  (Detected via `status == 403 and body.get('code') == 'missing_capability'`;
  evidence §6.)
- **Either tool 403 `project_scope_denied`** (unbound non-agent key — the
  plain wizard key; evidence §5) →
  `'This key cannot resolve project <pid>. Use a project-bound key, or the
  projects:agent key from the Connect-agent modal, then retry.'`
  (Note: `engram install` does NOT issue or augment a key — it consumes the
  `--api-key` you supply and installs plugins, `commands.py:634,643`; the
  `projects:agent` key comes from the Connect-agent modal in the console.
  Finding 15.)
  (Detected via `status == 403 and body.get('code') == 'project_scope_denied'`;
  `code` is present alongside `error_code`, evidence §6.) This is distinct
  from `missing_capability` and must be checked so the operator is not told to
  add `audit:read`/`memories:read` they already hold.
- **Either tool 403 `team_scope_denied`** (a `team_id` was forwarded that the
  key cannot grant — cross-team key or non-team-admin requesting a team; also
  raised by P1 on the version fallback for a team-scoped memory; evidence §5/§6)
  → context-aware remediation: memory_get names the memory
  (`'This key cannot access the team scope of memory <id> ... that memory's
  team'` when no team was forwarded, or names the forwarded `<tid>` when one
  was); audit names the actual scope — `'for project <id>'` on a project-wide
  call, `'for <target_type> <target_id>'` when a target was given (never a
  bare `'for memory .'`).
  (Detected via `status == 403 and body.get('code') == 'team_scope_denied'`,
  checked alongside the other two 403 codes so it is not swallowed by the
  generic `_error_text` path.) This terminal handling applies to the primary
  version GET only — a 403 from the supplementary LINKS GET (P1 also guards it)
  is handled by the links-warning rule above, not here.
- **Other non-200** → `_error_text(status, body)` generic path.
- **`project_not_found` (404)** → `PROJECT_NOT_FOUND_MESSAGE` via existing
  `_error_text` branch (`mcp_tools.py:402-403`).

## Test Plan (TDD — failing test first)

CLI test files follow the existing **unittest.TestCase + `StubTransport`**
convention already in `mcp_tools_tests.py` (`packages/cli/engram_cli/mcp_tools_tests.py:12-28`)
and `mcp_server_tests.py`; new tests are added to those files to match the
in-file style.

**Test commands (verified runners).** `packages/cli` is NOT copied into the
backend image or mounted into the `app` compose service (`docker-compose.yml`
mounts only `./apps/backend:/srv/app`; `apps/backend/Dockerfile:22,30` copies
only `apps/backend`), so the CLI tests cannot run via the backend `app`
service as-is. To honor the repo rule that Python tests run in a container,
not on the host (`CLAUDE.md:157-158`), run the CLI suite inside the SAME
`app` image (it has `python3`; the CLI is stdlib-only for its test path) by
mounting `packages/cli` into a throwaway `app` run:

```
docker compose -p engram-s2 run --rm \
  -v "$(pwd)/packages/cli:/cli" -e PYTHONPATH=/cli -w /cli \
  app python3 -m unittest discover -s /cli -p '*_tests.py' -v
```

If any CLI test path pulls a dependency absent from the `app` image, add a
minimal `cli-test` compose service (same base image + that dep) rather than
falling back to the host. CI runs the byte-equivalent command directly
(`.github/workflows/backend.yml:104`,
`PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p
'*_tests.py' -v`); the container lane above is the local/quickstart-compliant
equivalent and MUST be the one used during development.

The **backend** guards in Backend Prerequisites DO run in the backend
container per the project quickstart:

```
docker compose -p engram-s2 run --rm app pytest -q \
  engram/memory/views_tests.py engram/inspection/filters_tests.py \
  engram/inspection/services_tests.py engram/inspection/views_tests.py
```
(P1/P2a version+links+diff guards in `memory/views_tests.py`; P2c audit
target-display quarantine in `inspection/views_tests.py`; P3 filter in
`inspection/filters_tests.py`; P5 ordering in `inspection/services_tests.py`;
the P3-index migration is exercised by the filter test running against the
migrated schema.)

Introduce a route-aware stub (StubTransport returns one fixed body; the
multi-call tools need per-path bodies):

- Add `RouteStubTransport` to `mcp_tools_tests.py` mapping a substring of the
  URL path → `(status, body)`, recording `calls`, defaulting to `(404, {})`.

Order:

1. **`mcp_server_tests.py`** — assert `list_tools()` now returns **8** tools
   and includes `engram_memory_get` and `engram_audit` with the exact
   `inputSchema` `properties`/`required` above. **This file hard-codes the
   six-tool count in FOUR places (round-9 finding 3 found three; round-13
   finding 6 adds the fourth), ALL of which must move to 8, not just the direct
   `list_tools()` assertion:**
   - the direct tools/list count/names assertion `test_tools_list_returns_all_tools`
     (`mcp_server_tests.py:90-120`);
   - `test_tools_list_all_six_schemas_expose_optional_project_id`
     (`:114-120`, `self.assertEqual(6, len(tools))`) — bump to 8, rename the
     method, and confirm BOTH new tools also expose an OPTIONAL `project_id`
     (they do: `memory_get`/`audit` schemas above carry `project_id` and neither
     lists it in `required`), so the "all expose optional project_id" invariant
     still holds;
   - `test_run_server_handles_ndjson_round_trip` (`:431-455`, the NDJSON
     round-trip assertion `self.assertEqual(6, len(lines[1]["result"]["tools"]))`
     at `:455`) — bump to 8. **This is a FOURTH six-tool assertion the earlier
     draft omitted (round-13 finding 6); it lives in a different NDJSON test than
     `test_run_mcp_serve_wires_build_tools_and_returns_zero` and independently
     red-lights the CLI lane if left at 6;**
   - `test_run_mcp_serve_wires_build_tools_and_returns_zero` (`:489-504`, the
     `run_mcp_serve` integration assertion
     `self.assertEqual(6, len(lines[1]["result"]["tools"]))` at `:504`)
     — bump to 8.
   Leaving ANY of these FOUR at 6 red-lights the CLI unittest lane
   (`.github/workflows/backend.yml:104`). Prefer asserting the two new names are
   present over a bare count where the tool list is already in hand. *Fails first.*
1a. **`mcp_server_tests.py` — descriptions** — assert each new tool's
    `description` equals the exact string in **Tool descriptions** above, and
    that neither begins with `Step ` (they are unnumbered reference tools).
    Prevents the ambiguity of inventing conflicting step numbers.
2. **`mcp_tools_tests.py` — `build_tools`** — assert the returned dict has 8
   keys incl. the two new names. *Fails first.*
3. **`memory_get` render (version + links, project_id config)** — config with
   `project_id`; route stub returns a version body with multiple items
   (v3/v2/v1, `items[0].body` > 400 chars) + a links body with one link
   (`link_type='narrowed_by'` — a REAL `LinkType` value, `core/models.py:1104` —
   WITH a non-empty `label`, plus a second link whose `label` is empty/None);
   assert the render contains the full (untruncated, > 400 char) `items[0].body`,
   a `versions:` line listing ALL returned versions, the `links:` line rendering
   `narrowed_by: <target> (<label>)` for the labeled link and
   `<link_type>: <target>` with NO trailing ` ()` for the label-less link
   (finding 3 — label is a rendered field appended only when present), the
   `engram_search` pointer note, and that **NO** `/v1/inspection/memories/<id>`
   request was issued (`stub.calls` contains no inspection path — teamlead
   decision S2). *Fails first.*
4. **`memory_get` repo-only routing** — config with NO `project_id`, repo url
   stubbed via `mock.patch.object(mcp_tools, 'workspace_repository_url', ...)`
   (pattern at `mcp_tools_tests.py:74-77`); route stub returns a version body
   with **multiple** items (e.g. v3/v2/v1) + links; assert body rendered
   untruncated, a `versions:` line listing **all** returned versions (not just
   `items[0]` — finding 7), the `links:` line, and that the version/links GETs
   carried `repository_url` (no inspection call exists on any path).
5. **`memory_get` diff addendum** — both `from_version`/`to_version` given
   (each `>= 1`); stub returns diff `{'from': ..., 'to': ...}`; assert both
   labeled bodies render and the `/diff?from_version=&to_version=` call was
   issued.
5a. **`memory_get` diff not requested for one-sided/zero/negative args** —
    parametrize `(from_version, to_version)` over `(3, 0)`, `(0, 3)`, `(0, 0)`,
    `(-1, 2)`; assert NO `/diff` call was issued and the base body still
    renders (mirrors backend `min_value=1` on both, `serializers.py:122-124`).
5b. **`memory_get` diff 404 tolerated** — both versions `>= 1`, diff route
    returns `(404, {'code': 'version_not_found'})`; assert the main body still
    renders plus the `'(diff unavailable: ...)'` note, and the tool does not
    error out via `_error_text`.
7. **`memory_get` empty version fallback** — version route returns
   `(200, {'count': 0, 'items': []})`; assert friendly "not found or not yet
   visible" text AND that **no links call was issued** (assert no
   `/links` path in `stub.calls` — the early return from Data Flow step 3
   must prevent link disclosure for a hidden memory).
8. **`memory_get` links failure surfaced, not silent** — links route returns
   `(503, {})` while the version route returns 200; assert main body still renders, the
   tool does not error, AND the output contains the explicit
   `links: unavailable (HTTP 503` warning line (finding 2 — a non-2xx links
   fetch must be DISCLOSED, not silently dropped, so it is distinguishable from
   a confirmed empty-link record). A companion case: links route returns
   `(200, {'count': 0, 'items': []})` → assert NO `links:` line and NO
   `unavailable` warning (a genuine no-links record).
9. **`audit` happy path + target_id/target_type + transition metadata** —
   config with `project_id`; call with `memory_id`; stub returns
   `{'count': 1, 'items': [<audit event with
   event_type='MemoryTransitionCommitted', metadata={'transition_type':
   'refute', 'reason': 'contradicted'}, actor_id='<key-uuid>',
   actor_display='Alice', target_id='<memory_id>', target_display='Some Title'>]}`;
   assert the **memory-trace** scope
   header line (`audit trace for memory <id> ...` — the conditional header for
   the id+`target_type=memory` case, finding 4), one event line with
   `event_type`, `(refute)`, `actor=`, `result=`, `capability=`,
   `reason=contradicted`, that the call URL is
   `/v1/inspection/audit-events?...` and carries BOTH `target_id=<memory_id>`
   AND `target_type=memory` (finding 5), and NO truncation note (`count==1`).
   **ID preservation (round-8 finding 3):** assert the event line contains the
   raw `actor_id` value AND `(Alice)` (id anchored, display annotated — NOT
   `actor=Alice` with the id dropped), and likewise the raw `target_id` AND
   `(Some Title)`. A companion case with `actor_display=None`/`target_display=None`
   asserts the line renders `actor=<actor_id>`/`target=<target_id>` with NO
   trailing `()`.
9-hdr. **`audit` conditional headers (finding 4)** — (i) call with NO id args:
    assert the header is the project-wide form (`project-wide audit events ...`),
    NOT `audit trace for memory`; (ii) call with `target_id=X target_type=memory_link`:
    assert the header is `audit trace for memory_link X`. Guards against the
    fixed "memory <id>" header being emitted for project-wide/non-memory calls.
9a. **`audit` event without transition metadata** — item with `metadata={}`;
    assert the line renders with no `(...)` transition suffix and no `reason=`
    suffix (no `None` leakage).
9b. **render multiline-injection guard (shared helper + header)** — an audit
    item whose `metadata={'transition_type': 'refute', 'reason': 'line1\nline2\nfake
    2099-01-01 EvilEvent'}` AND whose `actor_id`, `target_id`, and `target_type`
    ALSO contain `'\n...fake record'` (these are the fallback/interpolated
    values the render substitutes; they are not control-char-stripped upstream
    — round-3 findings 7, 8 + round-5 finding 2), AND whose `actor_display` and
    `target_display` ALSO contain `'\n...fake record'` (raw operator-set
    identity/project/team names, returned unstripped — `inspection/views.py:421,430`
    for actor, `:476,489` for target — round-9 finding 1); assert the rendered
    event line still contains BOTH the (sanitized) `actor=<actor_id> (<actor_display>)`
    and `target=<target_id> (<target_display>)` annotations on ONE physical line
    (a newline in either display MUST NOT forge an extra apparent record), AND a `memory_get` links body
    whose link `target`/`label` contain `'\n'` (they flow into the `links:`
    line; secret-redacted but not newline-escaped upstream); assert each
    rendered EVENT/structured/`links:` line count is unchanged (no newline
    creates an extra apparent record) for ALL of these fields. **Also inject a
    newline into the
    caller's `target_id` and `target_type` ARGUMENTS** (which flow into the
    scope header, not just event fields — round-5 finding 3) and, using a stub
    that returns ZERO events, assert the scope header is exactly one physical
    line (the per-event guard never runs when there are no events, so the header
    sanitizer is what must hold). Exercises the shared render helper used by
    both tools and both surfaces.
9c. **`audit` newest-first single request + truncation note** — `limit=20`;
    stub returns `{'count': 25, 'items': [...20 newest-first...]}`. Assert the
    handler makes exactly ONE request (`len(stub.calls) == 1`, NO `offset` param,
    NO second call) whose query string carries `ordering=-created_at` and
    `limit=20`; that the rendered event lines are in the returned (newest-first)
    order; and that the truncation note
    `(showing most recent 20 of 25 events; 5 older omitted — narrow with
    since/until/event_type)` is appended (denominator is the response `count`, no
    `~`, no reconciliation note). (a) `count <= limit` (e.g.
    `{'count': 12, 'items': [...12 items...]}`): assert ONE request, page rendered
    newest-first, and NO truncation note. (b) `count` exactly equal to `limit`
    (`{'count': 20, 'items': [...20 items...]}`): assert ONE request and NO
    truncation note (`count` is not `> limit`). No offset, second-request, or
    race-reconciliation assertions remain — the two-request window is removed
    (teamlead decision 2026-07-20 S2).
10. **`audit` capability denied** — stub returns
    `(403, {'code': 'missing_capability', 'error_code': 'missing_capability',
    'detail': '...'})`; assert the `audit:read` re-issue message.
10a. **`audit`/`memory_get` project_scope_denied** — stub returns
    `(403, {'code': 'project_scope_denied', 'error_code':
    'project_scope_denied', 'detail': '...'})`; assert the project-scope
    message (NOT the `audit:read`/`memories:read` message) — finding 4.
10b. **`audit`/`memory_get` team_scope_denied** — stub returns
    `(403, {'code': 'team_scope_denied', 'error_code': 'team_scope_denied',
    'detail': '...'})`; assert the team-scope message (NOT the capability or
    project-scope message), proving the third 403 code is handled and not
    swallowed by the generic `_error_text` path — finding 9. Covers both the
    forwarded-`team_id` denial and the P1 version/links `team_scope_denied`.
11. **`audit` missing project_id** — config without `project_id`, repo-only;
    assert the "needs a project_id" message and that NO HTTP call was made
    (`stub.calls == []`).
12. **`audit` empty result** — `{'count': 0, 'items': []}` → header +
    "No audit events found."
13. **CLI parity dispatch (`main.py`)** — in `cli_lifecycle_tests.py` assert
    `engram memory get <id>` routes to `run_memory_get` and `engram audit`
    routes to `run_audit`, each with a stub transport, verifying stdout render
    + exit code 0. *Fails first (unknown subcommand).*
13a. **CLI `memory get` version/links + versions** — repo-only config;
    version+links stub; assert `run_memory_get` prints the untruncated body, the
    `versions:` line (all versions), the `links:` line, the `engram_search`
    pointer note, exit 0, and that no inspection call exists on any path.
13b. **CLI `memory get` not-found → exit 1 + stderr** — version route returns
    `(200, {'count': 0, 'items': []})` (the empty / not-visible shape); assert
    `run_memory_get` RETURNS `1` and writes the not-found remediation to stderr
    (the inner logic raises
    `CliError`, the wrapper catches it via `emit_error` → return 1, matching
    `run_memory_links`; NOT a raised exception, NOT a 0-exit friendly-text
    branch). Asserts the CLI hard-failure contract (exit 1 + stderr) vs MCP's
    returned-text contract over the same shared render/logic (findings 9, 10).
13c. **CLI `audit` capability denied → exit 1 + stderr** — `(403,
    missing_capability)`; assert `run_audit` returns `1` and writes the
    `audit:read` remediation to stderr (wrapper catches `CliError`).
13d. **CLI `audit` missing project_id → exit 1 + stderr** — repo-only; assert
    `run_audit` returns `1`, stderr carries "needs a project_id", and NO HTTP
    call.
13f. **CLI `audit` alias precedence + target_type default/passthrough** — (i)
    pass both `--memory-id` and `--target-id` (no `--target-type`); assert the
    outgoing `target_id` equals the `--target-id` value (target wins) AND
    `target_type=memory` is defaulted on the wire; (ii) pass `--target-id X
    --target-type memory_link`; assert the wire carries `target_type=memory_link`
    unchanged — proving the CLI can express the non-memory trace the tool
    advertises (finding 5).
13g. **CLI `memory get` diff** — pass `--from-version 1 --to-version 2` with a
    version/links + diff stub; assert `run_memory_get` issues the
    `/diff?from_version=1&to_version=2` request and stdout renders both labeled
    bodies (the ONLY test that exercises the CLI `--from-version`/`--to-version`
    argparse destinations and their mapping into the shared diff logic —
    finding 12). Also parametrize a one-sided case (`--from-version 1` only) →
    no `/diff` call.
13h. **CLI `memory get`/`audit` project_scope_denied → exit 1 + stderr** —
    `(403, {'code': 'project_scope_denied'})`; assert the wrapper returns `1`
    and stderr carries the project-scope remediation (NOT the
    `memories:read`/`audit:read` message) — CLI counterpart of MCP test 10a
    (finding 12; the enumerated CLI tests previously covered only missing
    capability and missing project).
14. **Bundle byte-sync** — the existing
    `packages/claude-plugin/bundle_sync_tests.py` and
    `packages/codex-plugin/bundle_sync_tests.py` guard byte-identity; they
    must pass after `scripts/sync_plugin_bundle.py` runs. No new test needed;
    listed as a gate.
15. **Installed-plugin E2E tool-set gate (MUST update + run)** — the E2Es
    hard-code exactly six tools: `EXPECTED_MCP_TOOLS` at
    `scripts/e2e_codex_plugin.py:31` (and the "checking all six tools" assert
    at `:656`), and `len(tools) != 6` at `scripts/e2e_claude_plugin.py:253`.
    Update the Codex `EXPECTED_MCP_TOOLS` set to the exact **8**-name set
    (adding `engram_memory_get`, `engram_audit`). For the Claude E2E, do NOT
    merely change `!= 6` to `!= 8` — a count-only check would pass a wrong
    eight-tool set; replace it with an **exact-name equality** assertion
    against the same 8-name expected set (mirroring `EXPECTED_MCP_TOOLS`), so a
    renamed/missing tool fails. Adjust the progress strings and re-run both
    E2Es. Adding the tools without this makes both E2Es fail. Required gate
    (finding 16).
15a. **Other CI tool-count asserts (MUST also update — round-5 finding 6).**
    The tool count is asserted in TWO more required CI jobs that order-item 15
    missed, and adding the tools breaks BOTH:
    - `packages/cli/engram_cli/cli_lifecycle_tests.py:3509`
      (`test_mcp_serve_round_trips_initialize_and_tools_list`) asserts
      `self.assertEqual(6, len(lines[1]["result"]["tools"]))`. This runs in the
      CLI unittest lane on every PR (`.github/workflows/backend.yml:104`,
      `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli`).
      Update the expected count to `8` (and, to match the intent of gate 15,
      prefer asserting the two new names are present rather than a bare count).
    - `scripts/e2e_golden_path.py:386`
      (`assert_equal(len(tool_names), 6, 'mcp tools count')`) runs in the
      Compose golden-path job on every PR
      (`.github/workflows/compose-e2e.yml:27`, `python3
      scripts/e2e_golden_path.py`). Update the expected count to `8` and, since
      `tool_names` is already in hand, add the two new names to the checked set
      so a wrong 8-tool set still fails.
    Both are hard gates: implementing the eight-tool set without these two edits
    red-lights the Backend and Compose-E2E CI jobs.

### Backend tests (Backend Prerequisites)

16. **P1 version team-scope** (`engram/memory/views_tests.py`) — a team-bound
    key (`scope.team_ids=(team_a,)`) requesting a `visibility_scope=TEAM`
    memory owned by `team_b` in the same project → `GET
    /v1/memories/<id>/version` returns 403 `team_scope_denied` (currently
    returns the body). **Fail-closed null-team case (round-11 finding 1):** a
    `visibility_scope=TEAM` memory with `team_id=NULL` requested by a
    project-scoped key (any `scope.team_ids`, including empty) ALSO returns 403
    `team_scope_denied` on BOTH `GET /v1/memories/<id>/version` and `GET
    /v1/memories/<id>/links` — proving P1 does not reuse the fail-open
    `team_id is not None` shortcut and agrees with retrieval's team-visibility
    rule (which rejects the same row). **Missing-parent fail-closed case (round-12 finding 2):**
    forcibly write a `MemoryVersion` AND a `MemoryLink` whose OWN
    `organization_id`/`project_id` equal the caller scope but whose parent
    `Memory` lives in a DIFFERENT project (bulk_create/update to bypass
    `clean()`, mirroring the unconstrained-child state); `GET
    /v1/memories/<id>/version` and `GET /v1/memories/<id>/links` (where `<id>` is
    the mis-projected parent memory, not in the caller's project) BOTH return the
    empty shape `{'count': 0, 'items': []}` — never the foreign child row —
    proving the handler fails closed when the scoped parent lookup returns `None`
    instead of falling through to the child query. *Fails first* (the version
    view currently returns the mis-projected child; the links view has no parent
    fetch at all). **Diff team-scope case (finding 1):** the same `team_b`,
    null-team, `SESSION`, and `ORGANIZATION` memories requested via `GET
    /v1/memories/<id>/diff?from_version=1&to_version=2` by a project-scoped key ALL
    return 403 `team_scope_denied`, and a `PROJECT`/authorized-`TEAM` memory
    returns 200 — proving the diff path applies the P1 whitelist, NOT the fail-open
    `ensure_memory_team_scope` shortcut (currently returns the bodies). **Diff
    mis-projected-version case (finding 2):** with an in-scope parent memory whose
    version row for `to_version` is forcibly written with a FOREIGN
    `organization_id`/`project_id` (bypassing `clean()`), `GET
    /v1/memories/<id>/diff` 404s that side as `version_not_found` — never the
    foreign version `body` (currently `_get_version` returns it). *Fails first.*
17. **P1 links team-scope + P2a digest quarantine**
    (`engram/memory/views_tests.py`) — same cross-team setup → `GET
    /v1/memories/<id>/links` returns 403 `team_scope_denied`; and an
    unproven-digest memory → links returns `{'count': 0, 'items': []}`
    (mirroring the version view's existing quarantine). *Fails first.*
17b. **P2c audit target-display scope + quarantine**
    (`engram/inspection/views_tests.py`) — three failing cases:
    (i) **cross-scope:** an `AuditEvent` in project P1 with
    `target_type='memory'`, `target_id=<a memory that lives in a DIFFERENT
    project P2 (or a different team)>`; request the audit list scoped to P1 and
    assert that row's `target_display` is `None` (raw-id fallback), NOT the
    other-scope memory's title — the memory-target title lookup is constrained
    to the request's `organization_id` + `project` + the P1 visibility whitelist
    (`PROJECT` OR authorized `TEAM`), so an out-of-scope memory is never resolved.
    (ii) **in-project but visibility-denied (finding 3):** an in-project
    `AuditEvent` whose `target_id` points at a `visibility_scope=SESSION` (and a
    second at `visibility_scope=TEAM, team_id=NULL`) memory — rows that the looser
    `team_filter` would ADMIT; assert `target_display` is `None`, NOT the title,
    proving P2c uses the P1 whitelist and not `team_filter`. (iii) **unproven
    digest (same scope):** an `AuditEvent` with `target_id=<unproven digest id>`
    in-scope; assert `target_display` is `None`, not the digest's title. A proven,
    in-scope `visibility_scope=PROJECT` (or authorized-`TEAM`) memory target still
    resolves to its title (control). *Fails first.*
18. **P3 audit `target_id`+`target_type` filter**
    (`engram/inspection/filters_tests.py` / audit view test) — three
    `AuditEvent`s: two with the SAME id value but different `target_type`
    (`memory` vs `memory_link`) plus one unrelated; `GET
    /v1/inspection/audit-events?project_id=...&target_id=<id>&target_type=memory`
    returns ONLY the `memory`-typed row (proves `target_id` alone is
    insufficient — finding 5). Plus an **index-presence assertion** (finding
    11) that proves the `AddIndex` migration physically shipped, NOT merely that
    the model declares the index: assert the composite index
    `(organization, project, target_type, target_id, created_at)` exists in the
    **migrated database schema** via
    `connection.introspection.get_constraints(cursor, AuditEvent._meta.db_table)`
    — find an entry whose `columns` equal
    `['organization_id', 'project_id', 'target_type', 'target_id', 'created_at']`
    and whose `index` flag is set. Do NOT assert against `AuditEvent._meta.indexes`
    alone: that reflects only Python model state, so it would pass even if the
    `AddIndex` migration were never generated (the pytest DB is built from
    migrations, so an undeclared migration means the index is simply absent from
    the introspected schema — the assertion above fails, exactly as intended,
    and CI's `makemigrations --check` at `.github/workflows/backend.yml:66` would
    independently flag the model/migration drift). The introspection assertion is
    what verifies "the migration actually shipped"; the model `Meta.indexes`
    declaration is verified separately by that CI drift check. *Fails first.*
19. **P5 audit list `ordering` param** (`engram/inspection/services_tests.py`
    and/or the audit view test) — seed several `AuditEvent`s with DISTINCT
    `created_at` values (plus at least one pair sharing a `created_at` to
    exercise the id tiebreaker). Assert:
    - `GET /v1/inspection/audit-events?project_id=...&ordering=-created_at`
      returns them NEWEST-first, and the equal-`created_at` pair is ordered by
      DESCENDING id (tiebreaker sign matches the ordering direction);
    - `ordering=created_at` (and the DEFAULT — no `ordering` param, the console
      case) returns them OLDEST-first with an ASCENDING id tiebreaker, UNCHANGED
      from today's behavior;
    - an INVALID `ordering` value (e.g. `ordering=bogus` or `ordering=title`)
      falls back to the `created_at` default, not an error.
    *Fails first* (the view currently hardcodes `.order_by('created_at', 'id')`
    and ignores the `ordering` param on the audit list).

## Out of Scope

- Backend changes BEYOND the guards in Backend Prerequisites. The COMPLETE,
  authoritative set of allowed guards is: P1 (incl. its fail-closed
  parent-missing, TEAM/null-team, and diff-path team-scope + diff-parent
  subrules), P2a, P2c, P3, P3-index, and P5. The former P4 and the P6 family
  (P6/P6-quarantine/P6-doc/P6-child/P6-audit), plus P2b, existed only to make the
  inspection memory DETAIL read safe; `engram_memory_get` no longer uses that
  read (teamlead decision S2, 2026-07-20), so they are OUT of this slice — see
  the inspection-hardening follow-up subsection below. (The former P6-child-diff
  is NOT out of scope: `/diff` IS one of the three reads `engram_memory_get`
  surfaces, so its parent-org/project guard is retained here, folded into P1 as
  the diff-parent subrule.) Inspection `repository_url` routing is a noted future
  improvement, NOT this slice.
- **Pre-existing inspection-DETAIL view team-scope / visibility leaks (separate
  follow-up).** Because `engram_memory_get` was cut back to the by-id
  version/links/diff reads (teamlead decision S2), this slice no longer touches
  the inspection memory DETAIL view, so several real, PRE-EXISTING leaks in that
  view are NOT fixed here and are tracked as a standalone inspection-hardening
  issue: the unfiltered `memory.versions.all()` in `_memory_source_provenance`
  (foreign `source_session_id`/`source_correlation_id`), the unfiltered
  `memory.retrieval_documents.all()` inlined in the detail render (cross-team
  document `full_text`), the loose `authorized_for_injection` field, the
  audit-target org-wide title resolution where it is not already covered by P2c,
  and the inspection `team_filter`-vs-memory-visibility divergence on the detail
  read. These pre-date this slice, are not on the read paths this slice
  surfaces, and this client-tooling slice does not own that audit. (The diff
  endpoint's OWN team-scope whitelist AND per-version parent-org/project guard
  are NOT deferred: `/diff` is one of the three reads `engram_memory_get`
  surfaces, and teamlead decision S2 binds all three to fail-closed team scope,
  so per Backend Prerequisite P1 the diff path is brought under the P1 whitelist
  and the P1 diff-parent subrule in THIS slice. The mis-projected-`MemoryVersion`
  body leak is steady-state disclosure — the operator directive waives only
  backward-compat / deployment choreography, not steady-state correctness — so it
  is fixed here, not deferred.)
- **Newest-first is a whitelisted `ordering` param, not general pagination.**
  P5 adds only `created_at` / `-created_at` to the audit list (default
  `created_at` unchanged); `engram_audit` sends `ordering=-created_at` and
  renders the returned page. It does NOT add an `offset` param to
  `engram_audit`, a two-request newest-window fetch, cursor pagination, or any
  reconciliation/snapshot machinery. Narrowing beyond one page is via
  `since`/`until`/`event_type`, not paging.
- **Complete cross-identity audit trace is deferred.** `engram_audit` v1
  returns only events whose audit target IS the memory (see the Design
  limitation note). A trace that also folds in merge-as-RESULT events,
  confidence-decay events (`metadata.memory_ids`), and link add/remove events
  (`metadata.memory_id`) requires JSON-metadata matching and its own index and
  is a future improvement, NOT this slice. The tool description and scope
  header state this boundary so it is never mistaken for a complete lifecycle.
- Conflict-queue listing (S3).
- Observation-detail / single-observation tool.
- New `audit:read` / project-scope key issuance flow — the tools only *report*
  the missing capability or project-scope denial; re-issuing keys is an
  operator/console action.
- Rich diff formatting (word-level); we render both full bodies plainly.

## Distribution / documentation gate (mandatory)

Implementing the two tools makes shipped docs that assert "six tools" stale.
Before the slice merges, update every surface that enumerates the tool set to
list **8** tools (adding `engram_memory_get`, `engram_audit`) and add the two
new CLI subcommands where CLI commands are documented (finding 11):

- `packages/claude-plugin/README.md:118-124` ("Six tools are exposed: ...").
- `packages/codex-plugin/README.md:3-4,40-42` ("six MCP tools" + the tool
  list).
- `docs/mcp-tools.md:27-39` ("Six tools ship" + the tool table — add two rows).
- `docs/guides/mcp.md:108,119,164-177` ("All six tools ..." ×2 + the tool
  reference).
- `docs/guides/plugins.md:81` ("and six MCP tools as the Claude Code package")
  — a LIVE user-facing guide the earlier draft's file list omitted, and whose
  phrase `six MCP tools` the old grep pattern (`six tools`/`6 tools`/`all six`/
  `Six tools`) does NOT match (round-9 finding 4). Update it to `eight MCP
  tools`.
- `docs/guides/cli.md:35-55,257-273` — add `engram memory get` and
  `engram audit` to the project-resolution list and command reference.
- `docs/agent-integrations.md:104-109` — the explicit six-item bullet list
  (`engram_search` … `engram_memory_feedback`). This list contains NO "six
  tools" phrase, so the grep gate below will NOT catch it — it must be updated
  by hand to add the two new tools (finding 14).

**Semantic correction, not just a count bump (finding 13).** Several of these
surfaces claim ALL tools share the repository-URL fallback and work without a
`project_id` — `docs/guides/mcp.md:108,119` ("All six tools resolve … the same
ladder", "all six tools … work in that mode") and `docs/mcp-tools.md:41` ("All
six also accept an optional per-call `project_id` … and fall back to a
repository-derived project"). That becomes FALSE for `engram_audit`, which this
slice REQUIRES to reject repository-only scope (inspection needs a resolved
`project_id`, evidence §4). So the update MUST NOT be a blind six→eight
substitution: these blanket statements must be rewritten to carve out
`engram_audit` as **project-only** (no `repository_url` fallback) while the
other seven keep the fallback. `engram_memory_get` keeps the fallback (its
version/links path is `repository_url`-routable). A reviewer MUST read the
rewritten sentences, not just confirm a count.

A grep gate (`grep -rn 'six tools\|6 tools\|all six\|Six tools\|six MCP tools'
docs packages/*/README.md` returns only historical spec references, not live
docs) is part of the merge checklist. The pattern MUST include the
`six MCP tools` alternative (round-9 finding 4) — without it the gate silently
misses `docs/guides/plugins.md:81` and `packages/codex-plugin/README.md:4`,
both of which use that exact phrase. Even so the gate is NECESSARY, NOT
SUFFICIENT: it cannot catch the phrase-free `agent-integrations.md` list or
verify the `engram_audit` project-only carve-out, both of which are separate
manual checklist items. Runs alongside the E2E tool-set gate (order-item 15).

## Review Reconciliation

(append-only)

- round 1, finding 1 (blocker), fixed — confirmed version/links GET lack the
  `ensure_memory_team_scope` guard that diff (`services.py:942`) and the POST
  paths (`:861`) have; added Backend Prerequisite P1 + backend tests 16/17 to
  deny cross-team disclosure; dropped the "zero backend change" premise.
- round 1, finding 2 (blocker), fixed — confirmed the audit filter set
  (`inspection/filters.py:32-38`) has no `target_id`, so the tool returned the
  first N project-wide events; added P3 `target_id` filter + `memory_id`/
  `target_id` tool args + backend test 18 so `engram_audit` traces one memory.
- round 1, finding 3 (blocker), fixed — confirmed transition type/reason live
  in `metadata` (returned at `inspection/views.py:402`) but the render dropped
  it; render now appends `(<transition_type>)` and `reason=<reason>` when
  present, with test 9/9a.
- round 1, finding 4 (major), fixed — confirmed `engram_search` returns the
  full redacted `memory.body` (`search/services.py:53-63`), not a truncated
  one; corrected §1 and pointed the 400-char truncation at session-start only.
- round 1, finding 5 (major), fixed — confirmed no DB constraint ties
  highest-version to `Memory.current_version`; softened the claim to
  "highest-version row / newest stored body" in evidence §3 and both design
  fallback references.
- round 1, finding 6 (major), fixed — confirmed links view lacks the
  digest-visibility quarantine version has (`views.py:131`); added P2 (backend
  quarantine on links) + client early-return before links on empty version
  (Data Flow step 3) + test 7 asserting no links call for a hidden memory.
- round 1, finding 7 (major), fixed — confirmed `_error_text` is terminal; the
  Error Handling now excludes diff from the terminal-404 path and routes diff
  404 solely to the addendum note, with test 5b.
- round 1, finding 8 (major), fixed — confirmed `packages/cli` is neither
  mounted nor copied into the `app` container (`docker-compose.yml:15-16`,
  `Dockerfile:22,30`); replaced the bogus pytest command with the CI runner
  `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli`.
- round 1, finding 9 (major), fixed — confirmed both E2Es hard-code six tools
  (`e2e_codex_plugin.py:31,656`, `e2e_claude_plugin.py:253`); added gate test
  15 requiring both be updated to 8 tools and re-run.
- round 1, finding 10 (minor), fixed — confirmed backend enforces both diff
  versions `min_value=1` (`serializers.py:122-124`); Data Flow step 5 and test
  5a now define diff issued only when both `>= 1`, else no call.
- round 1, finding 11 (minor), fixed — confirmed backend returns `None`
  confidence/kind and pads `related` with `link_type=None` siblings
  (`inspection/services.py:167-178`); render now omits null kind/confidence
  tokens and drops `related` entries with null `link_type`, with tests 3/3a.
- round 2, finding 1 (blocker), fixed — CONFIRMED merge commits its audit with
  `memory=source` (`transitions.py:2065` → `target_id=str(memory.id)` at
  `:1256`) so a result-side trace misses the merge; confidence decay writes NO
  `target_id` (`confidence_decay.py:84-101`); link events key `target_id` to
  the link UUID with memory only in `metadata.memory_id`
  (`services.py:1067,1073,1178,1184`). An exact `target_id` filter cannot be a
  complete trace. Reframed the tool from "trace how a memory reached its state"
  to "the memory's OWN recorded events", added an explicit Design limitation
  note + scope header + tool-description sentence enumerating what is NOT
  returned (merge-into, decay, links), and deferred the complete cross-identity
  trace in Out of Scope. Scope is now accurate, not overclaimed.
- round 2, finding 2 (blocker), fixed — CONFIRMED `related_memories` never
  calls `_quarantine` (`inspection/services.py:116-180` vs `:104-114`) and
  `_resolve_memory_targets` loads titles with no quarantine
  (`inspection/views.py:453-463`), so an unproven digest's title leaks through
  `engram_memory_get.related[]` and `engram_audit.target_display`. Expanded P2
  into P2a (links) + P2b (inspection `related[]`) + P2c (audit target-display)
  with backend tests 17a/17b.
- round 2, finding 3 (blocker), fixed — CONFIRMED transitions are unbounded
  (`transitions.py:1248-1262`), backend orders oldest-first
  (`inspection/services.py:263`) and DOES support `offset`
  (`inspection/views.py:159-161`); the old "few events, limit sufficient"
  claim dropped the newest events. Tool now does a newest-window fetch
  (`offset=count-limit`) + truncation note; updated Design/Data Flow, rewrote
  the Out-of-Scope pagination bullet, added test 9c.
- round 2, finding 4 (major), fixed — CONFIRMED the wizard key is issued
  unbound (`commands.py:418-435`, admin view never binds), and an unbound key
  without `projects:*`/`policy:admin`/`projects:agent` raises
  `project_scope_denied` (`access/services.py:293-309,135-149`;
  `core/repository.py:120-158`), so BOTH tool paths 403 — the "works out of
  the box" claim was false. Rewrote evidence §5, added `project_scope_denied`
  Error Handling + tests 10a/13b-style CLI coverage.
- round 2, finding 5 (major), fixed — CONFIRMED `target_type`/`target_id` are
  independent columns (`core/models.py:1064-1065`) keyed as a pair
  (`inspection/views.py:385`). P3 now adds BOTH filters; the tool defaults
  `target_type='memory'` with a `memory_id`; test 18 uses colliding ids across
  types.
- round 2, finding 6 (major), fixed — CONFIRMED no index contains `target_id`
  (`core/models.py:1072-1084`) and the view counts before slicing
  (`inspection/views.py:158`). Added P3-index (composite `AddIndexConcurrently`
  + migration) and corrected the "No schema/migration changes" claim.
- round 2, finding 7 (major), fixed — CONFIRMED `/version` returns every row
  (`memory/views.py:134-144`) but the fallback rendered only `items[0].body`.
  Fallback render now emits a `versions:` line listing all returned versions;
  test 4 asserts multiple versions.
- round 2, finding 8 (major), fixed — CONFIRMED reason is only
  redacted+truncated, not newline-escaped (`transitions.py:379-386`,
  `core/redaction.py:62-74`). Added a control-character collapse rule to BOTH
  renders (audit line + memory_get structured lines) with test 9b.
- round 2, finding 9 (major), fixed — CONFIRMED MCP handlers return text while
  CLI handlers raise `CliError`/exit 1 (`mcp_tools.py:400-407` vs
  `commands.py:2207-2221`). Mandated shared pure render/logic helpers and added
  CLI parity tests 13a-13f (fallback, not-found, denial, missing-project,
  null-render, alias precedence).
- round 2, finding 10 (major), fixed — `packages/cli` is absent from the app
  image, but the repo rule (`CLAUDE.md:157-158`) still forbids host runs.
  Replaced the host command with a containerized lane that mounts
  `packages/cli` into the `app` image (`docker compose run ... -v
  ...:/cli`), with a `cli-test` service fallback if a dep is missing; CI runs
  the byte-equivalent.
- round 2, finding 11 (minor), fixed — CONFIRMED six-tool claims in
  `claude-plugin/README.md:118`, `codex-plugin/README.md:3,40`,
  `docs/mcp-tools.md:27`, `docs/guides/mcp.md:108,119`, and CLI-command
  omission in `docs/guides/cli.md`. Added a mandatory Distribution/documentation
  gate enumerating each file + a grep gate.
- round 2, finding 12 (minor), fixed — CONFIRMED `build_domain_error_payload`
  emits BOTH `error_code` and `code` (`drf_exception_handler.py:120-122`).
  Corrected evidence §6 to show both fields for `missing_capability` and
  `project_scope_denied`; client still keys off `code`.
- round 2, finding 13 (minor), fixed — CONFIRMED `LinkType` has no
  `relates_to` (`core/models.py:1099-1106`). Test 3 now uses the valid
  `narrowed_by`.
- round 2, finding 14 (minor), fixed — CONFIRMED only 3 of 6 existing
  descriptions are numbered and they form a workflow sequence
  (`mcp_server.py:82,150,185`). Dropped the "Step N" mandate, specified the two
  exact unnumbered reference-style description strings, and added description
  test 1a.
- round 3, finding 1 (blocker), fixed — CONFIRMED `_resolve_memory_targets`
  filters by `organization_id` ONLY (`inspection/views.py:462`), and
  `AuditEvent.target_type`/`target_id` are unconstrained `CharField`s
  (`core/models.py:1064-1065`), so a project-scoped audit event referencing an
  out-of-project/team memory would leak that memory's title via
  `target_display` — excluding unproven digests alone (old P2c) does not close
  it. Rewrote P2c to constrain the memory-target title lookup to the request's
  org+project+team_filter AND exclude unproven digests; documented that
  project/team/identity name resolution stays org-scoped (org-config, not
  memory content, future hardening). Test 17b now has a cross-scope case.
- round 3, finding 2 (major), fixed — CONFIRMED backend orders ascending
  (`inspection/services.py:263`) so the window rendered oldest-first while the
  description said "newest first." Render now reverses the window to
  newest-first (pure client transform); updated Design windowing, the
  newest-window algorithm, Data Flow step 4, and test 9c order assertion.
- round 3, finding 3 (major), fixed — CONFIRMED count and slice are separate
  round-trips (`inspection/views.py:158-161`); a stale `count` could omit the
  newest event / report a stale total. Added race reconciliation: read `count2`
  from the second (offset) response, use it as authoritative for the note, and
  append an explicit "log changed during this read" note when `count2 != count1`.
  Updated algorithm + Data Flow + test 9c race variant.
- round 3, finding 4 (major), fixed — CONFIRMED the schema permits no-id
  (project-wide) and non-memory `target_type` calls while the header was fixed
  to "audit trace for memory <id>." Made the scope header CONDITIONAL
  (memory-trace / non-memory-target / project-wide) and added test 9-hdr.
- round 3, finding 5 (major), fixed — CONFIRMED the CLI `audit` subcommand had
  no `--target-type`, so it could only ever trace `memory` targets, breaking
  parity with the tool's advertised non-memory trace. Added `--target-type`
  (default `memory`, explicit passthrough) + test 13f case (ii).
- round 3, finding 6 (major), fixed — CONFIRMED `actor_display` (API-key
  owner/identity names, `inspection/views.py:421,430`) and project/team/identity
  `target_display` (`:476,489,505`) are returned RAW; only memory titles are
  `redacted_text` (`:463`). Corrected the false "redacted/truncated" claim,
  kept the control-char collapse on all fields, and scoped display-name
  secret-redaction out (pre-existing org-wide inspection behavior shared with
  the console; the high-risk `reason` field is already redacted upstream).
- round 3, finding 7 (major), fixed — CONFIRMED `actor_id`/`target_id` are
  unconstrained fields whose `redacted_text` does not strip control chars
  (`inspection/views.py:393,396`, `core/redaction.py:62-74`), and the render
  substitutes them as fallbacks. Broadened the control-char collapse to EVERY
  interpolated field (event_type, transition_type, actor_display, actor_id,
  result, target_display, target_id, capability, reason) and extended test 9b
  to inject newlines into `actor_id`/`target_id`.
- round 3, finding 8 (major), fixed — CONFIRMED the candidate-merge path
  (`_execute_candidate_revision` MERGE, `transitions.py:1961-1976`) commits with
  `memory=result_memory=<this memory>` so the merge IS on that memory's trace,
  while only the DIRECT two-memory merge (`:2060-2076`) hides the result side;
  and ALL `MemoryTransitionCommitted` rows carry `target_id=memory.id`
  (`:1248-1262`) so the six-event list is illustrative, not exhaustive. Rewrote
  the limitation note + tool description to distinguish candidate-merge-in
  (visible) from direct-merge-result (hidden) and to stop presenting the list
  as exhaustive.
- round 3, finding 9 (major), fixed — CONFIRMED `_team_ids`
  (`access/services.py:344-374`) is a THIRD gate evaluated before capability
  (`:151` before `:168`), raising `team_scope_denied` for a forwarded
  cross-team `team_id`, and P1 introduces the same code on version/links. Added
  the third gate to evidence §5, the wire shape to §6, a team-scope Error
  Handling branch, and MCP test 10b.
- round 3, finding 10 (major), fixed — CONFIRMED `run_*` wrappers catch
  `CliError`/return 1 and `main.py` has no outer catch (`main.py:39`), so a
  raising `run_audit`/`run_memory_get` would leak a traceback, not exit 1.
  Specified the shared helper RAISES while the public wrapper catches → returns
  1 + stderr; rewrote tests 13b-13d to assert exit code + stderr, not a raise.
- round 3, finding 11 (major), fixed — CONFIRMED the repo's audit indexes use
  ordinary `migrations.AddIndex` (`core/migrations/0020_*.py:31-61`) with NO
  `AddIndexConcurrently` precedent anywhere, and `atomic=False` is set only when
  needed (`0033_*.py:120`). Switched P3-index to an ordinary `AddIndex` declared
  in `AuditEvent.Meta.indexes`, dropped the false "covering"/"concurrent"
  claims, and added an index-presence assertion to test 18.
- round 3, finding 12 (major), fixed — CONFIRMED no enumerated CLI test
  exercised `--from-version`/`--to-version` or `project_scope_denied`. Added CLI
  test 13g (diff) and 13h (project-scope-denial) and corrected the coverage
  claim in the shared-logic section.
- round 3, finding 13 (major), fixed — CONFIRMED `docs/guides/mcp.md:108,119`
  and `docs/mcp-tools.md:41` assert ALL tools share the repository fallback,
  which is false for the project-only `engram_audit`. Expanded the doc gate to
  require rewriting those blanket statements to carve out `engram_audit`
  (project-only) rather than a blind six→eight substitution.
- round 3, finding 14 (minor), fixed — CONFIRMED `docs/agent-integrations.md:104`
  lists all six tools with no "six tools" phrase (grep-invisible). Added it to
  the doc-gate file list as a manual update, noting the grep gate cannot catch
  it.
- round 3, finding 15 (minor), fixed — CONFIRMED `engram install` consumes the
  supplied `--api-key` and installs plugins (`commands.py:634,643`); it does not
  issue a key. Corrected the `project_scope_denied` remediation to point at the
  Connect-agent modal for the `projects:agent` key.
- round 3, finding 16 (minor), fixed — CONFIRMED the Claude E2E uses a count
  check (`len(tools) != 6`) that a wrong 8-tool set would pass. Test 15 now
  requires exact-name equality for BOTH E2Es, not a count bump.
- round 3, finding 17 (minor), fixed — CONFIRMED `MemoryLinksView._link_response`
  returns `link_id`/`link_type`/`target`/`label`/`created_at` with no title
  resolution (`memory/views.py:303-310`); the P2/P2a "renders another memory's
  title" rationale was wrong. Corrected P2 to describe the links leak as the
  hidden memory's OWN link records (target/label), not titles; the quarantine
  is still warranted.
- round 4, finding 1 (major), fixed — CONFIRMED the reconciled note used
  `count2` as the "most recent of N" denominator while the returned window is
  anchored to `count1` (offset `= count1 - limit`). Because audit rows are
  append-only and sort to the end of the ascending `created_at, id` order
  (`inspection/services.py:263`), `qs[count1-limit:count1]` returns EXACTLY the
  newest `limit` of the `count1` snapshot even when the table grew, so labelling
  it "most recent <limit> of <count2>" falsely reported the two truly-newest
  race events as shown. Changed the truncation-note denominator to `count1`
  throughout (Design windowing, algorithm step 2/3/4, Data Flow step 4, test
  9c); `count2` now serves ONLY the race-disclosure note. The window is correct;
  the note is now truthful ("most recent 20 of 25; 5 older omitted" + a separate
  "2 newer arrived, re-run").
- round 4, finding 2 (major), refuted:false-positive — the premise "every
  API-key memory read records `AccessScopeResolved('memory', <id>)`" is false:
  `_audit` returns early on `AuditResult.ALLOWED` and creates NO row
  (`access/services.py:394-395`), so a successful read writes nothing and twenty
  ordinary reads cannot evict any transition. Only *denied* reads (three
  `AuditResult.DENIED` calls, `access/services.py:136-185`) write such a row,
  which is low-volume and legitimately part of "the memory's own recorded
  events." Added a precision note to the Design limitation section documenting
  that denial rows are in-scope (rendered as `AccessScopeResolved`/`result=denied`)
  while allowed reads emit nothing, so no eviction is possible.
- round 4, finding 3 (minor), fixed — CONFIRMED `AuditEvent._meta.indexes`
  reflects only Python model state, so it would pass with the model declaration
  but no `AddIndex` migration, leaving CI's `makemigrations --check`
  (`.github/workflows/backend.yml:66`) to fail. Rewrote test 18 to REQUIRE the
  migrated-schema check via `connection.introspection.get_constraints` (matching
  the composite columns + index flag) and forbade the `_meta.indexes`-alone
  assertion, so the gate verifies the migration physically shipped.
- round 5, finding 1 (major), fixed — CONFIRMED the round-4 "append-only ⇒
  `qs[count1-limit:count1]` returns EXACTLY the newest limit" claim is false
  under normal MVCC. `created_at` is `auto_now_add=True` (stamped at INSERT,
  `core/models.py:19`) and transition audits are inserted inside
  `transaction.atomic()` (`_commit_transition` create at
  `memory/transitions.py:1248`, callers wrap at `:1391/:1588/:1762/:1836`), so a
  row inserted with an OLD `created_at` but committed BETWEEN the two READ
  COMMITTED round-trips becomes visible mid-ordering and shifts the offset
  window — it does NOT land at position `>= count1`. Removed the false
  exactness/"appended rows land at the end" wording (Design windowing, algorithm
  step 2/3, Data Flow step 4); marked the window best-effort with a `~`-prefixed
  approximate denominator; and rewrote the race note from "N newer events
  arrived" to "row count moved by N — the window may be inconsistent, re-run for
  a stable view" (a late-committed OLDER row can grow/reshuffle the count too).
  Updated test 9c's expected note strings. This corrects the note without
  weakening the disclosure — the window is honestly labeled approximate and the
  race is surfaced.
- round 5, finding 2 (major), fixed — CONFIRMED `audit_event_response` returns
  `target_type` and `target_id` as independent fields
  (`inspection/views.py:395`) but the render emitted only
  `target=<target_display or target_id>`, so colliding ids across types and
  display-suppressed targets render identically (ambiguous in project-wide
  reads). Added an unconditional `target_type=<target_type>` token to the event
  render, added `target_type` to the sanitized-field list, and extended test 9b
  to inject a newline into `target_type`.
- round 5, finding 3 (major), fixed — CONFIRMED the one-line spoofing guard
  covered only the per-event line fields (`682-707`) while the scope header
  (`712-721`) interpolated the caller's raw `target_type`/`target_id`
  (unconstrained `CharField`s, `core/models.py:1064-1065`), so a newline in
  either header argument forges apparent lines even with zero events. Added an
  explicit rule that the scope header runs the SAME control-character collapse
  over its interpolated `<id>`/`<target_type>`, and extended test 9b to inject a
  newline into the header ARGUMENTS with a zero-event stub (where the per-event
  guard never runs).
- round 5, finding 4 (major), fixed — CONFIRMED there is no dedup/bound on
  denial rows: every `AuditResult.DENIED` capability/team check for
  `('memory', <id>)` writes another `AccessScopeResolved` row
  (`access/services.py:135-185`, `413`), and the tool's default `limit` is 20,
  so a burst of in-scope denials CAN push transition rows out of the default
  window. The round-4 "low-volume and cannot evict the transition history"
  reassurance was wrong. Rewrote the Design limitation note: allowed reads still
  emit nothing (ordinary usage is unaffected), but denials are unbounded and the
  real mitigations are the `result=denied` tagging plus
  `event_type=MemoryTransitionCommitted`/`since`/`until` narrowing — the tool no
  longer promises a denial-free window.
- round 5, finding 5 (major), fixed — CONFIRMED the "project/team/capability
  denials appear in the trace" claim is false for the emphasized unbound
  wizard-key case: `_audit` stores the row's `project` FK as
  `resolved_project_ids[0]` or `key.project_id` (`access/services.py:415`), NOT
  the requested project, and `project_scope_denied` passes NO
  `resolved_project_ids` (`:135-149`) → the unbound key's row is stored with
  `project=NULL`, which the audit query's hard `project=inspection_scope.project`
  filter (`inspection/services.py:255`) can never return. Rewrote the denial-row
  note to state which denials are actually in-scope (only those where the key
  resolved exactly the queried project — missing_capability / single-project
  team denial) and that the unbound `project_scope_denied` denial never appears.
- round 5, finding 6 (major), fixed — CONFIRMED two more required-CI tool-count
  asserts the gate missed: `cli_lifecycle_tests.py:3509`
  (`assertEqual(6, len(...tools))`, run by `backend.yml:104`) and
  `e2e_golden_path.py:386` (`assert_equal(len(tool_names), 6, ...)`, run by
  `compose-e2e.yml:27`). Added order-item 15a requiring both be updated to 8
  (with name-presence checks), so the eight-tool set does not red-light Backend
  and Compose-E2E CI.
- round 5, finding 7 (minor), fixed — CONFIRMED evidence §5 (`spec:132-134`)
  said the agent key is "issued by `engram install` / the Connect-agent modal",
  contradicting the already-corrected Error Handling text (`:870-872`);
  `run_install` only consumes an existing `--api-key` via `run_connect_flags`
  (`commands.py:634-644`). Corrected §5 to attribute the `projects:agent` key to
  the Connect-agent modal only and explicitly note `engram install` does not
  issue one.
- round 6, finding 1 (major), fixed — CONFIRMED `memory_response` computes
  `authorized_for_injection = status==APPROVED and not stale and not refuted`
  (`inspection/views.py:243`), while the real injection predicate
  `authorized_retrieval_documents` ALSO excludes memories with an unresolved
  `MemoryConflict` (`~Exists(... resolved_transition__isnull=True)`,
  `context/services.py:315-322`) and unproven digests (`:330`); CONFIRMED
  CONFLICT_OPEN keeps the memory active (`_require_active_memory`,
  `transitions.py:2414`), so the field — and `engram_memory_get`'s primary
  render — reports `True` for a memory injection refuses. Added Backend
  Prerequisite P4 making the field fail-closed (AND in the unresolved-conflict
  and `digest_visibility_failure` exclusions; team visibility stays per-request)
  + conflict-case backend test 16b; updated the API/Schema section and the
  render null-rules note.
- round 6, finding 2 (major), fixed — CONFIRMED the spec mandated
  `non-2xx links → omit the links: line silently` (Data Flow step 4, Error
  Handling, test 8), making a 403/5xx links failure indistinguishable from a
  confirmed empty-link record while the tool description promises "links" as
  part of "the complete record" (`spec:362`); `run_memory_links` already
  surfaces non-2xx as an error (`commands.py:2200`). Changed the contract so a
  non-2xx links fetch renders an explicit
  `links: unavailable (HTTP <status>) — ...` warning line (2xx-empty still omits
  the line), across Data Flow step 4, Error Handling, the render null-rules, and
  test 8 (now asserts the warning line is present, plus a 2xx-empty companion
  asserting no warning).
- round 6, finding 3 (major), fixed — CONFIRMED the backend runs `qs.count()`
  and the page slice as two separate READ COMMITTED statements
  (`inspection/views.py:158,161`) over an oldest-first queryset
  (`inspection/services.py:263`), so in the single-request path a commit landing
  between count and slice can leave `count1 == limit` while the slice returns the
  oldest `limit` of `limit+1` rows — silently dropping the newest event with no
  second read to detect it (the spec's `count1 <= limit` single-request branch
  had no reconciliation). This is a steady-state concurrency gap, not waived by
  the operator directive. Changed the second-request trigger from `count1 > limit`
  to **first-page-is-full (`len(items1) == limit`)** with
  `offset = max(count1 - limit, 0)` (offset `0` when `count1 == limit`, so the
  re-read's `count2` exposes mid-read growth); a non-full page is now the only
  no-second-request/no-note case (provably complete). Truncation note still fires
  only on `count1 > limit`; the race note fires on `count2 != count1`. Updated
  Design windowing, the newest-window algorithm, Data Flow audit step 4, and test
  9c (added full-page case a2: `count1 == limit` still issues the second request;
  companion race with `count2 = count1 + 1` appends the reconciliation note).
- round 7, finding 1 (major), fixed — CONFIRMED the round-6 fix moved the race
  into the SECOND request without closing it: that request repeats the same
  non-atomic `count()`-then-slice (`inspection/views.py:158,161`), so a row
  committing between the backend's own `count2` and the slice shifts the
  oldest-first page (dropping the newest row) while `count2 == count1` reports
  the read clean — an inconsistency INSIDE one backend response that no client
  count-comparison can detect. This is steady-state read consistency, in scope
  per the operator directive. Added Backend Prerequisite **P5**: the audit list
  view returns its `count` and page from ONE MVCC snapshot (window
  `Count(...) OVER()` on the page query, or a `REPEATABLE READ` transaction), so
  within any request count and slice can never diverge; the client's
  `count1`/`count2` comparison then soundly detects exactly the remaining
  between-request growth. Updated Design windowing, the newest-window algorithm
  step 2, Data Flow audit step 4, test 9c a2 (completeness now grounded in P5),
  the API/Schema + Out-of-Scope enumerations, and added backend test 19.
- round 7, finding 2 (major), fixed — CONFIRMED `authorized_retrieval_documents`
  requires an ELIGIBLE `RetrievalDocument` (`stale=False, refuted=False` on the
  document itself, `context/services.py:302-314`) beyond the memory's own
  APPROVED/not-stale/not-refuted flags, and that a missing/inconsistent document
  is a recognized state the P7 invariant tracks
  (`memory/invariant_queries.py:1157`); the round-6 P4 checked only conflict +
  digest, so an APPROVED memory with no eligible document rendered
  `authorized_for_injection=True` while retrieval returned nothing. Extended P4
  with term (a): an eligible `RetrievalDocument.exists()`, making the field
  `False` exactly when injection rejects; added backend test 16c for the
  missing/ineligible-document case that conflict-only 16b did not cover.
- round 7, finding 3 (major), fixed — CONFIRMED the primary header interpolates
  `kind` (`spec` render template) sourced from arbitrary JSON `metadata['kind']`
  (`inspection/views.py:238`) with no enum constraint on the mirrored column
  (`Memory.kind` is an unconstrained `CharField`, `core/models.py:730`), while
  the round-5 sanitization contract collapsed control chars only on
  `title`/`versions`/`related`/`links` — so a newline-bearing `kind` could forge
  a header/`versions:`/`title` line. Added `kind` to the control-character
  collapse rule and extended test 9b to inject a newline into
  `metadata['kind']`.
- round 7, finding 4 (minor), fixed — CONFIRMED the links GET runs its own
  authorization (`memory/views.py:200`, P1 adds a team check) so a links
  `project_scope_denied`/`team_scope_denied` 403 is reachable, and the spec
  defined both a "links non-2xx → warning, continue" rule and an "either tool
  403 → terminal denial" rule without precedence. Resolved: the links-warning
  rule ALWAYS wins for the supplementary links fetch (a links 403/any non-2xx
  degrades to the `links: unavailable` line, never terminal); the terminal 403
  rule governs only the PRIMARY fetch (inspection detail / version fallback) and
  `engram_audit`. Safe because the primary inspection detail already enforces
  the same team scope (`team_filter`, `inspection/services.py:52-53`) and 404s a
  team-scoped memory the key cannot see, so a rendered body already implies
  authorization. Updated the Links-non-2xx and team_scope_denied Error-Handling
  bullets.
- round 8, finding 1 (major), fixed — CONFIRMED inspection's `team_filter`
  (`team IS NULL OR team_id IN scope.team_ids`, `inspection/services.py:52-53`)
  is STRICTER than the memory-visibility rule that `ensure_memory_team_scope`
  (`memory/services.py:838-844`) and retrieval (`filter_documents_by_team_visibility`,
  `context/services.py:287-290`) enforce, and that the realtime path creates
  memories with a non-null team AND `visibility_scope=PROJECT` (candidate
  `memory/services.py:440,445`; promoted memory `memory/transitions.py:1046,1053,1057`).
  So a project-visible-but-team-tagged memory 404s through inspection detail for a
  no-team project-scoped key while search and the version/links fallback return it —
  `engram_memory_get` (which picks the inspection path when `project_id` resolves)
  hid a memory the caller could otherwise read. Added Backend Prerequisite P6:
  the inspection memory DETAIL read (`ListInspectionMemories.detail`) now scopes by
  `visibility_scope=PROJECT OR (visibility_scope=TEAM AND team_id ∈ scope.team_ids)`,
  matching the other two surfaces (no new disclosure — the predicate is the
  retrieval/fallback authorization rule); corrected the false "already enforces the
  same team scope" claim in Error Handling; added backend test 16e (with a TEAM-only
  control that still 404s). The stricter `team_filter` stays on the LIST/count/related/
  audit browse paths.
- round 8, finding 2 (major), fixed — CONFIRMED P5's prescribed
  `Window(Count('id'))` annotation with a `qs.count()` fallback "only when the page
  is empty" reintroduced the very two-statement race it claimed to close: an
  empty-page (zero-matching-rows) request whose fallback `qs.count()` ran on a fresh
  READ COMMITTED snapshot could return `{count:1, items:[]}` after a between-statements
  commit, and the client's `len(items1) < limit` branch (spec Data Flow) would then
  declare the read complete and skip reconciliation, dropping the event. Rewrote P5 to
  make a single `REPEATABLE READ` `transaction.atomic()` envelope the load-bearing
  mechanism (window annotation demoted to an in-envelope optimization; plain
  `count()`+`slice` in the same block is the simplest sound form), and REQUIRED the
  empty-page `qs.count()` fallback to run inside that same snapshot. Extended test 19
  with an empty-page single-snapshot structural assertion.
- round 8, finding 3 (major), fixed — CONFIRMED `audit_event_response` returns BOTH
  the id and the display for actor and target (`inspection/views.py:387-405`), that
  `_batch_resolve_actor_names` maps every API key to its `owner_identity.display_name`
  (`:421`, so N keys of one identity collapse to one display), and that memory titles
  are non-unique (`Memory.title` plain `CharField`, `core/models.py:717`). The render
  `actor=<actor_display or actor_id>` / `target=<target_display or target_id>` dropped
  the stable id whenever a display resolved, collapsing distinct actors/targets into
  identical lines. Changed the render template to always anchor on the id
  (`actor=<actor_id>[ (<actor_display>)]`, `target=<target_id>[ (<target_display>)]`),
  appending the display only as an annotation; added a render bullet and strengthened
  test 9 (id + display present, and no trailing `()` when display absent).
- round 8, finding 4 (major), fixed — CONFIRMED `memory_response` computes
  `authorized_for_injection` UNCONDITIONALLY (`inspection/views.py:243`) and
  `MemoryInspectionListView.get` calls it per row for up to 200 rows
  (`inspection/views.py:91`), so P4's three `.exists()`/quarantine sub-queries added
  at line 243 would be a large per-row N+1 on the console list. Scoped the strengthened
  computation to the `include_detail=True` branch only (the sole branch the two new
  read tools exercise); the LIST path keeps its pre-existing loose value (unchanged, no
  regression; a batched-annotation list fix is noted as future hardening). Added backend
  test 16d asserting the list path adds no per-row eligibility sub-queries.
- round 9, finding 1 (major), fixed — CONFIRMED `memory_response(include_detail=True)`
  inlines `retrieval_documents[]` from `memory.retrieval_documents.all()` with NO
  document-level scope filter (`inspection/views.py:273-276`, exposing `full_text`/
  `source_observation_ids`/`file_paths`/`visibility_scope` via
  `retrieval_document_response` `:307-325`), while search runs every document through
  `filter_documents_by_team_visibility` (`context/services.py:287-290`); `RetrievalDocument`
  and `Memory` carry INDEPENDENT `team`/`visibility_scope` columns (`core/models.py:716,720,869+`)
  and P7 tracks scope-inconsistent documents (`invariant_queries.py:1156`). So P6's
  widening let a caller reach a project-visible memory's detail and disclose its
  TEAM-scoped inconsistent document that search rejects — the "never a new disclosure"
  claim was false at the document level. Scoped the "no more, no less" claim to
  memory-SELECTION, added guard P6-doc (filter the detail's inlined documents through
  `filter_documents_by_team_visibility(..., inspection_scope.scope)`), and added backend
  test 16f.
- round 9, finding 2 (major), fixed — CONFIRMED the repo runs persistent connections
  (`CONN_MAX_AGE=60`, `settings/settings.py:185`) while tests run `CONN_MAX_AGE=0`
  (`settings/test_settings.py:15`), so P5's under-specified "set the connection isolation
  to REPEATABLE READ" admitted a connection-level mutation that would leak into reused
  connections and be invisible to the test harness, and PostgreSQL forbids changing
  isolation after the transaction's first query. Pinned the implementation to a
  transaction-local `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` issued as the first
  statement inside the atomic block (auto-resets at commit, no restoration), explicitly
  forbade mutating the connection/`OPTIONS` isolation, and extended test 19 to assert the
  transaction-local SET, an unchanged connection `isolation_level`, and no leak into a
  later read on the same connection.
- round 9, finding 3 (minor), fixed — CONFIRMED `packages/cli/engram_cli/mcp_server_tests.py`
  hard-codes the six-tool count in THREE places, not one: the direct tools/list assertion
  (`:96-120`), `test_tools_list_all_six_schemas_expose_optional_project_id` (`:454`,
  `assertEqual(6, len(tools))`), and `test_run_mcp_serve_wires_build_tools_and_returns_zero`
  (`:504`, NDJSON `assertEqual(6, len(lines[1]["result"]["tools"]))`). Order-item 1 named
  only the first; expanded it to enumerate all three (bump to 8, rename the six-schemas
  method, keep the optional-`project_id` invariant since both new tools carry an optional
  `project_id`), so the CLI unittest lane cannot stay red.
- round 9, finding 4 (minor), fixed — CONFIRMED `docs/guides/plugins.md:81` is a live guide
  reading "and six MCP tools" that the file list omitted, and the grep-gate pattern
  (`six tools\|6 tools\|all six\|Six tools`) does NOT match `six MCP tools` (verified: grep
  returns no match on plugins.md). Added `docs/guides/plugins.md:81` to the mandatory file
  list (→ "eight MCP tools") and added the `six MCP tools` alternative to the grep-gate
  pattern (which also now catches `packages/codex-plugin/README.md:4`).
- round 10, finding 1 (blocker), fixed — CONFIRMED P6 widens the inspection DETAIL
  base queryset to `visibility_filter` while `_quarantine` still discovers unproven
  digests via the narrower `team_filter` (`inspection/services.py:104-112`), and
  `Memory.team`/`visibility_scope` are independent columns (`core/models.py:716,720-724`),
  so a `visibility_scope=PROJECT`, team_b-tagged UNPROVEN digest is IN the widened
  selection but ABSENT from the `team_filter` digest set → not excluded → a by-id read
  returns its full body/documents. That is a NEW disclosure the widening opens (search
  quarantines unproven digests via `_quarantine_unproven_digests`,
  `context/services.py:333-342`, and the version/links fallback via P2a — NEITHER exposes
  it), breaking P6's "widening opens no new disclosure" invariant. Added P6-quarantine:
  the DETAIL path applies the single-memory `digest_visibility_failure(memory)` guard
  (`memory/digest_visibility.py:35`) to the fetched row and raises `memory_not_found` when
  set, dropping the digest regardless of team tag — identical to search/fallback. Added
  regression backend test 16g.
- round 10, finding 2 (major), fixed — CONFIRMED P4 term (a) used a team-UNSCOPED
  `RetrievalDocument...exists()` while real injection filters every document through
  `filter_documents_by_team_visibility` (`context/services.py:287-290,328`), so a
  `visibility_scope=PROJECT` memory whose only eligible document is a TEAM-scoped
  (other-team) document reported `authorized_for_injection=True` even though injection
  returns nothing AND P6-doc drops that document from the SAME detail render — an internal
  contradiction and a decision hazard. Since term (a) runs only on the request-scoped
  detail render (which already threads `inspection_scope`), changed it to
  `bool(filter_documents_by_team_visibility(memory.retrieval_documents.filter(stale=False,
  refuted=False), inspection_scope.scope))` so the field means "would injection return a
  document FOR THIS caller" and matches the P6-doc-filtered `retrieval_documents[]`;
  `inspection_scope is None` falls back to the unscoped `.exists()`. Updated the P4 note
  (team visibility IS folded on detail) and extended test 16c with the cross-team-document
  variant (`False` for the out-of-team key, `True` once `team_ids` includes team_b).
- round 10, finding 3 (major), fixed — CONFIRMED the literal P5 design (`SET TRANSACTION
  ISOLATION LEVEL REPEATABLE READ` inside a view-level `transaction.atomic()`) breaks the
  existing suite: audit-endpoint tests run under plain `@pytest.mark.django_db`
  (non-transactional, e.g. `inspection/inspection_api_tests.py:798-848`), which wraps each
  test in an outer transaction and runs setup INSERTs BEFORE the request, so the view's
  `atomic()` is only a savepoint and `SET TRANSACTION` after those queries raises SQLSTATE
  25001. (`ATOMIC_REQUESTS` is unset, so production autocommit would tolerate it, but the
  tests would not, and the earlier draft never converted them to `transaction=True`.)
  Redesigned P5 to a SINGLE windowed statement —
  `qs[offset:offset+limit].annotate(_total=Window(Count('id')))` with
  `total = page[0]._total if page else offset` — which draws count and page from ONE
  snapshot BY CONSTRUCTION under default READ COMMITTED, needs no `transaction.atomic()`,
  no `SET TRANSACTION`, and no test conversion, and has NO separate empty-page `count()`
  statement (so it satisfies round-8 finding 2 by construction and moots round-9 finding 2's
  connection-leak concern). Rewrote P5, test 19, and the API/Schema P5 summary accordingly.
- round 11, finding 1 (blocker), fixed — CONFIRMED the shared
  `ensure_memory_team_scope` (`memory/services.py:838-844`) denies ONLY when
  `team_id is not None`, so a `visibility_scope=TEAM, team_id=NULL` memory falls
  THROUGH it and the P1 version/links fallback would serve its body to any
  project-authorized key, while the primary P6 path
  (`Q(visibility_scope=TEAM, team_id__in=scope.team_ids)`) and retrieval
  (`filter_documents_by_team_visibility`, `context/services.py:287-290`, where
  `None ∉ allowed`) both REJECT it — `Memory.team` is nullable with no DB
  constraint tying TEAM to a non-null team (`core/models.py:716,761`). Redesigned
  P1 to fail CLOSED with the SAME predicate retrieval/P6 use
  (`visibility_scope == TEAM and team_id not in scope.team_ids`, which denies the
  null-team case) rather than reusing the fail-open shortcut, so the fallback can
  never leak a row the primary path 404s. Extended test 16 with the
  `TEAM, team_id=NULL` case on both version and links. (Tightening the shared
  `ensure_memory_team_scope` org-wide, affecting POST/diff, is noted Out of Scope
  for this read slice.)
- round 11, finding 2 (blocker), fixed — CONFIRMED `MemoryVersion` and
  `RetrievalDocument` carry independent `organization`/`project` FKs enforced only
  by `clean()`, not a DB constraint (`core/models.py:793,865,945`), while the
  detail render inlines children via the reverse relations
  `memory.versions.all()` / `memory.retrieval_documents.all()`
  (`inspection/views.py:273-276`) and P4 term (a) + P6-doc filtered those docs by
  TEAM visibility ONLY — never by org/project — so a mis-projected child would be
  inlined verbatim (leaking a foreign version `body` / document `full_text`) and
  could flip `authorized_for_injection` to `True`, whereas real retrieval
  hard-filters `organization`/`project` (`context/services.py:302,306-308`). Added
  P6-child: both child querysets are constrained to the parent's own
  `organization_id`/`project_id` (equal to the request scope, since the parent is
  P6-selected within `inspection_scope`) before any team/visibility filtering —
  applied to `versions[]`, P6-doc's `retrieval_documents[]`, and P4's eligibility
  `.exists()` (and its `inspection_scope is None` fallback). This makes the "never
  expose more than retrieval would" claim literally true and is a strict
  tightening. Added regression backend test 16h.
- round 11, finding 3 (major), fixed — CONFIRMED P5's `total = page[0]._total if
  page else offset` fabricates the count on an empty page: the audit list endpoint
  is a shared public inspection list whose `offset` serializer accepts any
  nonnegative value (`serializers.py:12`), so a consumer paginating past the end
  (`offset=200` over 3 rows) got `count=200` instead of the pre-P5 true total
  `count=3` (`qs.count()` regardless of offset) — a regression of the total-count
  contract that test 19's `offset=N` example masked (there `offset==count`, so
  `offset` coincidentally equals the true total). Changed the empty-page branch to
  `total = qs.count()` (restoring the true total); the NON-empty page keeps the
  single-statement `COUNT(*) OVER()` window the client's reconciliation relies on.
  The reintroduced fallback `count()` fires ONLY on the empty page, which the
  client never reaches (it issues `offset>0` only where append-only rows guarantee
  a non-empty page), so round-8 finding 2's soundness for the client is preserved.
  Updated the P5 mechanism/rationale and test 19 (empty-page true-total assertion;
  single-statement guarantee scoped to the non-empty page).
- round 12, superseded-by-teamlead-decision — teamlead decision 2026-07-20 S2
  replaces the entire two-request newest-window design with a small backend
  prerequisite: the audit-events inspection list now honors a whitelisted
  `ordering` param (`created_at` / `-created_at`, default `created_at` unchanged
  for console compatibility, id tiebreaker sign matching the ordering direction),
  mirroring the existing `MEMORY_ORDERING_FIELDS` + `_ordering` pattern
  (`inspection/services.py:56,76-78`) and replacing the hardcoded
  `.order_by('created_at', 'id')` (`:263`). `engram_audit` now makes ONE GET with
  `ordering=-created_at`, `limit`, and filters, and renders items as returned
  (newest first). This SUPERSEDES the fixes of the pagination/window chain —
  round 2 finding 3 (client newest-window `offset=count-limit` fetch), round 3
  finding 2 (client reverse-to-newest-first), round 3 finding 3 (two-request race
  reconciliation via `count2`), round 4 finding 1 (`count1` denominator), round 5
  finding 1 (best-effort window / `~` approximate denominator / "row count moved"
  note), round 6 finding 3 (first-page-FULL trigger), round 7 finding 1 (P5
  single-MVCC-snapshot count+page), round 8 finding 2 (P5 empty-page fallback
  race), round 9 finding 2 (P5 connection-isolation leak), round 10 finding 3 (P5
  single windowed `COUNT(*) OVER()` statement), and round 11 finding 3 (P5
  empty-page true-total) — all of which existed only to make the removed
  count-then-slice windowing sound. Removed: the count/slice windowing, offset
  math, first-page-FULL trigger, REPEATABLE-READ / single-snapshot P5 envelope,
  and all race-window disclosure notes and their tests (test 9c rewritten to a
  one-request newest-first + truncation-note assertion; backend test 19 rewritten
  to an ordering-param assertion). The truncation note is now a plain "showing N
  of M" line with a documented accepted-risk sentence that `count` and page come
  from two statements and may skew by in-flight writes — immaterial for a display
  note. Per the operator directive (2026-07-19) engram is a dogfood instance with
  no production deployment, and per decision S2 the count/slice race is an
  accepted, immaterial display-note risk; findings re-litigating this are not
  material.
- round 12, finding 1 (blocker), fixed — CONFIRMED the diff addendum
  (`GET /v1/memories/{memory_id}/diff`) resolves each side through
  `ResolveMemoryDiff._get_version` (`memory/services.py:951-956`), which filters
  `MemoryVersion.objects.filter(memory=memory, version=version_number)` with NO
  `organization_id`/`project_id` term; `MemoryVersion`'s parent-scope consistency
  is `clean()`-only (`core/models.py:818`; the only DB constraint is
  `(memory, version)` uniqueness, `:809-812`), so a version attached to an
  in-scope memory but carrying a FOREIGN scope — the exact row P6-child drops from
  the inspection `versions[]` — would be hidden from inspection yet disclosed
  (`body`) through this diff addendum, the very path this slice wires into
  `engram_memory_get`. Added prerequisite P6-child-diff: constrain the
  `_get_version` queryset to the parent memory's own
  `organization_id`/`project_id` (which equal the request scope, since
  `ResolveMemoryDiff.execute` already fetches `memory` by org/project at `:934-940`
  and applies `ensure_memory_team_scope` at `:942`), so a mis-projected version
  404s as `version_not_found` (rendered as the addendum's "diff unavailable" note)
  instead of leaking its body — byte-for-byte the same drop P6-child applies to
  `versions[]`. Added regression backend test 16i.
- round 12, finding 2 (blocker), fixed — CONFIRMED P1 never defined fail-closed
  behavior when the scoped parent memory does not exist: `MemoryVersionView.get`'s
  guard `if memory is not None and digest_visibility_failure(...)`
  (`memory/views.py:131-132`) short-circuits on `None` and falls through to the
  child query `MemoryVersion.objects.filter(org, project, memory_id)` (`:134-140`),
  and `MemoryLinksView.get` has no parent fetch at all (`:214-220`); since
  `MemoryVersion`/`MemoryLink` parent-scope consistency is `clean()`-only (no DB
  constraint), a child row whose OWN org/project equals the caller scope while its
  parent `Memory` lives in a foreign project is returned even though the primary
  (P6) path 404s that parent — contradicting P1's "a memory the primary path 404s
  can never leak through the fallback" invariant. Extended P1 to require, in BOTH
  GET handlers, returning the empty quarantine shape `{'count': 0, 'items': []}`
  when the parent fetch returns `None` (widen the guard to
  `if memory is None or digest_visibility_failure(memory) is not None:`), ordered
  fetch → `None`→empty → team-scope → child query, with `MemoryLinksView.get`
  gaining the parent fetch. Extended test 16 with mis-projected version/link rows
  asserting both GETs return the empty shape.
- round 13, finding 1 (blocker), fixed — CONFIRMED P6-child constrained only the
  inlined `versions[]`/`retrieval_documents[]`, but the detail render also
  populates top-level `source_session_id`/`source_correlation_id` from
  `_memory_source_provenance(memory)` (`inspection/views.py:270-272`), which
  iterates the UNFILTERED `memory.versions.all()` (`:214-228`) and emits the first
  version's `source_observation.session_id` + raw-event `correlation_id`;
  version→observation→session scope consistency is `clean()`-only
  (`core/models.py:579,818`), so a mis-projected version's foreign session/
  correlation ids leak through these two fields even after `versions[]` drops the
  same row — so "never expose more than retrieval would" was NOT literally true.
  Extended P6-child to require `_memory_source_provenance` iterate the SAME
  parent-scoped queryset (`memory.versions.filter(organization_id=...,
  project_id=...)`), and extended test 16h to assert both provenance fields come
  back `None` when the only version with a `source_observation` is mis-projected.
- round 13, finding 2 (major), fixed — CONFIRMED the P6 text claimed retrieval/
  search "admit a memory" by the memory's own `visibility_scope`/`team_id` and
  that P6 detail "admits exactly the memories `ensure_memory_team_scope`/search
  would". In reality retrieval authorizes the DOCUMENT
  (`filter_documents_by_team_visibility`, `context/services.py:287-289`) and then
  surfaces the parent memory's body (`search/services.py:63`), independent of the
  memory's own scope columns (the P7-tracked inconsistency,
  `memory/invariant_queries.py:1156`), so search's memory SET is NOT identical to
  the memory-visibility set (a TEAM memory with a PROJECT document is surfaced by
  search but denied by P6/fallback; a PROJECT memory with only another team's TEAM
  document is served by P6/fallback but not search). Corrected P6 to name the
  AUTHORITATIVE by-id projection precisely — the MEMORY-visibility rule
  (`ensure_memory_team_scope`, the fallback), which P6 matches byte-for-byte — and
  added a divergence subsection making DOCUMENT-level parity with retrieval a
  SEPARATE invariant enforced by P6-doc. No security weakening: P6/P1 remain the
  stricter side on the disclosure-relevant case, and the PROJECT-memory case is the
  established memory-visibility model the fallback already serves. No new test
  (16e control + 16f already pin both halves).
- round 13, finding 3 (major), fixed — CONFIRMED audit rows are written with
  `team_id=memory.team_id` (`memory/transitions.py:1248-1256`) and the audit list
  applies `team_filter` unconditionally (`inspection/services.py:257`), while P6
  widens the memory DETAIL read to admit a PROJECT-visibility team-tagged memory
  (the default realtime memory, `memory/transitions.py:1046,1053`); a project-
  scoped no-team key can thus read such a memory via `engram_memory_get` yet gets
  an EMPTY `engram_audit` trace (all its transition rows carry the excluded team
  tag). Added prerequisite P6-audit: a single-memory target read
  (`target_type='memory'`+`target_id`, the shape `engram_audit` always sends)
  authorizes the target by the P6 memory-visibility rule + digest quarantine and,
  when visible, runs the target-scoped audit query WITHOUT `team_filter` (all rows
  belong to that one authorized memory; the project-wide browse keeps `team_filter`
  unchanged), so `engram_audit` and `engram_memory_get` agree on which memories a
  key may inspect. Added backend test 16k.
- round 13, finding 4 (major), fixed — CONFIRMED the header `kind` token is NOT
  redacted upstream: `memory_response` reads `kind = metadata.get('kind') or None`
  from arbitrary JSON metadata (`inspection/views.py:238`) and returns it verbatim
  (`:261`), unlike `title`/`body`/`related` (`redacted_text`) and link
  `target`/`label` (`redact_value`) — so a secret-pattern value in
  `metadata['kind']` printed in the clear. Corrected the render note's false
  "redacted but not newline-escaped upstream" claim for `kind`, added a small
  backend guard redacting `kind` in `memory_response` (parity with title/body,
  safe on both render paths since legit vocabulary never matches a secret pattern),
  kept the control-char collapse for newline-forging, and added backend test 16j.
- round 13, finding 5 (major), fixed — CONFIRMED an internal scope contradiction:
  the intro said "three small backend guards" and Out of Scope allowed only
  "P1/P2/P3 + P3-index/P4/P5", both OMITTING the P6 family — a literal
  implementation could drop P6/P6-quarantine/P6-doc/P6-child/P6-child-diff,
  preserving the disclosure/consistency defects and failing tests 16e–16i.
  Rewrote both the intro and the Out-of-Scope bullet to enumerate the COMPLETE
  authoritative guard set (P1, P2a/b/c, P3, P3-index, P4, P5, and the full P6
  family incl. the new P6-audit) and to state that the P6 family is in scope and
  REQUIRED.
- round 13, finding 6 (major), fixed — CONFIRMED `mcp_server_tests.py` asserts a
  six-tool count in FOUR places, not the three the checklist enumerated: the
  omitted one is `test_run_server_handles_ndjson_round_trip`
  (`mcp_server_tests.py:455`, `assertEqual(6, len(lines[1]["result"]["tools"]))`),
  distinct from `test_run_mcp_serve_wires_build_tools_and_returns_zero` (`:504`).
  Following the old checklist would leave `:455` at 6 and red-light the CLI
  unittest lane (`.github/workflows/backend.yml:104`). Added the fourth location
  to the checklist and corrected the count from THREE to FOUR.
- round 14, superseded-by-teamlead-decision — teamlead decision S2 (memory_get,
  2026-07-20; operator directive 2026-07-19) cuts `engram_memory_get` back to the
  by-id `GET /v1/memories/<id>/version` + `/links` + optional `/diff` reads and
  DROPS the inspection memory DETAIL dependency entirely. Rich status fields
  (`status`, `confidence`, `kind`, stale/refuted validity,
  `authorized_for_injection`, related memories, retrieval documents, source
  provenance) are out of scope — the agent gets `kind`+`confidence` from
  `engram_search` and validity from its
  `conflict_excluded`/`stale_match`/`refuted_match` warnings (slice S3). This
  REMOVES the entire inspection-DETAIL-view team-scope/visibility hardening chain
  and its tests, superseding the fixes of: round 2 finding 2 (the P2b inspection
  `related[]` quarantine portion ONLY — the P2a links quarantine and P2c audit
  target-display scoping are KEPT); round 6 finding 1, round 7 finding 2, round 8
  finding 4, round 10 finding 2 (the P4 fail-closed `authorized_for_injection`
  chain); round 8 finding 1, round 13 finding 2 (P6 inspection-detail visibility
  scoping + memory↔document divergence); round 9 finding 1 (P6-doc detail
  `retrieval_documents[]` scoping); round 10 finding 1 (P6-quarantine); round 11
  finding 2, round 13 finding 1 (P6-child inlined
  `versions[]`/`retrieval_documents[]` + `_memory_source_provenance`
  parent-scope guards); round 12 finding 1 (P6-child-diff diff version-lookup
  parent-scope guard — now a tracked follow-up on the surfaced diff path, see Out
  of Scope); round 13 finding 3 (P6-audit single-memory trace visibility parity);
  and round 13 finding 4 (the `kind` redaction backend guard, moot now that no
  inspection `kind` is rendered). Removed backend tests 16b–16k and 17a with
  their prerequisites; removed client tests 3a (inspection null-token render) and
  6 (inspection-404 not-found, folded into the version-empty path, test 7) and
  13e (CLI inspection null-render); rewrote client tests 3/4 to the version/links
  render and 9b to link `target`/`label` newline injection. KEPT unchanged: P1
  (version/links fail-closed team scope, incl. null-team and
  missing/mis-projected-parent subrules — round 1 finding 1, round 11 finding 1,
  round 12 finding 2), P2a (links digest quarantine), P2c (audit
  `target_display` org+project+team_filter scoping — still referenced by the
  audit render), P3/P3-index (audit `target_id`+`target_type` filter + composite
  index), and P5 (audit `ordering` whitelist, itself the product of the round-12
  teamlead decision). Pre-existing inspection-DETAIL leaks
  (`_memory_source_provenance`, unfiltered detail `retrieval_documents`,
  audit-target org-wide title resolution where not covered by P2c, and the diff
  parent-scope guard) are tracked as a separate inspection-hardening follow-up,
  Out of Scope. Per the operator directive engram is a dogfood instance with no
  production deployment; findings re-litigating this scope cut or the descoped
  inspection-detail leaks are not material.
- round 15, finding 1 (blocker), fixed — CONFIRMED retrieval's
  `filter_documents_by_team_visibility` (`context/services.py:287-290`) is a
  WHITELIST admitting ONLY `PROJECT` or `TEAM ∧ team_id ∈ scope.team_ids`, while
  the P1 predicate as written (`deny when TEAM ∧ team_id ∉ scope.team_ids`) is a
  blacklist that fails OPEN for `SESSION` and `ORGANIZATION` rows
  (`VisibilityScope` permits all four, `core/models.py:52-56`; no DB constraint,
  `core/models.py:716,720,761`) — so the "SAME predicate / byte-for-byte
  consistent with retrieval" claim was false and version/links could serve a
  schema-valid `SESSION`/`ORGANIZATION` body/link. Redesigned P1 into a true
  whitelist: serve ONLY `PROJECT` or authorized-`TEAM`, raise `team_scope_denied`
  for every other scope (`SESSION`, `ORGANIZATION`, null-team `TEAM`,
  unauthorized `TEAM`). No live path creates `SESSION`/`ORGANIZATION` memories
  (curation requires PROJECT/TEAM, `core/models.py:2913-2914`) and retrieval
  never injects them, so the whitelist loses no reachable read while closing the
  schema-valid fall-through. Extended test 16 with SESSION/ORGANIZATION/null-team
  deny cases plus PROJECT/authorized-TEAM allow cases.
- round 15, finding 2 (major), fixed — CONFIRMED candidate supersession
  (`SUPERSEDE_MEMORY` outcome) creates a NEW winner via `_create_candidate_memory`
  but writes its SOLE audit event with `memory=loser`
  (`transitions.py:2136-2181`, single `_commit_transition` at `:2163` →
  `target_id=str(loser.id)`), only advancing the winner's pointer — so the
  newly-created winner has NO audit row of its own and `engram_audit` on it reads
  empty, yet the tool description implied supersede is covered. Added this WINNER
  side to the Design "does NOT return" list and reconciled the `engram_audit`
  description string (test 1a asserts the updated verbatim text) so the scope is
  accurate: the supersede is on the loser/source, the winner side is absent.
- round 15, finding 3 (minor), fixed — CONFIRMED the exact links template
  rendered only `<link_type>: <target>` while the sanitization rule and test 9b
  both treat `label` as an interpolated field, so an implementation could
  silently discard labels and still pass 9b. Made the observable contract
  explicit: `label` is appended as ` (<label>)` after `<target>` when non-empty
  (omitted with no trailing ` ()` otherwise), mirroring the audit
  `actor=<id> (<display>)` convention; extended test 3 to positively assert both
  the labeled and label-less render forms.
- round 16, finding 1 (blocker), fixed — CONFIRMED spec §Design line 17 promises
  version/links/diff are "each hardened to fail-closed team scope" (teamlead
  binding S2, 2026-07-20 lists all three), but the P1 body + the fail-open-shortcut
  parenthetical + Out of Scope left `/diff` on the fail-open
  `ensure_memory_team_scope` (`memory/services.py:942,838-844`), which admits
  `TEAM`/null-team, `SESSION`, and `ORGANIZATION` bodies that P1 denies on
  version/links — a direct contradiction of the binding. Extended P1 to cover the
  diff path: replace the `ensure_memory_team_scope` call at `:942` with the same
  `PROJECT`-or-authorized-`TEAM` whitelist; corrected the parenthetical
  (fail-open shortcut now unchanged only on the POST WRITE paths), the Out of
  Scope diff-guard deferral, the API/Schema and additive-guards summaries, and
  extended test 16 with diff 403/200 cases.
- round 16, finding 2 (blocker), fixed — CONFIRMED `ResolveMemoryDiff._get_version`
  (`memory/services.py:951-956`) filters only `(memory, version)` with no
  org/project term while `MemoryVersion` parent-scope consistency is `clean()`-only
  (`core/models.py:809-812,818`), so a mis-projected version attached to an
  in-scope memory leaks its foreign `body` through the newly-surfaced diff
  addendum. This is steady-state disclosure, which the operator directive does NOT
  waive (it waives only backward-compat / deployment choreography), so the earlier
  operator-directive deferral (former P6-child-diff) was invalid. Re-folded the
  round-12 fix into P1 as the diff-parent subrule (constrain `_get_version` to the
  parent's own `organization_id`/`project_id`; mis-projected version 404s as
  `version_not_found`), removed the Out-of-Scope deferral, and re-added the
  regression to test 16.
- round 16, finding 3 (major), fixed — CONFIRMED P2c scoped the audit
  `target_display` title lookup by `inspection_scope.team_filter`
  (`team__isnull OR team_id__in`, `inspection/services.py:52-53`), which ignores
  `visibility_scope` and admits `TEAM`/null-team, `SESSION`, and `ORGANIZATION`
  rows whose direct read P1 denies, so `_resolve_memory_targets`
  (`inspection/views.py:453-463`, org-only today) would still disclose their
  titles for an in-scope, unconstrained-`target_id` audit row
  (`core/models.py:1064-1065`). Replaced the `team_filter` scoping in P2c with the
  P1 visibility whitelist `Q(visibility_scope=PROJECT) | Q(visibility_scope=TEAM,
  team_id__in=scope.team_ids)` so a title resolves only for a memory the caller
  could read directly; extended test 17b with the in-project visibility-denied
  (`SESSION` / `TEAM`-null) fall-back case.
- round 9, finding 1 (minor), fixed — confirmed the spec mandates sanitizing
  `actor_display`/`target_display` (`:924-927`) but test 9b injected control
  chars only into reason/ids/types/link/header fields, never the displays, so an
  implementation could skip display sanitization with all tests green; the
  displays are raw operator-set names (`inspection/views.py:421,430` actor,
  `:476,489` target); extended test 9b to inject a newline into both
  `actor_display` and `target_display` and assert the annotated line stays one
  physical record.
- code-review round 3, finding 1, verdict fixed — docs/mcp-tools.md scoped the capability-specific reissue remediation to memory_get/audit only (other tools use generic _error_text by design).
- code-review round 3, finding 2, verdict fixed — stale acceptance line for team_scope_denied replaced with the context-aware contract implemented in read_tools.py:69 (project-wide/target_type-labelled audit denials, memory-labelled memory_get denials).
