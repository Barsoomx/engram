# S1 client-filters — MCP/CLI filter and render parity

Slice S1. Client-side only. Four coordinated changes to the MCP tool
schemas/handlers and CLI so the client can send filters the backend already
accepts and render enrichment fields the backend already returns:
(1) send the `kinds` filter (search + context); (2) render the search enrichment
fields `inclusion_reason`, `matched_terms`, and the top-level `warnings` array;
(3) surface context `items[]` (citations) and send `token_budget`/`kinds`; and
(4) expose the observation filters
(`observation_type`/`session_id`/`since`/`until`/`offset`) and render
`observed_at`/`session_id`.

This slice ships NO backend production-code edits. Earlier review rounds had
accumulated a backend + client "echo-redaction" design — server-side redaction of
`matched_terms`/`inclusion_reason` and of the `kinds` validation echo, a
strengthened embedded-JSON redactor in `core/redaction.py`, a client-side
`redact_token_shaped` helper, a `first_field_error` nested-error lifter, and a
backend↔client `SECRET_STRING_RE` parity guard. That ENTIRE design is SUPERSEDED
by the teamlead decision of 2026-07-20 (see Review Reconciliation round 12) and is
absent from this spec. Two rulings replace it:

- **`matched_terms` / `inclusion_reason` render with the same treatment as the
  memory body render** — server-provided content rendered verbatim, no
  client-side redaction. Terms and `inclusion_reason` are substrings of the same
  document material whose full body the search render already exposes verbatim,
  so they cannot leak anything the body does not already. Extra client-side
  redaction of these fields is rejected as security theater; this is a documented
  accepted risk anchored to body-render parity (see Error Handling).
- **`kinds` validation errors of ANY shape render a FIXED client-side message**
  naming the allowed kinds from a client-side constant that mirrors `MEMORY_KINDS`;
  the client does NOT echo any server-provided detail text, so there is no echo
  path and no client-side redaction parser is needed. Because `kinds` is validated
  at the serializer FIELD level, every rejection — membership (`validate_kinds`,
  detail `Invalid kind(s): …` echoing caller input), list-length
  (`ListField max_length=6`, which fires BEFORE `validate_kinds`), or item-length /
  blank (`CharField max_length=40`, also BEFORE `validate_kinds`) — puts `kinds` as
  the TOP-LEVEL key of the 400 body regardless of the nested value's shape, so the
  detector keys on that field PRESENCE (not on a nested code) and substitutes the
  fixed message for all three shapes, reading NOTHING out of the nested value (see
  Error Handling). This satisfies binding S1 and closes the membership detail's
  echo path without a client-side redaction parser. All other error codes keep the
  existing generic error rendering unchanged.

All edited files live under `packages/cli/engram_cli/` (canonical source). The
Claude and Codex plugins bundle byte-identical copies under
`packages/claude-plugin/hooks/engram_cli/` and
`packages/codex-plugin/hooks/engram_cli/`; a bundle byte-sync step is mandatory
in the implementation checklist. There are no backend production-code edits and no
`scripts/sync_plugin_bundle.py` parity-guard additions in this slice.

## Problem and Evidence

All refs verified against the working tree on 2026-07-19.

### 1. `kinds` filter accepted by server, never sent by client

- Search serializer accepts `kinds` (list, max 6): field
  `apps/backend/engram/search/serializers.py:51-56`, `validate_kinds`
  `apps/backend/engram/search/serializers.py:102-109`, cap
  `SEARCH_KINDS_MAX_ITEMS = 6` at `search/serializers.py:10`.
- Context serializer accepts `kinds` (list, max 6): field
  `apps/backend/engram/context/serializers.py:63-68`, `validate_kinds`
  `context/serializers.py:160-167`, cap `CONTEXT_KINDS_MAX_ITEMS = 6` at
  `context/serializers.py:13`.
- Valid values: `MEMORY_KINDS = ('decision', 'convention', 'gotcha',
  'architecture', 'incident', 'digest')` at
  `apps/backend/engram/core/models.py:134`.
- Client never sends `kinds`. `search_memory` payload build
  `packages/cli/engram_cli/mcp_tools.py:143-150` has no `kinds`; `fetch_context`
  payload build `mcp_tools.py:190-200` has no `kinds`; MCP `engram_search`
  inputSchema `packages/cli/engram_cli/mcp_server.py:93-103` and
  `engram_context` inputSchema `mcp_server.py:113-125` expose no `kinds`. CLI
  `search` subparser `packages/cli/engram_cli/main.py:162-169` has no `--kind`,
  and `build_search_payload` `commands.py:1942-1966` never sets it.

### 2. Search render drops enrichment the server already returns

- Search response items carry `inclusion_reason` and `matched_terms`, and the
  response carries top-level `warnings`: `SearchResult.to_response`
  `apps/backend/engram/search/services.py:45-51` (`'warnings'` at `:50`),
  `_item_response` returns `inclusion_reason` (`search/services.py:66`) and
  `matched_terms` (`search/services.py:72`).
- Warning shape is `{code, message, memory_id}` via
  `RetrievalWarning.to_dict` `apps/backend/engram/context/retrieval_warnings.py:33-34`.
  Codes emitted ON THE SEARCH PATH: `semantic_unavailable`, `stale_match`,
  `refuted_match` (`retrieval_warnings.py:42-118`) and `conflicting_memory`
  (`retrieval_warnings.py:134-177`). NOTE: `budget_dropped` is NOT a search
  warning — `SearchMemories._result` calls `compute_retrieval_warnings` without
  `dropped_for_budget` (`search/services.py:160-171`), so it defaults to 0 and
  `budget_dropped` only fires for positive drops (`retrieval_warnings.py:180-197`),
  which is a context-budget concern, not search. The `render_warnings` helper is
  code-agnostic (it renders whatever codes arrive), so this does not change the
  render — but the client render must not ASSUME `budget_dropped` on search, and
  the example block below is illustrative of the shared renderer, not a claim
  that search emits every listed code.
- MCP `search_memory` render `mcp_tools.py:165-175` prints only citation, title,
  memory_id, `search_item_suffix`, and body — it drops `inclusion_reason`,
  `matched_terms`, and the whole `warnings` array.
- CLI `run_search` render `commands.py:2064-2066` prints only citation, title,
  suffix, body — same drops. `search_item_suffix` at `commands.py:2000-2007`
  renders only kind + confidence.

### 3. `engram_context` drops `items[]`, cannot send `token_budget` or `kinds`

- `fetch_context` returns only `rendered_context` (`mcp_tools.py:211-215`),
  discarding `items[]`. After a `/clear` the caller sees citation markers `[Mn]`
  in the rendered text but has no map from `[Mn]` to `memory_id`.
- Context response DOES include `items` (with `citation`, `memory_id`, `kind`,
  `confidence`, `inclusion_reason`, `matched_terms`) and `warnings`:
  `ContextBundleResult.to_response` (the class is `ContextBundleResult`,
  `context/services.py:122-128`; `to_response` at
  `apps/backend/engram/context/services.py:128-154`),
  `_item_response` `context/services.py:174-190` (`citation` `:179`, `memory_id`
  `:180`, `confidence` `:185`, `kind` `:186`). The client Citations render exposes
  only `memory_id`/`kind`/`confidence` (not `inclusion_reason`/`matched_terms`).
- `token_budget` is accepted (`context/serializers.py:70`,
  `min_value=1`, default `None`) but the MCP path never sends it; only the hook
  path sends it (`commands.py:1478` `= 1200`, `commands.py:1516` `= 2000`, via
  constants `USER_PROMPT_SUBMIT_TOKEN_BUDGET`/`SESSION_START_TOKEN_BUDGET` at
  `commands.py:66-67`). MCP `engram_context` inputSchema `mcp_server.py:113-125`
  exposes neither `token_budget` nor `kinds`.
- There is no CLI `context` subcommand (subparsers in `main.py:86-218` define
  `connect/install/doctor/disconnect/mcp-install/mcp/hook/search/memory/observations/import`
  only). CLI parity for context is therefore N/A.

### 4. `engram_observations` exposes only limit + project_id

- Server accepts `observation_type`, `session_id`, `since`, `until`, `offset`
  (this slice), and additionally `correlation_id` (deferred — see Out of Scope):
  view wiring `apps/backend/engram/observations/views.py:54-60`, serializer
  fields `apps/backend/engram/observations/serializers.py:34-41`
  (`limit` `:34` max `OBSERVATION_LIST_LIMIT_MAX = 100` at `:5`; `offset` `:35`
  min 0; `observation_type` CharField `:38`; `session_id` UUIDField `:39`;
  `since` DateTimeField `:40`; `until` DateTimeField `:41`).
- MCP `engram_observations` inputSchema exposes only `limit` and `project_id`
  (`mcp_server.py:156-164`, properties at `:158-161`). Handler
  `list_observations` `mcp_tools.py:263-269` only forwards `limit`,
  `project_id`/`repository_url`, `team_id`.
- Observation render drops `observed_at` and `session_id`, both present in the
  response body: `observation_response` returns `session_id`
  (`observations/services.py:44`), `observation_type` (`:46`), `observed_at`
  (`:61`); MCP render `mcp_tools.py:284-293` prints only observation_type,
  title, body. CLI `run_observations` render `commands.py:2271-2273` does the
  same. CLI `observations` subparser `main.py:201-204` has only `--limit`,
  `--config-dir`, `--project`.

## Design

Smallest additive change set; the server already validates every new value, so
the client does not re-validate beyond shape.

Decisions:

1. **Pass `kinds` through untyped-beyond-list.** Client sends `kinds` only when
   non-empty; server enforces max 6 and membership. Rejected: client-side enum
   validation — duplicates server logic and drifts when `MEMORY_KINDS` changes.
2. **Omit-when-present-check for all new optional params** (kinds, token_budget,
   and the four observation filters). Precise gate: `kinds` and the observation
   string filters are sent only when non-empty; `token_budget` is gated on
   `is not None` (NOT falsy), so an explicit `token_budget=0` IS forwarded and
   lets the server reject it (`min_value=1`). `offset` uses a different, explicit
   rule: it is forwarded only when non-zero and omitted when `0`, because `0` is
   the server default (`observations/serializers.py:35`) and a valid no-op, so
   omitting it keeps request bodies minimal without changing behavior. Rejected:
   always sending defaults —
   needlessly changes request bodies and risks tripping server `required=False`
   semantics for `session_id`/dates.
3. **Render enrichment is additive and compact (SEARCH path, plus the empty
   paths).** Per item, add a `match:` line only when `inclusion_reason` or
   `matched_terms` is present; add a client `Warnings:` block whenever `warnings`
   is non-empty. **IMPORTANT scope of the client `Warnings:` block — it is NOT
   unconditional across all three tools.** It applies to: (a) the SEARCH render
   on BOTH paths (populated items and empty items), and (b) the CONTEXT render
   ONLY when `rendered_context` is empty. It MUST NOT be emitted on the
   *non-empty* context path, because the context backend already embeds warnings
   inside `rendered_context`: `_render_context`
   (`apps/backend/engram/context/services.py:1281-1307`) appends a `> Warnings:`
   block (`> - <message>` per warning) to the rendered text whenever warnings
   exist, and the response simultaneously carries structured `warnings`
   (`context/services.py:145-154`; dual representation proven by
   `context_api_tests.py:1892-1899`). A literal client `Warnings:` block on the
   non-empty context path would therefore print every warning TWICE (once from
   `rendered_context`, once from the client block). Search does NOT embed
   warnings in any rendered text, so the client block is the only surface there.
   Zero items with a warning is a normal backend result, not a corner case: the
   search stale-match path returns `items == []` together with a `stale_match`
   warning (`apps/backend/engram/search/search_api_tests.py:489,517-524`), and
   that warning is the single most useful explanation for an empty search. THREE
   early-return sites drop the warning today and ALL must be fixed: the MCP
   `search_memory` empty-items early return (`mcp_tools.py:161-163`), the MCP
   context empty-render early return (`mcp_tools.py:211-213`), AND the CLI
   `run_search` empty-items early return (`commands.py:2059-2062`, which today
   writes `No memory matched the search.` and returns before any warning render).
   Each must render the warnings block before/with the empty message rather than
   dropping it (see Error Handling). Rejected: always emitting the block even
   with zero warnings — noisy for the common zero-warning case. Rejected: adding
   the client block to the non-empty context path — duplicates the warnings
   `rendered_context` already carries.
4. **Context citations block built from `items[]`.** On the non-empty context
   path the client appends ONLY a `Citations:` block after `rendered_context`,
   one line per item mapping `[Mn]` to `memory_id/kind/confidence`; it does NOT
   add a client `Warnings:` block there, because `rendered_context` already
   contains the backend's `> Warnings:` block (decision 3). The Citations block
   is gated on `items[]` being non-empty, NOT on `rendered_context` being
   non-empty: `render_citations([])` returns an empty string and appends nothing
   — not even a bare `Citations:` header. This resolves the third real backend
   state, `items == []` WITH a non-empty `rendered_context`: the session-start
   empty bundle returns `rendered_context ==
   '# Engram context\n\nNo approved memory matched this request.'` with
   `items == []` (`_render_context` non-`user_prompt_submit` branch,
   `context/services.py:1287-1291`, proven by `context_api_tests.py:1801-1803`).
   That response takes the non-empty-`rendered_context` path (so it is NOT the
   `Engram returned no context for this session.` empty message) and renders the
   bundle text VERBATIM with NO Citations block appended, because `items` is
   empty. Only `rendered_context`'s own embedded `> Warnings:` block (if any)
   appears; the client adds nothing. When
   `rendered_context` is empty, there is no embedded warning text, so the client
   DOES render the `Warnings:` block if `warnings` is non-empty: the backend's
   quarantine
   response deliberately returns empty `rendered_context` and `items` plus the
   safety warning `context_bundle_digest_visibility_unproven`
   (`apps/backend/engram/context/services.py:155-171`), and hiding that signal
   would defeat the slice. So the empty-render path emits `Engram returned no
   context for this session.` followed by the warnings block when present.
   Rejected: returning JSON — breaks the existing text-content contract and the
   "rendered bundle" UX.
5. **Reuse `search_item_suffix` unchanged**; add ALL new render via small
   shared helpers in `commands.py` — `search_match_line(item)`,
   `render_warnings(warnings)`, `render_citations(items)`, and
   `observation_meta_line(item)` (the `observed_at=... session_id=...` line) — so
   both the MCP path (`mcp_tools.py`) and the CLI path (`commands.py`) call the
   SAME function and the additive lines are byte-identical by construction, not
   by coincidence. The observation metadata line in particular must come from the
   one shared `observation_meta_line`, NOT two local renderers in
   `mcp_tools.py:284-293` and `commands.py:2271-2273`; two local copies could
   drift and would defeat the byte-identical claim. Rejected: duplicating any
   render in `mcp_tools.py`.
6. **No CLI `context` command added** (out of scope, and none exists to extend).
7. **`--json` CLI path unchanged** (`run_search` `commands.py:2054-2057` dumps
   the raw body) — enrichment already present in the body.

## API and Schema Changes

No backend endpoints, response *shapes*, serializer FIELDS, validation rules, or
accepted values change. This slice is client-side only. Endpoints used are
unchanged: `POST /v1/search/`, `POST /v1/context/session-start`,
`GET /v1/observations/`. The client sends filters the serializers already accept
and renders fields the responses already return; the server remains authoritative
for all validation.

### MCP inputSchema additions (`mcp_server.py`)

`engram_search` properties gain:

```json
"kinds": {"type": "array", "items": {"type": "string"}}
```

`engram_context` properties gain:

```json
"kinds": {"type": "array", "items": {"type": "string"}},
"token_budget": {"type": "integer"}
```

`engram_observations` properties gain (the `since`/`until` `description` strings
are mandatory, not optional prose — MCP callers get no CLI `--help` text, so the
non-obvious ingestion-time semantics MUST travel in the schema itself):

```json
"observation_type": {"type": "string"},
"session_id": {"type": "string"},
"since": {
  "type": "string",
  "description": "ISO-8601 lower bound (inclusive) on ingestion time (created_at), NOT the displayed observed_at. With delayed ingestion a returned row's observed_at may fall outside this window."
},
"until": {
  "type": "string",
  "description": "ISO-8601 upper bound (exclusive) on ingestion time (created_at), NOT the displayed observed_at. A row whose created_at equals until is excluded."
},
"offset": {"type": "integer"}
```

`required` arrays are unchanged for all three tools.

Additionally, append one sentence to the `engram_observations` tool description
so the created_at/observed_at distinction is visible even to a caller that reads
only the top-level description, verbatim target text:

> Time filters since/until bound ingestion time (created_at, until exclusive);
> results still display and sort by observed_at.

This description edit is subject to the same exact-equality test update as the
search/context descriptions (see Test Plan item 1).

### MCP tool description updates (`mcp_server.py`)

Append to `engram_search` description and `engram_context` description one
sentence advertising the pattern, verbatim target text:

> Filter by kinds=[convention,decision] to fetch project conventions or
> decisions on a topic (e.g. gitlab workflow).

### MCP handler passthrough (`mcp_tools.py`)

The MCP server does NOT enforce `inputSchema` — `handle_request`
(`mcp_server.py:205-257`; the `tools/call` branch at `:229-245` shallow-copies
`arguments` and calls the handler), so a malformed caller could pass `kinds` as a
string or `offset` as a bool. Each passthrough therefore gates on the concrete
TYPE (`isinstance`), not truthiness.

**Present-but-wrong-shape is a HARD LOCAL ERROR, not a silent drop (fail closed,
not fail open).** A narrowing filter that arrives present-but-mis-typed MUST NOT
be treated as absent: silently dropping `kinds='gotcha'` would run an all-kinds
search, and dropping a malformed `session_id`/`observation_type`/time selector
would run an UNFILTERED observations query — in both cases returning materially
BROADER authorized data than the caller asked to narrow to. That is a fail-open
regression even though it never crosses authorization scope (the server still
scopes every row). So the rule is:
- **absent** — key missing, `None`, empty string `''`, or empty list `[]` — is
  omitted from the payload/params (a genuine "no filter" request); and
- **present but wrong-shaped** — a non-empty value whose concrete type is wrong —
  raises a local `ValueError` from the handler. `handle_request` already wraps
  every `tools/call` in a try/except that turns a handler exception into a
  JSON-RPC error (`mcp_server.py:244-251`), so the malformed narrowing request
  fails closed with a message telling the caller to fix the shape, instead of
  silently widening the result set.

The CLI path cannot hit these wrong-shape cases (argparse already enforces
`--offset type=int` and `--kind action=append`), so the raise-on-wrong-shape
guard lives in the MCP handlers only.

- `search_memory`: `arguments.get('kinds')` — when a non-empty `list`
  (`isinstance(value, list) and value`), set `payload['kinds']`; when absent/
  `None`/`[]`, omit; when present as a non-list (e.g. the bare string
  `'gotcha'`), raise `ValueError('kinds must be an array of strings')`.
- `fetch_context`: same list rule for `kinds` (raise on a present non-list). For
  `token_budget`: when an integer (`isinstance(value, int) and not
  isinstance(value, bool)`), set `payload['token_budget']` — gate on presence/
  type, NOT falsy `value or ...`, because `token_budget=0` is a valid non-null
  integer that must reach the server so its `min_value=1` validation runs and
  rejects it; when absent/`None`, omit; when present as a bool or non-int (e.g.
  `True`, `'5'`), raise `ValueError('token_budget must be an integer')`. (Note:
  `token_budget` is a budget, not a scope-narrowing filter, so dropping it would
  not broaden authorized data — but it is raised anyway for uniform caller-bug
  feedback under the one present-but-wrong-shape rule.)
- `fetch_context` request_id (replay-widening guard): the handler MUST mint a
  FRESH request_id every call and MUST NOT honor a caller-supplied
  `arguments['request_id']`. Replace `_new_request_id(arguments)` at
  `mcp_tools.py:194` with a bare `f'mcp-{uuid.uuid4()}'` for THIS handler only.
  Rationale: `request_id` is not in the `engram_context` inputSchema (its
  exposure is deferred to slice S6, see Out of Scope), so no well-behaved caller
  sends one; but `handle_request` does not enforce the schema, so a hidden extra
  `request_id` reaches the handler, and `BuildContextBundle` returns any existing
  bundle keyed on `(organization, project, request_id)`
  (`context/services.py:901`, `core/models.py:980`) WITHOUT re-applying `kinds`
  or `token_budget`. Reusing an id with a NARROWER `kinds` or LOWER
  `token_budget` would therefore return the earlier, BROADER cached bundle — the
  exact silent fail-open widening this section forbids for the request body, now
  via the replay key. Minting fresh closes it and makes every `engram_context`
  re-request reflect the current filters. This is deliberately narrower than the
  three write handlers (`create_memory_link`, `update_memory_version`,
  `submit_memory_feedback`), which KEEP `_new_request_id(arguments)`: there
  `request_id` is a mutation idempotency key with no narrowing filter to drop, so
  honoring a caller value is correct (and covered by
  `test_explicit_request_id_wins`). When slice S6 exposes `request_id` on
  `engram_context` it MUST first add replay fingerprinting — reject a reused id
  whose `kinds`/`token_budget` differ from the stored bundle — before honoring a
  caller value.
- `list_observations`: gate each argument on its CONCRETE type, raising on a
  present wrong-shaped value:
  - `observation_type`, `session_id`, `since`, `until`: add to the query `params`
    dict (as `str(value)`, a no-op for strings) ONLY when
    `isinstance(value, str) and value`; omit when absent/`None`/`''`; raise
    `ValueError('<name> must be a string')` when present as a non-string (e.g.
    `observation_type=['tool_use']`). It must NOT be `str()`-coerced into the
    misleading param `"['tool_use']"`, and it must NOT be silently dropped (which
    would run the query unfiltered on that field).
  - `offset`: forward ONLY when
    `isinstance(value, int) and not isinstance(value, bool) and value != 0`; omit
    when absent/`None`/`0` (`0` is the server default,
    `observations/serializers.py:35`, so omitting it is a valid no-op that keeps
    the query string minimal); raise `ValueError('offset must be an integer')`
    when present as a bool or non-int. The `not isinstance(value, bool)` clause is
    mandatory because `bool` is an `int` subclass and `offset=True` would
    otherwise pass a "non-zero integer" check and be forwarded as `offset=1`.
    This omit-`0` behavior is deliberately different from `token_budget=0`, which
    is forwarded (invalid, so the server rejects it) rather than omitted.

### CLI flags (`main.py`)

`search` subparser gains:

```python
search.add_argument('--kind', action='append', default=[], dest='kinds')
```

`observations` subparser gains:

```python
observations.add_argument('--session-id', dest='session_id', default='')
observations.add_argument('--type', dest='observation_type', default='')
observations.add_argument('--since', default='', help=_SINCE_UNTIL_HELP)
observations.add_argument('--until', default='', help=_SINCE_UNTIL_HELP)
observations.add_argument('--offset', type=int, default=0)
```

The `--since`/`--until` help text is owned HERE (in `main.py`, where the
argparse flags are defined — NOT in `commands.py`, which only reads
`args.since`/`args.until` and never sets help). Define one module-level constant
in `main.py` and pass it to both flags:

```python
_SINCE_UNTIL_HELP = (
    'Filter on ingestion time (created_at), not the displayed observed_at. '
    '--since is inclusive (>=); --until is exclusive (<).'
)
```

The exact required text is mandatory and MUST state BOTH bounds: `--since` is
inclusive and `--until` is exclusive. The earlier draft string
(`--since/--until filter on ingestion time (created_at); --until is exclusive`)
was defective — it never stated `--since` is inclusive — and is replaced by the
constant above.

`--since`/`--until` semantics (both the flag help text and the spec state this):
the server filters `--since`/`--until` on the observation's `created_at`
(ingestion time), **not** on the displayed `observed_at`
(`apps/backend/engram/core/api/filters.py:6-8`). `--since` is inclusive (`gte`),
`--until` is exclusive (`lt`). Results are ordered by `observed_at` first, then
`created_at` (`apps/backend/engram/observations/services.py:95`), and the render
shows `observed_at`. Consequence: with delayed ingestion (`observed_at` well
before `created_at`) a row can be displayed with an `observed_at` outside the
`--since`/`--until` window, and a boundary timestamp equal to `--until` is
excluded.

### CLI payload/param wiring (`commands.py`)

- `build_search_payload` gains a `kinds: list[str]` keyword; sets
  `payload['kinds']` only when non-empty. `run_search` passes
  `list(args.kinds or [])`.
- `run_observations` adds the four filters + offset to `params` when present
  (mirrors MCP omit-when-empty; `--offset 0` omitted).

### Render format examples

IMPORTANT: MCP and CLI keep their existing, *different* item headers — this
slice changes only the additive lines (`match:`, `Warnings:`, `Citations:`,
`observed_at/session_id`), never the header. The examples below therefore show
each header in its own path; the additive lines are byte-identical across paths
because they come from the shared `commands.py` helpers.

Search item — CLI header (`commands.py:2065` `M1: <title><suffix>`):

```
M1: Gitlab workflow convention [convention, conf 0.920]
  match: exact match: gitlab | terms: gitlab, workflow
  Use draft MRs and label stage: waiting for review.
```

Search item — MCP header (`mcp_tools.py:170` `[M1] <title> (memory_id=...)`):

```
[M1] Gitlab workflow convention (memory_id=abc-123) [convention, conf 0.920]
  match: exact match: gitlab | terms: gitlab, workflow
  Use draft MRs and label stage: waiting for review.
```

`search_match_line(item)` renders `inclusion_reason` and every `matched_terms`
term VERBATIM — the same treatment as the memory body render, no client-side
redaction (accepted risk by body-render parity; see Error Handling). Per-segment
format (so the byte-identical claim is well-defined, including for a filter-only
result): the line is
`  match: <inclusion_reason>`, and the ` | terms: <comma-joined matched_terms>`
segment is appended ONLY when `matched_terms` is a non-empty list. When
`matched_terms` is empty the line is just `  match: <inclusion_reason>` with NO
trailing ` | terms:` segment. This is a real case: the filter-only retrieval
path returns `inclusion_reason='filter-only authorized memory'` with
`matched_terms=()` (`context/services.py:398-404`), which renders exactly
`  match: filter-only authorized memory` (no terms segment). Symmetrically, if
`inclusion_reason` is blank but `matched_terms` is non-empty, render
`  match: | terms: ...`? No — instead render only `  terms: <...>` with no empty
`match:` prefix. The whole line is emitted only when at least one of
`inclusion_reason`/`matched_terms` is present (decision 3). Cover the filter-only
(terms-absent) case in the render test.

The `[convention, conf 0.920]` suffix is produced by the existing
`search_item_suffix` (`commands.py:2000-2007`), which already omits blank `kind`
and null/blank `confidence` — no change needed for the search-item suffix.

Warnings block, only when `warnings` non-empty (shared, both paths, and also
emitted after the empty-result message per Error Handling):

```
Warnings:
  [stale_match] stale memory matched: "old deploy step" (memory_id=abc-123)
  [budget_dropped] 2 matching memories dropped for token budget
  [context_bundle_digest_visibility_unproven]
```

Per-line format rules for `render_warnings`, so the safety signal is never
garbled (the `code` is the load-bearing part and MUST always be preserved):

- The `code` is always rendered inside `[...]`. A warning with no `code` (should
  never happen from the backend) is skipped entirely rather than rendering an
  empty `[]`.
- `message` is OPTIONAL. Some real warnings carry no `message` — the quarantine
  safety warning is exactly `{'code': 'context_bundle_digest_visibility_unproven'}`
  with neither `message` nor `memory_id` (`context/services.py:171`). When
  `message` is absent, `None`, or blank, render the code-only line `[code]`
  (no trailing space, no literal `None`), as the third example line above.
- `memory_id=...` suffix is appended only when the warning carries a non-blank
  `memory_id`; omitted otherwise.

So the render is `[code]`, optionally ` <message>`, optionally ` (memory_id=<id>)`,
each segment included only when its source value is non-blank.

Context (MCP), after `rendered_context`:

```
<rendered_context text>

Citations:
  [M1] memory_id=abc-123 kind=convention confidence=0.920
  [M2] memory_id=def-456 kind=gotcha confidence=0.780
```

Context items commonly have `confidence == None` and `kind == ''`: both fields
are nullable/blank-default at the model (`core/models.py:726,730`) and the
response returns them verbatim (`context/services.py:184-186` →
`confidence: str(...) if not None else None`, `kind: memory.kind`), proven by
the default-case API test (`context/context_api_tests.py:349-366` —
`'confidence': None`, `'kind': ''`). The Citations line therefore **omits each
of `kind=` and `confidence=` independently when its value is absent** (`kind`
empty/missing, or `confidence` `None`). A fully-unpopulated item renders just
`[M1] memory_id=abc-123`. Formatting is the raw response string for
`confidence` (no reformatting/padding); `kind` is the raw string.

Empty `items`: when `rendered_context` is empty, return `Engram returned no
context for this session.` — followed by the `Warnings:` block when `warnings`
is non-empty (see Error Handling; e.g. the quarantine safety warning). No
Citations block when there are no items.

Observation item — MCP header (`mcp_tools.py:288` `[type] <title>`) and CLI
header (`commands.py:2271` `type: <title>`) are unchanged and differ; only the
added second line is shared:

```
# MCP
[user_prompt] Investigate deploy failure
  observed_at=2026-07-18T20:15:03+00:00 session_id=9f2c...-...
  User asked to look at the failing deploy pipeline.
# CLI
user_prompt: Investigate deploy failure
  observed_at=2026-07-18T20:15:03+00:00 session_id=9f2c...-...
  User asked to look at the failing deploy pipeline.
```

`observed_at`/`session_id` line is emitted only when at least one of the two is
present; missing values render as empty and are skipped. NOTE on displayed
`observed_at`: the list is ordered by `observed_at` first
(`observations/services.py:95` `order_by('-observed_at', '-created_at')`) and
this displayed field is `observed_at`, but the `--since`/`--until` filters act
on `created_at` (`apps/backend/engram/core/api/filters.py:6-8`, `since` inclusive
`gte`, `until` exclusive `lt`) — see CLI flags below for the documented
semantics.

## Data Flow

1. MCP client (Claude/Codex) calls a tool with new optional args → `mcp_server`
   forwards the caller's PUBLIC `arguments` to the bound handler in `mcp_tools`.
   Precisely (`mcp_server.py:229-236`): it shallow-copies `arguments`, strips the
   caller-supplied internal `INTERNAL_REPOSITORY_URL_ARGUMENT` and conditionally
   re-injects a trusted repository-scope value, then passes the dict on. All
   PUBLIC S1 fields (`kinds`, `token_budget`, the observation filters) pass
   through untouched; only the internal repository-scope key is sanitized. (The
   earlier "forwards `arguments` unchanged" wording ignored this sanitization.)
2. Handler builds the request body/params, adding new fields only when present,
   and POSTs/GETs to the same endpoints via the injected `Transport`.
3. Server validates (`kinds` membership/cap, `token_budget` min, `session_id`
   UUID, `since`/`until` datetime, `offset` min) and returns the existing
   response shapes.
4. Handler renders text: search/observations gain match/warnings/observed_at
   lines; context appends a Citations block from `items[]`.
5. CLI path is identical except args come from argparse and there is no context
   command.

## Error Handling

- **Accepted risk — search enrichment render (body-render parity).** The search
  `match:` line renders `inclusion_reason` and each `matched_terms` term VERBATIM,
  with the same treatment as the memory body render. Terms and `inclusion_reason`
  are substrings of the same server-provided document material whose full body the
  search render already exposes verbatim, so they cannot leak anything the body
  does not already. Extra client-side redaction of these two fields is therefore
  rejected as security theater; this is a documented accepted risk anchored to
  body-render parity (teamlead decision 2026-07-20). The client carries NO
  token-shaped or JSON-key redaction helper.

- **`kinds` validation errors (ANY shape) — fixed client-side message (no server
  echo).** Per binding teamlead decision S1 (2026-07-20), a `kinds` filter
  rejected for ANY reason — membership, list-length, or item-length — renders one
  FIXED client-side message naming the allowed kinds and echoes NO server detail.
  Because `kinds` is validated at the serializer FIELD level, every rejection puts
  the field name `kinds` as the TOP-LEVEL key of the 400 body (field-level DRF
  nesting; the top-level-key-is-field-name shape is proven for the sibling `query`
  field by `context_api_tests.py:459`), regardless of the nested value's shape.
  The three shapes are:
  1. **membership** (`validate_kinds`, `search/serializers.py:102-109`,
     `context/serializers.py:160-167`) →
     `{'kinds': {'code': ['search_kinds_invalid'], 'detail': ['Invalid kind(s): …']}}`
     (search) / `{'kinds': {'code': ['context_kinds_invalid'], …}}` (context) — a
     dict carrying a nested `code`, with `detail` echoing caller input;
  2. **list-length** (`ListField` `max_length=6`, `search/serializers.py:51-56`,
     `context/serializers.py:63-68`; seven-item 400 tested at
     `search_api_tests.py:628`, `context_api_tests.py:2266`) →
     `{'kinds': ['Ensure this field has no more than 6 elements.']}` — a PLAIN
     LIST of strings;
  3. **item-length / blank** (`CharField(max_length=40)`) → the DRF child-index
     shape `{'kinds': {'0': ['Ensure this field has no more than 40 characters.']}}`
     — a DICT keyed by string index, no `code` key.

  The detector is therefore keyed on FIELD PRESENCE, not on the nested shape or
  code: when either decoder (`_error_text` `mcp_tools.py:400-407`; `error_from_body`
  `commands.py:1667-1675`) processes an error body (only reached on a non-2xx
  response) that is a dict carrying a top-level `kinds` key — with ANY nested value
  (dict with `code`, DRF child-index dict, or plain list) — it substitutes the
  FIXED, fully client-side message that names the allowed kinds from a client-side
  constant mirroring `MEMORY_KINDS`
  (`decision, convention, gotcha, architecture, incident, digest`):

  ```
  Invalid kind filter. Allowed kinds: decision, convention, gotcha, architecture, incident, digest.
  ```

  The client reads NOTHING out of the nested value — no `detail`, no DRF template
  string, no index — so the caller-supplied content the membership `detail` echoes
  (`Invalid kind(s): …`, e.g. a token- or JSON-key-shaped `kinds` entry) never
  reaches client output. The list-length and item-length DRF templates interpolate
  no caller input, but they are covered by the same fixed message anyway (S1
  requires uniform rendering, not a per-shape echo-risk assessment). Because the
  message is a client-side constant there is no echo path and no client-side
  redaction parser. Define the allowed-kinds tuple and the fixed message once in
  `commands.py` (`_ALLOWED_KINDS` / `KINDS_ERROR_MESSAGE`) and reference them from
  both decoders so the shared `KINDS_ERROR_MESSAGE` CONSTANT is byte-identical
  across the MCP and CLI paths and both bundles. Byte-identity is scoped to that
  message constant, NOT to each path's full rendered output: the two decoders keep
  their EXISTING, different envelopes — MCP `_error_text` returns the bare message
  (like `PROJECT_NOT_FOUND_MESSAGE`, `mcp_tools.py:402-403`), while CLI
  `error_from_body`→`emit_error` wraps it as `<code>: <KINDS_ERROR_MESSAGE>` plus a
  `remediation:` line (`commands.py:1667-1675,1772-1774`). For the CLI envelope's
  `<code>` slot, use a fixed client-side sentinel (e.g. `invalid_kind_filter`) for
  every `kinds` shape, since the list-length and item-length bodies carry no
  server `code` to key on and S1 forbids echoing server detail. The render tests
  (Test Plan item 2) assert the shared allowed-kinds substring IS present and any
  server-echoed substring is ABSENT on BOTH paths for ALL THREE shapes; they do
  NOT assert the two full outputs equal each other (they differ by envelope, which
  is not a defect). This detection is narrow — it recognizes only the top-level
  `kinds` field and substitutes a constant — it is NOT a general nested-error
  lifter, and it leaves every non-`kinds` field error on its existing generic
  path.

- **All other validation / HTTP errors — existing generic rendering, unchanged.**
  Non-`kinds` validation failures (`token_budget` `min_value=1`
  `context/serializers.py:70`; `session_id` UUID, `since`/`until` datetime,
  `offset` `min_value=0` `observations/serializers.py:35-41`) and every other
  error keep the decoders' CURRENT behavior. This slice adds NO nested-error
  lifting helper: the decoders read only the top-level `code`/`detail` exactly as
  today (`_error_text` `mcp_tools.py:400-407`; `error_from_body`
  `commands.py:1667-1675`), so a body with no top-level `code` (and not the
  recognized `kinds` shape) degrades to the existing generic `error: request
  failed` (MCP) / `http_error` (CLI) string, unchanged from before this slice. No
  `first_field_error` helper, no field allowlist, no cross-endpoint error-path
  change, and therefore no risk of surfacing input echoed by other endpoints
  (e.g. `link_type`'s `ChoiceField` message) — those keep degrading to the
  generic fallback exactly as today.

- **Capability denials**: unchanged; `search:query` / `memories:read`
  (`BuildContextBundle` requires `memories:read`, not `context:build`:
  `context/services.py:879`) / `observations:read` denials arrive as a top-level
  `code=missing_capability` (status 403) and render via the existing decoder path.
- **`project_not_found` (404)**: unchanged; mapped to `PROJECT_NOT_FOUND_MESSAGE`
  (`mcp_tools.py:402-403`) via the top-level `code`.
- **Empty results**: search → `No memory matched the search.`
  (`mcp_tools.py:163`); observations → `No observations found.`
  (`mcp_tools.py:282`) / CLI `No observations recorded for this project.`
  (`commands.py:2268`); context empty → `Engram returned no context for this
  session.` (`mcp_tools.py:213`). All unchanged.
- **Warnings present but zero items**: search renders the empty-result message
  **followed by the Warnings block** when `warnings` is non-empty, on BOTH
  client paths. The MCP `mcp_tools.py:161-163` early return and the CLI
  `run_search` `commands.py:2059-2062` early return are each restructured to
  compute `render_warnings(body.get('warnings'))` and append it to the `No
  memory matched the search.` message before returning (real case: `stale_match`
  with `items == []`, `search_api_tests.py:489`). The CLI must not `return 0`
  ahead of the warning render — that is the current dropped-warning bug and is
  guarded by a dedicated CLI test (Test Plan item 3). Context with empty
  `rendered_context` likewise appends the Warnings block to the no-context
  message when `warnings` is non-empty (real case: quarantine's
  `context_bundle_digest_visibility_unproven`, `context/services.py:155-171`);
  Citations remain omitted when there are no items.

## Test Plan

TDD: write each assertion as a failing test first, then implement. Client test
suites in `packages/cli/engram_cli/` use `unittest.TestCase` with a stub/fake
transport (`StubTransport` in `mcp_tools_tests.py:13-29`; `FakeTransport` in
`cli_lifecycle_tests.py`). Mirror that existing style rather than introducing
pytest functions into these files, to keep each suite internally consistent;
this is a deliberate, recorded deviation from the repo's default pytest-function
preference. These suites are pure-Python stdlib (no Django/DB), but the repo
rule stands: once Compose exists, Python tests run **inside the backend
container**, not on the host (`CLAUDE.md` Commands And Verification; the CLI
`packages/cli/README.md:71-80` gives the exact command). Run them with:

```bash
ENGRAM_ENV_FILE=.env.example \
docker compose -p engram-s1 -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro \
  -v /usr/lib/git-core:/usr/lib/git-core:ro \
  -e PYTHONPATH=/workspace/packages/cli --entrypoint python3 api \
  -m unittest discover -s packages/cli -p '*_tests.py' -v
```

The `ENGRAM_ENV_FILE=.env.example` prefix is MANDATORY, not cosmetic. The deploy
stack's shared `x-backend` anchor declares `env_file: ${ENGRAM_ENV_FILE:-.env}`
(`deploy/compose/docker-compose.yml:10-11`), resolved relative to the compose
file's directory. Its default target `deploy/compose/.env` is gitignored
(`.gitignore:5`) and absent in a fresh worktree, so a bare invocation aborts with
`env file … not found` BEFORE Python starts. The tracked
`deploy/compose/.env.example` is the only committed env file (`.gitignore:7`
`!.env.example`), and `--no-deps`+`--entrypoint python3` means the client suites
(pure stdlib) read none of its values — pointing `ENGRAM_ENV_FILE` at it only
satisfies Compose's file-existence check in every worktree.

(from the repo root; this is the CLIENT `unittest discover` command and matches
`packages/cli/README.md:71-80`). It uses the deploy stack
(`deploy/compose/docker-compose.yml`, whose own `name:` is `engram`) with
`--entrypoint python3` and `--no-deps` because the client suites are pure Python
stdlib and need only `python3`, not a database and not pytest. The `-p engram-s1`
project name is MANDATORY, not optional: CLAUDE.md (Worktree Development
Quickstart) requires a UNIQUE Compose project name per worktree because a shared
project name can corrupt the test database (the ROOT test stack's default project
name is `engram-test`; the deploy stack's is `engram` — neither is safe to share
across concurrent worktrees, so always pass `-p engram-s1` or the worktree's own
name).

This slice is client-side only, so there is NO backend pytest command: all tests
live in the client suites above. Record the exit code of the client
`unittest discover` command as slice evidence.

Test files (all already exist; extend them):

1. `packages/cli/engram_cli/mcp_server_tests.py` — schema presence:
   - `engram_search` inputSchema properties include `kinds` (array of string).
   - `engram_context` inputSchema properties include `kinds` and `token_budget`
     (integer); `required` still `['session_id']`.
   - `engram_observations` inputSchema properties include `observation_type`,
     `session_id`, `since`, `until`, `offset`; `required` still `[]`.
   - `engram_search`/`engram_context`/`engram_observations` descriptions: the
     existing test `test_tools_list_descriptions_direct_proactive_search`
     (`mcp_server_tests.py:144-160`) asserts **exact equality** against the
     complete old description strings. Appending the advertise sentence (search,
     context) and the ingestion-time sentence (observations) breaks those
     assertions, so this slice MUST update those exact expected strings to
     `<old description> + ' ' + '<appended sentence>'` for all three tools (not
     merely add a `assertIn`/containment check). Keep the exact-equality style; a
     containment check would silently pass on a wrong or truncated description.
   - `engram_observations` inputSchema: `since` and `until` `description`
     strings are asserted by **exact equality** against the mandated text in
     "MCP inputSchema additions" (not merely non-empty / `assertIn`). A
     presence-only check would pass on swapped `since`/`until` descriptions, a
     truncated description, or placeholder prose — yet those descriptions are the
     ONLY place an MCP caller learns the non-obvious created_at-inclusive /
     created_at-exclusive semantics (there is no `--help` for MCP callers), so
     the wording is load-bearing and must be pinned exactly. Assert
     `props['since']['description'] == '<mandated since text>'` and the same for
     `until`, so a swap (since text under `until`) or truncation fails.

2. `packages/cli/engram_cli/mcp_tools_tests.py` — passthrough + render via
   `StubTransport`:
   - `search_memory` sets `payload['kinds']` when `kinds` non-empty; omits key
     when absent/empty.
   - `fetch_context` sets `payload['kinds']` and `payload['token_budget']` when
     provided; omits when absent (`None`). Explicit boundary: `token_budget=0`
     IS forwarded (it is non-null; the server's `min_value=1`
     `context/serializers.py:70` must run and reject it) — assert
     `payload['token_budget'] == 0` is present, distinguishing "omit `None`"
     from "silently swallow invalid zero". Do not use falsy-coalescing
     (`value or default`) for `token_budget`; gate on `is not None`.
   - `list_observations` adds `observation_type`/`session_id`/`since`/`until`/
     `offset` to query params when provided; omits when absent (and omits
     `offset` when 0).
   - **malformed-shape fails closed** (guarding the present-but-wrong-shape rule
     in "MCP handler passthrough"; without these a loose truthiness or
     silent-drop gate would fail OPEN and widen the result set): assert each
     PRESENT wrong-shaped narrowing value RAISES `ValueError` from the handler
     rather than being dropped or forwarded — `search_memory` with
     `kinds='convention'` (a bare string, not a list) raises; `fetch_context`
     with `kinds='convention'` (a bare string, not a list) raises — this case is
     MANDATORY and independent of the `search_memory` `kinds` case because
     `fetch_context` (`mcp_tools.py:178`) is a SEPARATE handler from
     `search_memory` (`mcp_tools.py:135`); without it an implementation that
     silently drops malformed context `kinds` would run an all-kinds context
     bundle yet still pass every other mandated test (this is the failure the
     `fetch_context` fail-closed requirement in "MCP handler passthrough" guards);
     `fetch_context`
     with `token_budget=True` (a bool) and with `token_budget='5'` (a string)
     each raise; `list_observations` with a present non-string value for EACH of
     the four string filters — `observation_type=['tool_use']` (a list),
     `session_id=123` (an int), `since=123` (an int), and `until=['x']` (a list) —
     raises (the value is NOT coerced via `str(...)` into a misleading param and
     NOT silently dropped into an unfiltered query), and with `offset=True` (a
     bool) raises. All FOUR string-filter cases are MANDATORY and independent: the
     design gates each on its own `isinstance(value, str) and value` branch with no
     mandated shared helper, so an implementation that rejects malformed
     `observation_type` yet silently drops a wrong-shaped `session_id`, `since`, or
     `until` (running the broader unfiltered query the fail-closed rule prevents)
     must fail this test rather than pass on `observation_type` alone. Pair each
     with the ABSENT case (key missing, `None`, `''`, or `[]`)
     asserting the key is simply omitted — proving absent-omit and
     present-but-wrong-shape-raise are distinct, so a genuine no-filter request
     still works while a mis-typed narrowing request cannot silently broaden.
   - **`fetch_context` always mints a fresh request_id (replay-widening guard)**:
     call `fetch_context` twice with the SAME caller-supplied
     `request_id='fixed-1'` but DIFFERENT filters (first `{'session_id':'s',
     'kinds':['convention','gotcha']}`, then `{'session_id':'s',
     'kinds':['gotcha']}`) and assert BOTH outgoing payloads carry a distinct
     server-minted `request_id` that starts with `mcp-` and neither equals
     `fixed-1`. This proves the narrower second request cannot key onto the first
     bundle's broader `(org, project, request_id)` cache entry. Contrast with the
     write path: keep `test_explicit_request_id_wins` green
     (`update_memory_version` still honors `request_id='fixed-1'`), documenting
     that the fresh-mint rule is scoped to `fetch_context` only.
   - search render includes a `match:` line from `inclusion_reason`/
     `matched_terms` (rendered VERBATIM, no client redaction) and a `Warnings:`
     block from a `{code,message,memory_id}` warning; zero-warning body renders no
     block.
   - **empty-items + warning**: a body with `items == []` and a `stale_match`
     warning renders `No memory matched the search.` FOLLOWED BY the `Warnings:`
     block (regression guard for the dropped-warning bug).
   - context render appends a `Citations:` block mapping `[Mn]` →
     `memory_id/kind/confidence` from `items[]`. Cover THREE independent-omission
     cases (guarding against a coupled `if kind and confidence`): (a) both
     present → `[M1] memory_id=<id> kind=convention confidence=0.920`; (b) both
     absent (`confidence == None`, `kind == ''`) → `[M1] memory_id=<id>` (omits
     both); (c) exactly ONE present, e.g. `kind='gotcha'`, `confidence=None` →
     `[M1] memory_id=<id> kind=gotcha` (renders `kind=`, omits `confidence=`).
     Both fields are independently nullable/blank at the model
     (`core/models.py:726-730`) and serialized independently
     (`context/services.py:174-189`).
   - **empty context + warning**: empty `rendered_context` yields the no-context
     message and no Citations block, but WHEN `warnings` is non-empty the
     `Warnings:` block IS appended. Use the REAL quarantine warning
     `{'code': 'context_bundle_digest_visibility_unproven'}` (no `message`, no
     `memory_id`) and assert the rendered line is exactly
     `  [context_bundle_digest_visibility_unproven]` — a bare code with no
     trailing space and no literal `None` — proving `render_warnings` preserves a
     message-less safety code instead of garbling it.
   - **`kinds` validation error renders the FIXED message and echoes no server
     detail — for ALL THREE shapes** (this replaces all the removed
     `first_field_error` nested-shape / token-redaction / field-scope tests, and
     satisfies binding S1). Assert all three `kinds` rejection bodies render the
     FIXED client-side message `Invalid kind filter. Allowed kinds: decision,
     convention, gotcha, architecture, incident, digest.`:
     (a) **membership** — `{'kinds': {'code': ['search_kinds_invalid'], 'detail':
     ['Invalid kind(s): egk_abcdefghijklmnop.']}}` (search path; and the same with
     `context_kinds_invalid` for the context path). Assert the allowed-kinds names
     ARE present AND the server `detail` substring is NOT — specifically
     `'egk_abcdefghijklmnop' not in text` and `'Invalid kind(s)' not in text` —
     proving the client never surfaces server-echoed detail (a token-shaped value
     in the echoed detail cannot leak because it is never rendered);
     (b) **list-length** — `{'kinds': ['Ensure this field has no more than 6
     elements.']}` (a plain list) renders the SAME fixed message; assert the
     allowed-kinds names ARE present AND `'Ensure this field' not in text`;
     (c) **item-length** — `{'kinds': {'0': ['Ensure this field has no more than
     40 characters.']}}` (the DRF child-index dict) renders the SAME fixed message;
     assert the allowed-kinds names ARE present AND `'Ensure this field' not in
     text`.
     Cover BOTH client paths for each shape: MCP `_error_text` and CLI
     `error_from_body`/`emit_error`.
   - **non-`kinds` validation errors degrade to the generic message (unchanged)**:
     a 400 body shaped `{'token_budget': ['Ensure this value is greater than or
     equal to 1.']}` (no top-level `code`, not the recognized `kinds` shape)
     renders the existing generic fallback (`error: request failed` for MCP;
     the `http_error` generic path for CLI), proving this slice adds no
     nested-error lifting for other fields and no cross-endpoint error-path change.
   - observation render includes `observed_at` and `session_id`.

3. `packages/cli/engram_cli/cli_lifecycle_tests.py` — CLI flags via
   `FakeTransport` (this is where `run_search`/`run_observations` are tested,
   e.g. `test_search_posts_query_and_prints_matches:1929`,
   `test_observations_lists_recorded_observations:2584`):
   - `search --kind convention --kind decision` puts `kinds:
     ['convention','decision']` in the posted payload; no `--kind` omits it.
   - `search` text render shows the match line and Warnings block.
   - **CLI empty-items + warning** (regression guard for the dropped-warning bug
     at `commands.py:2059-2062`): a `FakeTransport` returning `items == []` plus
     a `stale_match` warning makes `run_search` write `No memory matched the
     search.` FOLLOWED BY the `Warnings:` block to stdout, and still returns 0.
     Asserting only the first line would let the current early `return 0` pass;
     assert the warning line is present.
   - `observations --session-id <uuid> --type user_prompt --since ... --until
     ... --offset 5` sends those as query params; render shows observed_at/
     session_id.
   - **no-filter omission** (finding: default values must not be sent): extend
     the existing no-filter observations test (`cli_lifecycle_tests.py:2584`) to
     assert the request params dict does NOT contain `observation_type`,
     `session_id`, `since`, `until`, or `offset` when the flags are unset — an
     empty-string filter and `offset=0` are omitted, not sent as `''`/`0`. Same
     for `search` with no `--kind`: assert `'kinds'` is absent from the payload.
   - **session-only metadata line** (finding: the metadata line is emitted when
     EITHER field is present, not only when both are): a `FakeTransport`
     observation row with `observed_at=None` and a non-empty `session_id` still
     renders the `session_id=...` line (guards against a coupled
     `if observed_at and session_id`). `session_id` is required
     (`core/models.py:518-523`) while `observed_at` is nullable
     (`core/models.py:547`), so session-only is a real response.
   - **help text**: the `--since` and `--until` flag help strings state BOTH that
     they filter on ingestion time (`created_at`) AND that `--since` is inclusive
     and `--until` exclusive. Assert against the parser's formatted help (or the
     `_SINCE_UNTIL_HELP` constant) that the substrings `created_at`, `--since is
     inclusive` (or `inclusive`), and `--until is exclusive` (or `exclusive`) are
     all present, so a truncated help string that drops the inclusive/exclusive
     semantics fails the test.

## Out of Scope

- **`correlation_id` observation filter.** The server DOES accept and apply it
  (`observations/serializers.py:37`, `observations/filters.py:13`
  `raw_event__correlation_id`, tested `observations_api_tests.py:550-595`), so
  full filter parity would include it. It is deferred here deliberately: it is a
  tracing-chain selector aimed at operators debugging a single correlated
  request, not the primary session/type/time filters an agent needs, and adding
  it costs a schema property + flag + param wiring with its own tests. Slice S6
  (DX fixes) owns it alongside `team_id`/`request_id` exposure. This slice's
  "filter parity" scope is explicitly the five session/type/time filters above.
- `team_id` / `request_id` schema exposure on MCP tools (slice S6).
- **Granting `observations:read` (and project scope) to wizard-issued keys.** The
  observations LIST endpoint requires `observations:read`
  (`apps/backend/engram/observations/views.py:35`) and, for an org-wide unbound
  key, project admin or agent scope (`apps/backend/engram/access/services.py:278-294`),
  but the default wizard key set `WIZARD_API_KEY_CAPABILITIES`
  (`packages/cli/engram_cli/commands.py:57-61`) grants only
  `memories:read`/`observations:write`/`search:query` and binds no project
  (`commands.py:432-435`). So a fresh `engram connect` key CANNOT exercise the
  observation filters/rendering this slice adds; end-to-end dogfooding of the
  observation path requires a SEPARATELY provisioned key carrying
  `observations:read` (plus `projects:agent` or a project-bound key). Changing
  wizard issuance is a provisioning/security decision owned by slice S6 (DX
  fixes), not this client-render slice; S1's observation tests use stub/fake
  transports and do not need a live key. This is a PRE-EXISTING gap (the
  `engram_observations` tool already could not be called by a wizard key before
  S1), surfaced here rather than introduced by S1.
- New MCP tools (slice S2).
- **All server-side and client-side redaction machinery.** Earlier review rounds
  had accumulated server-side redaction of `matched_terms`/`inclusion_reason`,
  redaction of the `kinds` validation echo in the two `validate_kinds` methods, an
  embedded-JSON strengthening of `redact_value` in `core/redaction.py`, a client
  `redact_token_shaped` helper, a `first_field_error` nested-error lifter, and a
  backend↔client `SECRET_STRING_RE` parity guard. ALL of it is out of scope per
  the teamlead decision of 2026-07-20 (Review Reconciliation round 12):
  `matched_terms`/`inclusion_reason` render verbatim under body-render parity (an
  accepted risk — they are substrings of the same server-provided document
  material the body render already exposes in full), and `kinds` validation errors
  render a fixed client-side message that echoes no server detail. So no redaction
  path exists on either the client or the backend for this slice, and there is no
  client residual to defer.
- **Backend changes of ANY kind.** This slice is client-side only: no serializer,
  service, or `core/redaction.py` edits; no new warning codes; no serializer
  FIELD/validation-rule/accepted-value or request/response-shape changes (slice S3
  owns backend warning additions).
- A CLI `context` subcommand (none exists; not added here).
- Client-side validation of `kinds` values, `token_budget` bounds, or datetime
  formats — server is authoritative.
- `--json` output format changes for `search` (already carries enrichment).

## Merge-Order Coordination

S1, S2, and S6 all edit `mcp_server.py` (`list_tools`) and `mcp_tools.py`
(`build_tools` / handlers). MOST S1 edits are additive: new schema properties,
new appended description sentences, new optional payload/param keys, new render
lines — whoever merges second re-applies these additively (append properties, do
not rewrite existing tool dicts). BUT four edits are NOT pure insertions; they
are control-flow REPLACEMENTS of existing early-return / fallback sites and MUST
be re-applied as edits to that specific control flow, not appended around it:
- MCP `search_memory` empty-items early return (`mcp_tools.py:161-163`) — now
  renders the empty message FOLLOWED BY the Warnings block (matching Error
  Handling and the regression test: the empty-result message comes first, then
  the `Warnings:` block);
- MCP `fetch_context` empty-render early return (`mcp_tools.py:211-213`) — now
  appends the Warnings block on empty context;
- CLI `run_search` empty-items early return (`commands.py:2059-2062`) — must
  render warnings before `return 0` (dropping-warning bug);
- the `_error_text`/`error_from_body` fallback branches
  (`mcp_tools.py:400-407`, `commands.py:1667-1675`) — a narrow branch is added
  BEFORE the generic fallback: when the body carries a top-level `kinds` field of
  ANY nested shape (membership `{code,detail}` dict, DRF child-index dict, or plain
  list), render the fixed allowed-kinds message reading nothing from the nested
  value; all other bodies keep the existing generic fallback unchanged (no
  `first_field_error` lifter).
A merger who treats these as additive would preserve the dropped-warning path. No
shared function signature changes except `build_search_payload` gaining a keyword
arg (default `[]`, backward compatible).

## Implementation Checklist

1. `mcp_server.py`: add schema properties (incl. mandatory `since`/`until`
   `description` strings on `engram_observations`) + description sentences on all
   three tools (search/context advertise, observations ingestion-time). Update
   the exact-equality description assertions in `mcp_server_tests.py:144-160` for
   all three tools in the same commit.
2. `mcp_tools.py`: passthrough in `search_memory` (kinds), `fetch_context`
   (kinds + `token_budget is not None`, AND fresh-mint request_id at `:194`
   ignoring any caller-supplied `request_id` — replay-widening guard),
   `list_observations`; render additions using shared helpers, including Warnings
   on empty search items and empty context render; add the `kinds`-error
   fixed-message branch to `_error_text` BEFORE its generic fallback (all other
   bodies keep the existing generic fallback).
3. `commands.py`: shared render helpers `search_match_line` (renders
   `inclusion_reason`/`matched_terms` verbatim — no client redaction — and omits
   ` | terms:` when `matched_terms` empty),
   `render_warnings` (code-only when message blank),
   `render_citations` (independent omission of `kind=`/`confidence=`),
   `observation_meta_line` (shared observed_at/session_id line, emitted when
   EITHER present); the `_ALLOWED_KINDS` tuple (mirror of `MEMORY_KINDS`) and the
   fixed `KINDS_ERROR_MESSAGE`; a narrow `kinds`-error detector that recognizes a
   body carrying a top-level `kinds` field of ANY nested shape (membership
   `{code,detail}` dict, DRF child-index dict, or plain list) and returns the fixed
   message (NO server detail read out of the nested value); wire it into
   `error_from_body` BEFORE the generic
   `http_error` fallback (all other bodies unchanged — no `first_field_error`
   lifter, no field allowlist); `build_search_payload` `kinds` kwarg; `run_search`
   match/warnings render INCLUDING the empty-items path (`commands.py:2059-2062`
   must render warnings before `return 0`); `run_observations` filters +
   `observation_meta_line` render (via the shared helper, NOT a local
   `commands.py:2271-2273` copy).
4. `main.py`: `--kind` on `search`; `--session-id/--type/--since/--until/
   --offset` on `observations`; the `_SINCE_UNTIL_HELP` constant on `--since`/
   `--until` stating BOTH `created_at` filtering AND `--since` inclusive /
   `--until` exclusive (help text is owned in `main.py`, not `commands.py`).
5. Tests (TDD, failing first): the three client test files above run inside the
   backend container per the Test Plan command. There are NO backend tests in this
   slice (client-side only).
6. **Bundle byte-sync**: run `python3 scripts/sync_plugin_bundle.py` (canonical
   `packages/cli/engram_cli/` → `packages/claude-plugin/hooks/engram_cli/` and
   `packages/codex-plugin/hooks/engram_cli/`); verify with
   `python3 scripts/sync_plugin_bundle.py --check` (exit 0 = in sync). Use the
   `python3` executable, NOT `python` — this environment ships only
   `/usr/bin/python3` and a bare `python` is not on PATH, so `python …` fails
   before checking either bundle. No new parity-guard logic is added to
   `sync_plugin_bundle.py` in this slice.

## Review Reconciliation

(append-only)

- round 1, finding 1, fixed — DRF field validators nest `{code,detail}` under
  the field name (`search/serializers.py:102-109`, proven by
  `context_api_tests.py:459`); Error Handling now specifies a shared
  `first_field_error` fallback so named codes surface via `_error_text` and
  `error_from_body` instead of degrading to generic, plus a regression test.
- round 1, finding 2, fixed — zero items + `stale_match` is a real backend
  result (`search_api_tests.py:489,517-524`); design decision 3 + Error Handling
  now render the Warnings block after the empty-search message; test added.
- round 1, finding 3, fixed — quarantine returns empty `rendered_context` +
  `context_bundle_digest_visibility_unproven` (`context/services.py:155-171`);
  decision 4 + Error Handling now append Warnings on empty context render; test
  added.
- round 1, finding 4, fixed — existing `test_tools_list_descriptions_...`
  asserts exact equality (`mcp_server_tests.py:144-160`); test plan + checklist
  now require updating those exact expected strings, not adding containment.
- round 1, finding 5, fixed — replaced "no docker compose required" with the
  mandated backend-container `unittest discover` command (`README.md:71-80`,
  CLAUDE.md rule).
- round 1, finding 6, fixed — context items default to `confidence=None`/
  `kind=''` (`core/models.py:726,730`, `context_api_tests.py:349-366`);
  render-examples now define independent omission of `kind=`/`confidence=`;
  test covers the unpopulated item.
- round 1, finding 7, fixed — capability corrected to `memories:read`
  (`context/services.py:879`), not `context:build`.
- round 1, finding 8, fixed — `token_budget` gated on `is not None` (not falsy);
  decision 2 + passthrough + test plan now forward and assert `token_budget=0`
  so server `min_value=1` rejects it.
- round 1, finding 9, fixed — MCP and CLI item headers differ
  (`mcp_tools.py:170,288` vs `commands.py:2065,2271`); render examples relabeled
  per-path, noting only the additive lines are shared.
- round 1, finding 10, fixed — `--since/--until` filter `created_at` (inclusive/
  exclusive) while display/order use `observed_at`
  (`core/api/filters.py:6-8`, `observations/services.py:95`); documented the
  distinction and boundary/delayed-ingestion consequence in the flag help text.
- round 2, finding 1, fixed — confirmed only `kinds` uses the custom
  `{code,detail}` dict shape; `token_budget`/`session_id`/`since`/`until`/`offset`
  raise ordinary DRF `{'<field>': ['<msg>']}` lists (`context/serializers.py:70`,
  `observations/serializers.py:35-41`) that the code-list-only helper would miss.
  Rewrote Error Handling so `first_field_error` handles BOTH shapes (dict→code[0]/
  detail[0]; list→field-name-as-code/first-string) and retracted the false
  "kinds/UUIDs/dates/budgets all surface" claim; added a plain-field-error test.
- round 2, finding 2, fixed — quarantine warning is code-only
  `{'code': 'context_bundle_digest_visibility_unproven'}` (`context/services.py:171`);
  defined `render_warnings` per-line rules (code always in `[...]`, message/
  memory_id each omitted when blank, no literal `None`) and tightened the empty-
  context test to assert the exact bare-code line.
- round 2, finding 3, fixed — MCP callers get no `--help`; added mandatory
  `since`/`until` schema `description` strings carrying the created_at inclusive/
  exclusive semantics plus an appended `engram_observations` description sentence,
  and extended the exact-equality/schema-presence tests to cover them.
- round 2, finding 4, fixed — `_error_text`/`error_from_body` are global decoders
  (`mcp_tools.py:159..400`, `commands.py:352..2265`); retracted "no other error
  paths change", added a Global-decoder note stating the fallback is additive and
  gated on absent top-level `code`, so all paths are safe and the shared helper is
  the single tested point.
- round 2, finding 5, fixed — CLI `run_search` early-returns on empty items
  (`commands.py:2059-2062`), dropping warnings with no guarding test; added the CLI
  empty-items site to decision 3 + Error Handling and a dedicated CLI regression
  test asserting the Warnings block follows the empty-search message.
- round 2, finding 6, fixed — removed the leftover unresolved "truthy or
  explicitly `0`?" wording; decision 2 and the passthrough now state one rule:
  `offset` omitted when `0` (valid server default), forwarded when non-zero,
  explicitly contrasted with `token_budget=0` which is forwarded to be rejected.
- round 3, finding 1 (BLOCKER), fixed — CONFIRMED search returns `matched_terms`/
  `inclusion_reason` RAW (`search/services.py:66,72`) while context redacts them
  via `_result_from_bundle` rebuild from persisted redacted items
  (`context/services.py:1003-1016,1270,1274`), proven by the context token-leak
  test (`context_api_tests.py:1109-1152`) with no search equivalent;
  `first_contains_match` (`context/services.py:1411-1421`) returns the whole
  document value so a short query surfaces an `egk_` token
  (`core/redaction.py:18-27`). Redesigned: added the "Search render safety"
  section requiring `SearchResult._item_response` to redact both fields (mirror
  context), scoped the ONE backend edit into the intro/Out-of-Scope, added
  checklist step 5 and Test Plan item 4 (search redaction regression, failing
  first). Not weakened — the render is made safe rather than dropped.
- round 3, finding 2, fixed — CONFIRMED `correlation_id` is accepted+applied
  (`observations/serializers.py:37`, `observations/filters.py:13`); corrected the
  false "only … five" evidence claim and added `correlation_id` to Out of Scope
  (deferred to S6 as a tracing-chain selector, with rationale).
- round 3, finding 3, fixed — CONFIRMED `kinds` is `ListField(child=CharField(
  max_length=40))` (`search/serializers.py:51-56`) so a 41-char/blank element
  yields the DRF child-index shape `{'kinds': {'0': [...]}}` (a third shape the
  two-shape helper missed); added rule 3 to `first_field_error` (walk the inner
  index dict, field-name-as-code) and a dedicated mcp_tools test.
- round 3, finding 4, fixed — CONFIRMED the API-key wizard passes
  `fallback='api_key_issue_failed'` (`commands.py:438`) with targeted remediation
  (`commands.py:72-92`) and its serializer emits ordinary `{'name': [...]}`
  errors (`console/serializers/api_keys.py:49-56`); a global `first_field_error`
  would flip it to code `name`+generic remediation. Redesigned the global-decoder
  note: `error_from_body` consults `first_field_error` ONLY when `fallback` is
  the generic `http_error`, preserving specific fallbacks; corrected the scope
  (claude-mem import uses its OWN decoder `import_claude_mem.py:276-287`, not
  affected); added a specific-fallback-preserved test.
- round 3, finding 5, fixed — CONFIRMED non-empty context embeds warnings in
  `rendered_context` via `_render_context` (`context/services.py:1281-1307`)
  while also returning structured `warnings`; a literal client Warnings block
  would double-print. Clarified decision 3 (client Warnings block scope = search
  both paths + context ONLY when `rendered_context` empty) and decision 4
  (non-empty context appends ONLY Citations).
- round 3, finding 6, fixed — CONFIRMED argparse flags live in `main.py:201-204`
  (not `commands.py`) and the mandated help string never stated `--since`
  inclusive. Replaced it with the `_SINCE_UNTIL_HELP` constant stating BOTH
  bounds, moved help-text ownership to `main.py` in the checklist, and added a
  help-text assertion to Test Plan item 3.
- round 3, finding 7, fixed — CONFIRMED MCP schemas are handwritten dicts
  (`mcp_server.py:149-162`) with no generated contract; strengthened Test Plan
  item 1 to assert the `since`/`until` `description` strings by EXACT equality
  (catching swaps/truncation), not presence.
- round 3, finding 8, fixed — CONFIRMED CLAUDE.md mandates a unique Compose
  project name; added `-p engram-s1` to the evidence command and a note that it
  is mandatory (default `engram-test` collides / corrupts the test DB).
- round 3, finding 9, fixed — added a no-filter omission assertion (params lack
  `observation_type`/`session_id`/`since`/`until`/`offset`; payload lacks
  `kinds`) to Test Plan item 3.
- round 3, finding 10, fixed — CONFIRMED `session` required
  (`core/models.py:518-523`), `observed_at` nullable (`core/models.py:547`),
  `None` preserved (`observations/services.py:41-62`); added a session-only
  (observed_at None) render test and the shared `observation_meta_line` helper
  emits when EITHER field is present.
- round 3, finding 11, fixed — CONFIRMED `kind`/`confidence` independent at model
  (`core/models.py:726-730`) and serializer (`context/services.py:174-189`);
  extended the citations test to the one-field-present case (`kind=` present,
  `confidence=` omitted).
- round 3, finding 12, fixed — CONFIRMED filter-only retrieval yields
  `inclusion_reason='filter-only authorized memory'` with empty `matched_terms`
  (`context/services.py:398-404`); defined `search_match_line` to omit the
  ` | terms:` segment when `matched_terms` is empty and to cover the case in tests.
- round 3, finding 13, fixed — named the shared `observation_meta_line` helper in
  decision 5 and checklist (single source, not two local renderers at
  `mcp_tools.py:284-293` / `commands.py:2271-2273`), preserving byte parity.
- round 3, finding 14, fixed — CONFIRMED `mcp_server.py:229-245` does not enforce
  `inputSchema`; specified `isinstance`-based passthrough gates (`kinds` list,
  `token_budget` int-not-bool) so malformed shapes are dropped client-side, with
  the server authoritative for anything that slips through.
- round 3, finding 15, fixed — corrected the "all additive" merge note to flag
  the four control-flow REPLACEMENTS (three empty-item early returns + the
  decoder fallback) that a literal additive re-apply would break.
- round 3, finding 16, fixed — CONFIRMED search calls `compute_retrieval_warnings`
  without `dropped_for_budget` (`search/services.py:160-171`; only fires >0 at
  `retrieval_warnings.py:180-197`); corrected the evidence so `budget_dropped` is
  not listed as a search-emitted code (the code-agnostic renderer is unchanged).
- round 3, finding 17, fixed — CONFIRMED `mcp_server.py:229-236` shallow-copies
  and sanitizes the internal repository-scope key; replaced "forwards `arguments`
  unchanged" with the precise public-fields-through / internal-key-sanitized
  description.
- round 3, finding 18, fixed — corrected `ContextResult.to_response` →
  `ContextBundleResult` (`context/services.py:122-128`) and clarified `_error_text`
  is DEFINED at `mcp_tools.py:400` but CALLED from `159,209,248,278,324,365`.
- round 4, finding 1 (BLOCKER), fixed — CONFIRMED the new `first_field_error`
  decoder introduces a token-leak: `validate_kinds` echoes rejected values
  verbatim (`Invalid kind(s): {…}.`, `search/serializers.py:106`), `kinds` is
  `ListField(child=CharField(max_length=40))` (`:51-56`) so an `egk_…`/`sk-…`
  token fits and matches `SECRET_STRING_RE` (`core/redaction.py:18-27`), the DRF
  handler preserves the detail unredacted (`drf_exception_handler.py:137-138`),
  and the CLI `redact_secret` only masks the configured key
  (`commands.py:1781-1785`) while MCP `_error_text` masks nothing. Redesigned (not
  weakened): required a self-contained `redact_token_shaped` helper (byte-identical
  `SECRET_STRING_RE` copy) applied to the `code`/`detail` inside `first_field_error`
  before surfacing, added a token-shaped redaction regression test on BOTH client
  paths (Test Plan item 2), and noted the raw-body residual is never rendered.
- round 4, finding 2, fixed — CONFIRMED the deploy `api` service targets the
  Dockerfile `runtime` stage (`poetry install --only main`, `Dockerfile:26`) which
  omits pytest (dev-only, `pyproject.toml:25`), so Test Plan item 4's backend
  pytest cannot run there; the ROOT compose service `app` targets the `test` stage
  (`docker-compose.yml:4-9`). Split the test-command paragraph: client
  `unittest discover` stays on the deploy `api`+`python3` command (matches
  `README.md:71-80`), backend redaction pytest runs via
  `docker compose -p engram-s1 run --rm app pytest -q …`; also corrected the
  default-project-name conflation (deploy stack `name: engram`, root test stack
  `name: engram-test`).
- round 4, finding 3, fixed — CONFIRMED the passthrough promise (concrete-type
  gating incl. boolean `offset`) was not encoded in the `list_observations` rule,
  which stringified any non-empty value and called `offset` merely a "non-zero
  integer" (so `observation_type=['tool_use']` and `offset=True` could slip
  through); MCP dispatch does no schema validation (`mcp_server.py:229-245`).
  Tightened the rule to `isinstance(value, str) and value` for the string filters
  and `isinstance(value, int) and not isinstance(value, bool) and value != 0` for
  `offset`, and added malformed-shape drop tests (string `kinds`, list
  `observation_type`, bool/str `token_budget`, bool `offset`) to Test Plan item 2.
- round 4, finding 4, fixed — CONFIRMED the `Engram call failed: HTTP 400 …`
  format exists only in MCP `_error_text` (`mcp_tools.py:400-407`); CLI
  `error_from_body` (`commands.py:1667-1671`) has no HTTP status and `emit_error`
  prints `<code>: <detail>` + remediation (`commands.py:1772-1774`). Clarified
  that `first_field_error` returns only the `(code, detail)` tuple and changes no
  signature; each decoder renders it in its OWN format and each path's test pins
  its own format string.
- round 4, finding 5, fixed — reworded the "All edited files live under
  `packages/cli/engram_cli/`" claim to "All CLIENT-side edited files …" and
  explicitly named the ONE backend edit (`apps/backend/engram/search/services.py`,
  exempt from byte-sync) so the prerequisite backend change is not dropped as
  scope creep.
- round 4, finding 6, fixed — changed the Error Handling summary from "TWO
  distinct nested shapes" to THREE and added the ListField child-index bullet, so
  the summary matches the three-shape `first_field_error` rules.
- round 4, finding 7, fixed — CONFIRMED this environment has no `python`, only
  `/usr/bin/python3` (`README.md:71`); changed the mandatory bundle-sync commands
  in checklist step 7 from `python` to `python3` with a note.
- round 5, finding 1 (BLOCKER), fixed — CONFIRMED the server still returns
  `matched_terms`/`inclusion_reason` raw (`search/services.py:66,72`) while the
  CLI (PyPI) and plugins (`publish-pypi.yml`, `plugin-repository`) and the backend
  image (`publish-images.yml`) are independently deployable, so a newer client can
  render an older server's unredacted response — backend-first ordering is
  unenforceable across operator-controlled upgrades. Redesigned (not weakened):
  kept the authoritative backend redaction AND added client-side defense-in-depth
  — `search_match_line` now passes `inclusion_reason`/each `matched_terms` term
  through the existing `redact_token_shaped` (idempotent with backend redaction),
  making the `match:` render safe against a version-skewed backend; updated intro,
  Search render safety, render-format, checklist steps 3/5, and added a version-
  skew client redaction test to Test Plan item 2.
- round 5, finding 2, fixed — CONFIRMED MCP dispatch does no schema validation
  (`mcp_server.py:229-245`) and the backend would reject the malformed values
  (`search/serializers.py:51`, `observations/serializers.py:38`), so silently
  dropping a present-but-mis-typed narrowing filter fails OPEN (all-kinds search /
  unfiltered observations = broader authorized data). Changed the passthrough rule
  from silent-drop to fail-closed: absent (missing/`None`/`''`/`[]`) is omitted,
  present-but-wrong-shape raises a local `ValueError` that `handle_request`'s
  try/except turns into a JSON-RPC error; updated the passthrough section and the
  Test Plan malformed-shape tests to assert raise + paired absent-omit.
- round 5, finding 3, fixed — CONFIRMED `SECRET_STRING_RE` has six alternatives
  (`core/redaction.py:18-27`) and `sync_plugin_bundle.py` only checks CLI↔plugin
  copies (not backend↔client), so an incomplete copy passes the single-`egk_`
  test while leaking five classes. Added an executable parity guard: a
  table-driven test covering a representative value for all six alternatives PLUS
  an exact `SECRET_STRING_RE.pattern` string pin against the backend literal, in
  Error Handling + checklist step 3 + Test Plan item 2.
- round 5, finding 4, fixed — CONFIRMED the `app` service uses
  `working_dir: /srv/app` and mounts `./apps/backend` there
  (`docker-compose.yml:10,16`), so `apps/backend/engram/search/search_api_tests.py`
  resolves to a nonexistent path; corrected the pytest argument to
  `engram/search/search_api_tests.py` with an explanatory note.
- round 5, finding 5, fixed — reconciled the file-scope claim: the intro now says
  ONE backend PRODUCTION-code edit (`search/services.py`) PLUS its backend test
  edit (`search_api_tests.py`, Test Plan item 4), both exempt from byte-sync, so
  the required regression test is not read as scope creep.
- round 5, finding 6, fixed — corrected the Merge-Order Coordination line for the
  MCP `search_memory` empty-items early return: it now says the empty message is
  rendered FOLLOWED BY the Warnings block, matching Error Handling (`:703`) and
  the regression tests (`:824`, `:892`) instead of the contradictory "before".
- round 5, finding 7, fixed — CONFIRMED the MCP dispatch symbol is
  `handle_request` (`mcp_server.py:205`, called from `run_server` `:281`), not
  `handle_message`; corrected the name in the passthrough section.
- round 6, finding 1 (BLOCKER), fixed — CONFIRMED the leak: `redact_text` →
  `redact_value` only fires JSON-key redaction when the WHOLE string parses as
  JSON (`core/redaction.py:62-74,85-98`), but `inclusion_reason` is prefixed
  `f'exact match: {term}'` (`context/services.py:366,376,386`), so
  `redact_text('exact match: {"password":"hunter2"}')` bails at
  `parse_json_string` (leading `exact match: `) and `hunter2` is not
  token-shaped — the reason leaks a JSON-key secret that the SAME term redacts in
  `matched_terms`, and the client regex-only `redact_token_shaped` cannot catch
  it either. The `egk_`-only regression test could not see this. Redesigned (not
  weakened): the backend edit now redacts the BARE terms first (JSON-key aware)
  and reconstructs the reason from the redacted term (`matched_terms[0]` == the
  term after the fixed prefix, `context/services.py:362-403`), closing BOTH
  secret classes authoritatively; added a JSON-key row to the backend regression
  test (Test Plan item 4). Corrected the client overclaim: `redact_token_shaped`
  covers the TOKEN-SHAPED class under version skew only; the JSON-key-object
  class under a pre-slice backend is closed by the backend edit and its remaining
  narrow client residual is recorded in Out of Scope (porting `redact_value`'s
  dict logic + `SENSITIVE_KEY_MARKERS` into the CLI deferred). Updated Search
  render safety, checklist step 5, and Out of Scope.
- round 6, finding 2, fixed — CONFIRMED an exact-string pin of the CLI
  `SECRET_STRING_RE.pattern` against a HARDCODED backend literal quoted into the
  client test proves only CLI-impl == test-snapshot; if the backend pattern later
  changes while both the CLI copy and the quoted literal stay old, it still passes
  (`sync_plugin_bundle.py` reads only `packages/cli` → plugin copies, never
  backend redaction). Moved the authoritative backend↔client parity guard into
  `scripts/sync_plugin_bundle.py --check`: it parses `SECRET_STRING_RE` from BOTH
  `core/redaction.py` and CLI `commands.py` (via `ast`) and fails CI on drift —
  a real read of the live backend source. Dropped the hardcoded-literal pin;
  kept the client-suite six-alternative table as the offline behavioral check.
  Updated the Error Handling parity paragraph, Test Plan item 2, added Test Plan
  item 5, checklist step 3, and added checklist step 8.
- round 7, finding 1 (BLOCKER), fixed — CONFIRMED the JSON-key gap: `kinds` is
  `ListField(child=CharField(max_length=40))` (`search/serializers.py:51-56`), so
  `{"password":"hunter2"}` (22 chars) fits, `validate_kinds` echoes it verbatim
  into `Invalid kind(s): …` (`search/serializers.py:106`,
  `context/serializers.py:164` — the ONLY two value-echoing validation details;
  all other serializer messages are fixed and interpolate no input), the DRF
  handler preserves it unredacted (`drf_exception_handler.py:137-138`), and the
  regex-only `redact_token_shaped`/`SECRET_STRING_RE` matches token FORMATS, not
  JSON keys (`core/redaction.py:18-27`) — so `first_field_error` would newly
  surface it. The round-6 "defer client JSON-key port" rationale did NOT transfer
  (that path has a backend safety net; the validation body has none). Redesigned
  (not weakened): closed it at the SOURCE by redacting the invalid values in both
  `validate_kinds` methods via `redact_text`/`redact_value` (JSON-key aware,
  `core/redaction.py:62-74,85-98`), which masks BOTH token- and JSON-key-shaped
  secrets and also cleans the raw body, while leaving plain invalid kinds (`foo`)
  unchanged. Corrected the false "only safe once this [client] redaction is in
  place" claim: the backend `validate_kinds` redaction is authoritative on any
  current server, and client `redact_token_shaped` is version-skew
  defense-in-depth (token class only, matching the search path); the JSON-key
  client residual under a STALE backend stays in Out of Scope. Updated the intro
  file-scope, Token-redaction section, Out of Scope, checklist step 5b, and added
  Test Plan item 4b.
- round 7, finding 2, fixed — CONFIRMED no CI lane auto-discovers
  `scripts/*_tests.py`: the backend pytest job runs with cwd `apps/backend`
  (`backend.yml:76`), the three `unittest discover` jobs cover only their package
  dirs (`backend.yml:104-110`), and the client evidence command uses
  `-s packages/cli` (`:895`); `scripts/*_tests.py` run only by explicit
  invocation (`compose-e2e.yml:30`). Corrected Test Plan item 5's false "Runs on
  the host / CI" claim: added a Collection-lane paragraph requiring an EXPLICIT
  `backend.yml` step (`python3 -m unittest scripts.sync_plugin_bundle_tests`) and
  noted the `--check` runtime guard already runs in `codex-plugin-e2e.yml`
  (`e2e_codex_plugin.py:585`) as a second, integration-level home; extended
  checklist step 8 to add that CI step.
- round 8, finding 1 (BLOCKER), fixed — CONFIRMED the global `first_field_error`
  wiring introduces a steady-state JSON-key leak OUTSIDE `kinds`:
  `MemoryLinkSerializer.link_type` is a DRF `ChoiceField`
  (`memory/serializers.py:79`) whose `"{input}" is not a valid choice.` echoes
  caller input verbatim, `create_memory_link` renders failures via `_error_text`
  (`mcp_tools.py:248`) with the MCP schema unenforced (`mcp_server.py:229-245`),
  the backend does NOT redact `link_type`, and regex-only `redact_token_shaped`
  misses `{"password":"hunter2"}` — so the claim "the ONLY validation detail …
  that echoes input is Invalid kind(s)" was false once the decoder is global.
  Redesigned (not weakened): bounded `first_field_error` to a
  `_FIRST_FIELD_ERROR_FIELDS` allowlist (the seven slice request fields), so
  input-echoing errors on non-slice endpoints (`link_type`, api-key `name`, …)
  degrade to the generic fallback exactly as today and are never lifted; among
  allowlisted fields only `kinds` echoes input and it is redacted at source. Added
  the "Why first_field_error is field-scoped" note, corrected the ONLY-detail
  claim, extended the JSON-key Out-of-Scope bullet with the third (link_type)
  surface, updated checklist step 3, and added a field-scope-bound test (Test Plan
  item 2) asserting `hunter2xyz` is not surfaced on both client paths. Not a
  version-skew/deploy issue.
- round 8, finding 2 (BLOCKER), fixed — CONFIRMED the internal contradiction: the
  intro (`:38`) and checklist step 5b mandate `validate_kinds` serializer edits in
  both search and context, while API/Schema (`:342`) said "No backend …
  serializers … change" and Out of Scope (`:1268`) said "no serializer changes".
  Reworded both to state precisely that no serializer FIELD/validation-rule/
  accepted-value or request/response SHAPE changes, and that the two backend edits
  are value-level redactions of error/response TEXT only (the `validate_kinds`
  edit changes only the emitted error string, not the schema), so the authoritative
  redaction fixes are not dropped by a literal reading and Test Plan item 4b is not
  omitted.
- round 8, finding 3, fixed — CONFIRMED `WIZARD_API_KEY_CAPABILITIES`
  (`commands.py:57-61`) grants only `memories:read`/`observations:write`/
  `search:query`, binds no project (`commands.py:432-435`), while the observations
  LIST endpoint requires `observations:read` (`observations/views.py:35`) plus
  project admin/agent scope for unbound keys (`access/services.py:278-294`), so a
  fresh `engram connect` key cannot exercise the observation path. Documented it
  explicitly as an Out-of-Scope provisioning prerequisite (pre-existing gap,
  wizard-cap change owned by S6, S1 tests use stub transports and need no live
  key), satisfying "explicitly restrict the feature to separately provisioned
  keys". Not superseded by the operator directive (capability provisioning, not
  deploy choreography).
- round 8, finding 4, fixed — CONFIRMED the deploy `api` service inherits
  `env_file: ${ENGRAM_ENV_FILE:-.env}` from the `x-backend` anchor
  (`deploy/compose/docker-compose.yml:10-11,40`); the default `deploy/compose/.env`
  is gitignored (`.gitignore:5`) and absent in a fresh worktree, while only
  `deploy/compose/.env.example` is tracked (`.gitignore:7`), so the client
  `unittest discover` command aborted with `env file … not found` before Python
  started. Prefixed the command with `ENGRAM_ENV_FILE=.env.example` (the tracked
  file, resolved relative to the compose dir) and documented why; `--no-deps`+
  `--entrypoint python3` means the stdlib suites read none of its values.
- round 8, finding 5, fixed — CONFIRMED the backend evidence command ran only
  `engram/search/search_api_tests.py` while Test Plan item 4b's `validate_kinds`
  redaction regression lives in BOTH the search and context suites (`:1184`).
  Added `engram/context/context_api_tests.py` to the pytest command and explained
  that both files are required so the context-side regression is actually
  executed as checkpoint evidence.
- round 8, finding 6, fixed — CONFIRMED the `backend.yml` unit-test job installs
  pytest only inside the `apps/backend` Poetry env and invokes it via `poetry run
  pytest` (`backend.yml:44-76`), so the permitted alternative `python3 -m pytest
  scripts/sync_plugin_bundle_tests.py` on the runner's system interpreter has no
  pytest module. Dropped the raw-pytest alternative in Test Plan item 5 and
  mandated the stdlib `python3 -m unittest scripts.sync_plugin_bundle_tests` form
  (needs no dependency), which is the only portable runner for a `scripts/`-rooted
  test with no Poetry env.
- round 9, finding 1, fixed — CONFIRMED the replay-widening hazard:
  `_new_request_id` honors a caller-supplied `arguments['request_id']`
  (`mcp_tools.py:394-397`) and `BuildContextBundle` returns any existing bundle
  keyed on `(organization, project, request_id)` WITHOUT re-applying `kinds`/
  `token_budget` (`context/services.py:901`, `core/models.py:980-986`), so
  replaying an id with narrower filters would return the earlier broader cached
  bundle — a silent fail-open widening on the very filters this slice adds.
  Mandated that the `fetch_context` handler mint a FRESH request_id every call
  and NOT honor a caller value (schema does not expose `request_id`; deferred to
  S6, which must add replay fingerprinting first). Scoped strictly to
  `fetch_context`; the three write handlers keep `_new_request_id(arguments)`
  (idempotency key, no narrowing filter) — `test_explicit_request_id_wins` stays
  green. Added a same-id/different-`kinds` Test Plan assertion and a checklist
  note. Steady-state idempotency, not a deploy-window concern, so not waived by
  the operator directive.
- round 9, finding 2, fixed — the Test Plan mandated container execution for all
  local Python tests (`:1004-1006`) yet the `scripts.sync_plugin_bundle_tests`
  wiring told the operator to run the bare host form
  `PYTHONPATH=. python3 -m unittest …` locally, contradicting `CLAUDE.md:156-157`.
  Resolved by splitting the two contexts explicitly: the bare form is the
  CI-runner-only lane (a documented exception — `backend.yml` already runs every
  unit-test step directly on the GitHub runner via Poetry, no Compose, so one
  more runner-native `unittest` step is consistent), while LOCAL evidence MUST be
  gathered inside the backend container via the same `docker compose … run
  --entrypoint python3` pattern as the client suites (command now given in Test
  Plan item 5 and cross-referenced from checklist step 8).
- round 10, finding 1, fixed — CONFIRMED steady-state leak: `redact_value`'s
  JSON-key masking only fires when the WHOLE string parses as JSON
  (`parse_json_string` bails unless the first non-space char is `{`/`[`,
  `core/redaction.py:87`), so the prescribed `redact_text(item)` (kinds echo) and
  `redact_text(term)` (matched terms) were no-ops on a PREFIXED value like
  `prefix {"password":"hunter2xyz"}` — 32 chars, within the 40-char `kinds`
  `CharField`, and the full document exact-term `first_contains_match` returns
  whole. The spec's "closes BOTH classes" claim was therefore false for prefixed
  JSON. Redesigned the closure at the root: added backend edit (3) strengthening
  `redact_value` to redact JSON objects/arrays EMBEDDED in a larger string (new
  "Embedded-JSON redaction" section; bounded brace/bracket substring scan +
  `json.loads` + recurse + substitute; keeps the pure-JSON and `SECRET_STRING_RE`
  passes; no over-redaction of brace-free prose). Both edits (1) and (2) now
  depend on (3); corrected the overclaims in "Search render safety" and "Token
  redaction of the surfaced strings", the intro backend-edit list, the Out of
  Scope required-edits list (now three), and the checklist (new foundation step
  4d). Added PREFIXED-value regression rows to Test Plan items 4 and 4b and a new
  core unit test item 4c. Not a rolling-deploy/back-compat concern, so not waived
  by the operator directive.
- round 11, finding 1 (BLOCKER), fixed — CONFIRMED the round-10 non-nested
  `\{[^{}]*\}` scan is insufficient: `prefix {"password":{"v":"hunter2xyz"}}` (38
  chars) fits the `kinds` `CharField(max_length=40)` (`search/serializers.py:51-56`,
  `context/serializers.py:63-68`), and the non-nested match extracts only the inner
  `{"v":"hunter2xyz"}` — `v` is not a sensitive key, `redact_value`'s recursion
  (`core/redaction.py:36-50,79-82`) is a no-op, the outer `password` wrapper is
  never parsed, `hunter2xyz` is not token-shaped, and both `validate_kinds` echoes
  (`search/serializers.py:102-107`, `context/serializers.py:160-165`) plus
  `first_field_error` surface the secret. Redesigned "Embedded-JSON redaction" and
  Test Plan item 4d to mandate a nesting- AND string-aware
  `json.JSONDecoder().raw_decode` fragment scanner (walk chars, decode at each
  `{`/`[`, resume past the consumed span; non-sensitive fragments and
  `JSONDecodeError` positions emitted unchanged), explicitly forbidding the regex.
  Pinned an outer-sensitive/composite-value regression row
  (`prefix {"password":{"v":"hunter2xyz"}}`) plus a brace-inside-string row in Test
  Plan item 4c. Not weakened — the redactor is made correct for nested values, not
  scoped down. Not a rolling-deploy/back-compat concern, so not waived by the
  operator directive.
- round 11, finding 2 (minor), fixed — CONFIRMED `apps/backend/engram/core/
  redaction_tests.py` does not exist and Test Plan item 4c requires creating it,
  but the verification command named only `search_api_tests.py` and
  `context_api_tests.py` explicitly, so the new embedded-JSON regression (including
  the nested-object row that pins the scanner) would never be collected and the
  checkpoint could pass green with the scanner unexecuted. Added
  `engram/core/redaction_tests.py` to the pytest invocation and rewrote the
  surrounding "ALL THREE backend test files are required" note.
- round 12, all findings, superseded-by-teamlead-decision — the teamlead decision
  of 2026-07-20 replaces the accumulated echo-redaction design in its entirety.
  The fixes replaced (not re-litigated — they are not material under the decision)
  are the whole kinds-echo and term-redaction chain accumulated across earlier
  rounds: round 1 finding 1 (`first_field_error` nested-error lifter), round 2
  finding 1 (two-shape `first_field_error`), round 3 finding 1 (BLOCKER — search
  `SearchResult._item_response` redaction of `matched_terms`/`inclusion_reason`),
  round 3 finding 3 (ListField child-index third shape), round 3 finding 4 (global
  decoder specific-fallback gate), round 4 finding 1 (BLOCKER — client
  `redact_token_shaped` on surfaced error detail), round 4 finding 6 (three-shape
  summary), round 5 finding 1 (BLOCKER — client-side match-line redaction for
  version skew), round 5 finding 3 (six-alternative `SECRET_STRING_RE` behavioral
  table + parity pin), round 6 finding 1 (BLOCKER — bare-term reconstruction for
  the JSON-key class), round 6 finding 2 (`sync_plugin_bundle.py --check`
  backend↔client parity guard), round 7 finding 1 (BLOCKER — `validate_kinds`
  source redaction in both serializers), round 7 finding 2 (`scripts/*_tests.py`
  CI lane), round 8 finding 1 (BLOCKER — `_FIRST_FIELD_ERROR_FIELDS` field-scope
  bound / `link_type` leak), round 8 finding 2 (serializer-edit scope wording),
  round 10 finding 1 (embedded-JSON `redact_value` strengthening), and round 11
  findings 1-2 (nesting-aware `raw_decode` scanner + its `redaction_tests.py`
  lane). PER THE DECISION: (a) the client renders a FIXED message listing the
  allowed kinds from a client-side constant on a `kinds` validation error and
  never renders server-echoed detail — so no echo path and no client-side
  redaction parser exist, and all nested-JSON echo-redaction machinery is removed
  from the spec (`first_field_error`, `redact_token_shaped`, the three backend
  redaction edits in `search/services.py`, the two `validate_kinds` methods, and
  `core/redaction.py`, plus the `SECRET_STRING_RE` parity guard and Test Plan
  items 4/4b/4c/5); and (b) `matched_terms`/`inclusion_reason` render with exactly
  the same treatment as the memory body render (server-provided content rendered
  verbatim) — they are substrings of the same document material the body render
  already exposes in full, so extra client-side redaction is rejected as security
  theater and this is a documented accepted risk anchored to body-render parity.
  The slice is now client-side only (schema/filter passthrough + render
  enrichment). Retained from earlier rounds because they are NOT redaction
  machinery: the empty-items/empty-context Warnings rendering (round 1 findings
  2-3, round 2 finding 5), the fail-closed present-but-wrong-shape passthrough
  guard (round 5 finding 2), and the `fetch_context` fresh-request_id
  replay-widening guard (round 9 finding 1). Cites the teamlead decision of
  2026-07-20.
- round 13, finding 1 (major), fixed — CONFIRMED the `kinds` `ListField(child=
  CharField(max_length=40), max_length=6)` (`search/serializers.py:51-56`,
  `context/serializers.py:63-68`) rejects two ways BEFORE `validate_kinds`: >6 items
  yields the plain list `{'kinds': ['Ensure this field has no more than 6
  elements.']}` (tested `search_api_tests.py:628`, `context_api_tests.py:2266`) and
  a child >40/blank yields the child-index dict `{'kinds': {'0': [...]}}` — neither
  carries the `search_kinds_invalid`/`context_kinds_invalid` code, so the narrow
  detector misses them, contradicting the intro's blanket "on a kinds validation
  error." Narrowed the promise: the FIXED message covers ONLY the membership error
  (the sole `kinds` detail that echoes caller input); the other two `kinds` field
  errors degrade to the existing generic fallback, documented as no-echo-safe
  because those DRF messages are fixed templates. Updated the intro bullet and added
  a "Scope of the fixed message" paragraph to Error Handling. Preserves the teamlead
  no-echo/no-parser invariant — not a re-litigation of decision (a).
- round 13, finding 2 (minor), fixed — CONFIRMED the two decoders keep DIFFERENT
  envelopes (MCP `_error_text` returns `Engram call failed: HTTP {status} {code}:
  {detail}` `mcp_tools.py:400-407`; CLI `emit_error` prints `<code>: <detail>` +
  `remediation:` `commands.py:1667-1675,1772-1774`), so the "render byte-identical
  text" wording was false for full output. Scoped byte-identity to the shared
  `KINDS_ERROR_MESSAGE` constant only (each path keeps its own envelope: MCP returns
  the bare message like `PROJECT_NOT_FOUND_MESSAGE`, CLI wraps it), and stated the
  render tests assert the shared substring present / server detail absent on both
  paths, NOT cross-path full-output equality. Reworded the Error Handling paragraph.
- round 14, finding 1 (major), refuted:superseded-by-teamlead-decision — re-raises
  the round-13-finding-1 point (already reconciled): the reviewer reads teamlead
  decision (a)'s "on a kinds validation error the client renders the fixed
  allowed-kinds message" as mandating the fixed message on EVERY `kinds` field error
  (>6 items via `ListField(max_length=6)`, child >40 via `CharField(max_length=40)`,
  both confirmed to reject BEFORE `validate_kinds` at `search/serializers.py:51-56` /
  `context/serializers.py:63-68`). Decision (a)'s binding purpose is echo
  elimination ("so no echo path exists and no client-side redaction parser is
  needed"), and the spec fully satisfies it: only `validate_kinds` echoes caller
  input, and that is exactly the membership error the fixed message replaces; the
  other two DRF errors interpolate no caller input, so degrading them to the generic
  fallback leaks nothing. Broadening the fixed allowed-kinds message to a "too many
  items" or "child too long" error would MISREPORT the failure (the real problem is
  count/length, not an invalid kind), so the spec's membership-only scoping is
  correct, not a gap. Findings re-litigating decision (a) are declared not material.
- round 14, finding 2 (major), fixed — CONFIRMED the mandatory malformed-shape tests
  (Test Plan item 2) exercised malformed `kinds` only through `search_memory` and
  `fetch_context` only through `token_budget`, while the passthrough requirement
  (line 320 / "MCP handler passthrough") demands `fetch_context` fail closed on a
  present non-list `kinds`; `fetch_context` (`mcp_tools.py:178`) and `search_memory`
  (`mcp_tools.py:135`) are independent handlers, so an implementation that silently
  drops malformed context `kinds` (running an all-kinds bundle) could pass every
  mandated test. Added a MANDATORY `fetch_context` with `kinds='convention'` (bare
  string) raises case to the malformed-shape test list, explicitly noting its
  independence from the `search_memory` case.
- round 14, finding 3 (minor), fixed — CONFIRMED a third real render state the spec
  left ambiguous: `items == []` WITH a non-empty `rendered_context`, produced by the
  session-start empty bundle (`_render_context` non-`user_prompt_submit` branch,
  `context/services.py:1287-1291`, returns
  `'# Engram context\n\nNo approved memory matched this request.'`), proven by
  `context_api_tests.py:1801-1803` (`items == []`, non-empty `rendered_context`).
  Decision 4 said "no Citations block when there are no items" only for the
  empty-`rendered_context` path, leaving this state open to a bare `Citations:`
  header. Amended decision 4 to gate the Citations block on `items[]` being
  non-empty (NOT on `rendered_context`): `render_citations([])` returns an empty
  string and appends nothing, so this state renders the bundle text verbatim with no
  Citations block.
- round 15, finding 1 (major), fixed — CONFIRMED the spec violated binding S1: it
  scoped the fixed allowed-kinds message to the membership error only and degraded
  list-length (`ListField max_length=6`) and item-length (`CharField max_length=40`)
  `kinds` failures to the generic fallback, contradicting S1's "kinds validation
  errors of ANY shape (membership, item-length, list-length) render a fixed
  client-side message". All three failures surface the field name `kinds` as the
  400 body's top-level key (field-level DRF nesting, proven for the sibling `query`
  field by `context_api_tests.py:459`), so redesigned the detector to key on
  top-level `kinds` FIELD PRESENCE (any nested shape: `{code,detail}` dict, DRF
  child-index dict, or plain list) and render the single fixed message with no
  server detail read out of the nested value; CLI envelope uses a fixed client-side
  sentinel code since list/item-length bodies carry no server `code`. Expanded Test
  Plan item 2 to assert the fixed message and absence of server-echoed substrings
  for all three shapes on both MCP and CLI paths.
- round 16, finding 1 (major), fixed — CONFIRMED three sections still carried the
  round-13/14 membership-only, code-based detector wording after round 15 rewrote
  Error Handling (`:596-657`) and Test Plan item 2 to key on top-level `kinds`
  field PRESENCE for all shapes: the intro bullet (`:29`, "membership-validation
  errors" + "The other `kinds` field errors … degrade to the existing generic
  rendering"), Merge-Order Coordination (`:987`, "recognized `kinds` validation
  shape (`search_kinds_invalid`/`context_kinds_invalid`)"), and checklist step 3
  (`:1016`, "nested `code` of `search_kinds_invalid`/`context_kinds_invalid`").
  VERIFIED `kinds` is `ListField(child=CharField(max_length=40), max_length=6)`
  (`search/serializers.py:51-56`), so list-length (>6) and item-length (>40/blank)
  reject at the DRF field BEFORE `validate_kinds` (`search/serializers.py:102-109`)
  and carry no membership code — the stale detector would miss them, violating S1
  and failing the mandatory three-shape tests. Rewrote all three sections to the
  any-shape / field-PRESENCE detector (reading nothing from the nested value),
  matching the round-15 Error Handling and binding S1. Reconciliation rounds 13/14
  (append-only history) left intact.
- round 16, finding 2 (major), fixed — CONFIRMED the fail-closed observation-filter
  guarantee (passthrough `:366-374`: `observation_type`/`session_id`/`since`/`until`
  each raise on a present non-string) was under-pinned by Test Plan item 2, whose
  malformed-shape matrix mandated only `observation_type=['tool_use']` and
  `offset=True`. MCP dispatch does no schema validation (`mcp_server.py:229`) and
  the design mandates no shared helper across the four string branches, so an
  implementation could reject malformed `observation_type` yet silently drop a
  wrong-shaped `session_id`/`since`/`until`, run the broader unfiltered query the
  rule forbids, and still pass every mandated test — the same independent-branch gap
  round 14 finding 2 fixed for `fetch_context` `kinds`. Extended the malformed-shape
  matrix to require a present non-string case for EACH of the four string filters
  (`session_id=123`, `since=123`, `until=['x']` added), marked all four MANDATORY
  and independent. Retained fail-closed safety requirement, not a deploy/back-compat
  concern.
