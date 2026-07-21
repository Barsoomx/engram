# S6 dx-fixes — MCP DX / hygiene batch (client-only)

Slice S6. Three small, additive, client-only changes to the `engram_cli`
package (and its two byte-identical plugin bundles). No backend change.

Merge-order note: S6 touches the same client files as S1 and S2
(`mcp_server.py`, `mcp_tools.py`). Apply S6 **after** S1/S2 land; every S6 edit
is additive (new optional schema keys, one relaxed guard, override plumbing) and
should rebase cleanly on top of them. Re-run the bundle byte-sync step last.

**Cross-slice tool-count reconciliation (S2).** S2 adds two MCP tools —
`engram_memory_get` and `engram_audit` — so once both slices land `list_tools`
returns **eight** tools, and S2 freezes both new schemas with exact-schema tests
that assert an 8-tool list and each new tool's precise `inputSchema`
(`2026-07-19-mcp-read-tools-design.md:941-943`). S6 deliberately owns and edits
only the **six original** tools listed in *Tool inputSchemas* below; it does
**not** modify S2's two schemas or S2's exact-schema tests. Two consequences make
this unambiguous rather than a "six-or-eight" choice:

- **`team_id` reaches all eight functionally, but is schema-advertised only on
  the six.** The per-call override is implemented at the shared
  `_require_runtime_for_arguments` → `resolve_runtime` layer (Design §3), so it
  functionally reaches every tool that resolves runtime — including S2's
  `engram_memory_get` and `engram_audit`, both of which already forward
  `runtime.team_id` (`2026-07-19-mcp-read-tools-design.md:116,506-512,625`). S6
  does not add `team_id` to their schemas; advertising it there is S2's to own
  (see Out of Scope).
- **`request_id` is intentionally not exposed on the two read-only GET tools.**
  Their S2 handlers do not call `_new_request_id` and their GET contracts do not
  consume `request_id`, so adding it to their schemas would advertise an inert
  field. S6 wires `request_id` only into the six tools it owns.

Wherever this spec says "all six" or "every handler", it means exactly the six
original tools named in *Tool inputSchemas* below — never eight.

## Problem and Evidence

All refs verified against the working tree on 2026-07-19.

### 1. `engram mcp-install` refuses a repo-url-routed setup

`run_mcp_install` hard-requires `project_id` in the local config:

- `packages/cli/engram_cli/commands.py:1810-1817` — reads `server_url` and
  `project_id`, then `if not server_url or not project_id: raise CliError('missing_config', ...)`.

But the whole data plane already routes by `repository_url` when `project_id` is
absent:

- `apps/backend/engram/core/repository.py:86-122` — `resolve_project_for_scope`:
  `project_id is None` branch canonicalizes `repository_url` and matches/creates
  a project (`_unbound_agent_capability`, line 125-126).
- `packages/cli/engram_cli/mcp_tools.py:58-63` — `resolve_runtime` selects
  `repository_url = workspace_repository_url()` exactly when `project_id` is
  empty; line 67-68 returns `None` only when *both* project and repo are absent.
- `packages/cli/engram_cli/mcp_tools.py:382-391` — `_scope_payload` emits
  `project_id` **or** `repository_url`, never requiring the former.

So `mcp serve` runs fine project-less, but `mcp-install` (which only writes the
`mcpServers` entry, `build_engram_mcp_entry` at `commands.py:1857-1868`, a
payload that does **not** reference `project_id`) blocks the operator before
they can get there.

Evidence correction vs. brief: the brief cited `config.py:43-53` as part of the
gate. Those lines are `default_claude_code_config_path` /
`default_claude_desktop_config_path` — filesystem path helpers that gate
nothing. The **only** `project_id` gate for install is
`commands.py:1811-1817`. `config.py` is **not** modified by this slice.

### 2. `request_id` is consumed but hidden from every inputSchema

Four handlers already read `request_id` off `arguments` via `_new_request_id`
(`mcp_tools.py:394-397`):

- `fetch_context` — `mcp_tools.py:194`
- `create_memory_link` — `mcp_tools.py:237`
- `update_memory_version` — `mcp_tools.py:313`
- `submit_memory_feedback` — `mcp_tools.py:354`

None of the six `inputSchema`s in `list_tools` (`mcp_server.py:79-202`) declare
`request_id`, so a calling agent has no documented way to supply a stable id for
idempotent retries.

Evidence correction vs. brief: the brief said "5 handlers". Only **4** forward
`request_id` today. `search_memory` (`mcp_tools.py:135-175`) and
`list_observations` (`mcp_tools.py:256-293`) do **not**. The backend accepts
`request_id` on those endpoints too — `SearchRequestSerializer.request_id`
(`apps/backend/engram/search/serializers.py:58`, echoed at
`search/views.py:22,41`) and `ObservationListQuerySerializer.request_id`
(`apps/backend/engram/observations/serializers.py:36`, echoed at
`observations/views.py:32,65`) — so this slice adds `request_id` to **all six**
schemas and wires the two missing handlers to forward it, keeping every schema
honest.

### 3. `team_id` cannot be set per call

`team_id` is resolved once, from env-or-config only:

- `packages/cli/engram_cli/mcp_tools.py:56` — `team_id = os.environ.get('ENGRAM_TEAM_ID') or as_string(config.get('team_id'))`.
- It flows into `McpRuntime.team_id` (`mcp_tools.py:33,74`) and out via
  `_scope_payload` (`mcp_tools.py:388-389`) and `list_observations` params
  (`mcp_tools.py:268-269`).

No tool `inputSchema` exposes `team_id`, so a caller that is *authorized* to
scope a single call to a specific team cannot express that intent. The backend
accepts `team_id` on all six endpoints (search `serializers.py:45`, observations
`serializers.py:34`, and the memory/context views via `_scope_payload`).
`project_id` already has a per-call override (`project_override`,
`mcp_tools.py:51-55,108-110`); `team_id` has no parallel. This slice adds the
plumbing so an authorized key can select a team per call; it does **not** widen
authorization.

**Authorization precondition (decisive, do not overpromise).** The per-call
override is only *usable* by keys the backend already permits to select a team —
it is transport, not a grant. `_team_ids` (`access/services.py:344-374`) gates
`team_id` as follows:

- A **team-bound key** (`key.team_id` set) rejects any different `team_id`
  (`team_scope_denied`) and accepts only its own bound team — so an explicit
  override is at best a no-op there.
- An **unbound key** may select a team only with effective `teams:*` or
  `policy:admin`, an existing team, and a `ProjectTeam` linkage between the
  requested team and one of the `project_ids` resolved **at authorization time**
  (`_team_ids`, `access/services.py:357-372`); otherwise it returns `None` →
  `team_scope_denied`.

  **Linkage granularity caveat (do not overpromise).** For a repo-url-routed
  call the request carries **no** `project_id`, so `_project_ids`
  (`access/services.py:296-299`) resolves `project_ids` to *every* project in the
  organization. The `ProjectTeam` linkage check therefore passes when the team is
  linked to **any** org project — not specifically the project the
  `repository_url` resolves to. Repository resolution happens **after**
  authorization, and the persisted session's model validation checks only
  organization equality for `team` (`check_organization_scope`,
  `core/models.py:401-406`), not team↔project linkage. So neither this transport
  nor the backend authorization guarantees the selected team is linked to the
  repo-resolved project; an admin/operator key can pair a team linked to project
  A with a request that routes to project B. This is a **pre-existing** backend
  property — `team_id` already travels from env/config on repo-routed calls today
  (`mcp_tools.py:56`, `_scope_payload`) — and the per-call override adds no new
  reach beyond what config/env already expresses. Closing the cross-project
  linkage gap requires a backend recheck against the resolved project and is
  tracked in Out of Scope, not addressed by this client-only slice.
- The ordinary agent capability set is
  `memories:read, memories:review, observations:write, observations:read,
  search:query, projects:agent`
  (`engram_bootstrap_golden_path.py:26-33`) — it contains **no** team-admin
  capability, so an ordinary agent that supplies a non-bound `team_id` receives
  `team_scope_denied`. The feature is therefore meaningful for admin / operator
  keys, not for the default agent key. The client stays permissive (no
  capability check); the backend remains the authorization authority, and the
  advisory is surfaced verbatim by `_error_text`.

Also note `team_id` (like `project_id`) is a `UUIDField` on every serializer
(`search/serializers.py:45`, observations `serializers.py:34`); a non-UUID string
is rejected by the serializer with HTTP 400 **before** authorization runs. All
worked examples and tests below use real UUIDs for that reason.

## Design

Three additive changes; smallest surface that makes the observed behavior
possible and honest.

1. **Relax the install gate.** In `run_mcp_install`, require only `server_url`
   (`api_key` is already validated at `commands.py:1804-1809`). When
   `project_id` is empty, still install, and emit one advisory warning line to
   `stderr` telling the operator that memory will route by the git remote of the
   working directory. The generated `mcpServers` entry is unchanged.
   - **Advisory scope (precedence-honest).** The advisory is emitted at install
     time from the **config file only**, but serve-time routing precedence is
     `project_override (per-call) > ENGRAM_PROJECT_ID (env) > config.project_id`
     (`mcp_tools.py:51-55,58`), and any effective project suppresses repo routing
     (`repository_url = ''` at `mcp_tools.py:58-59`). So a serve-time
     `ENGRAM_PROJECT_ID` (or per-call `project_id`) makes the "route by the git
     remote" claim false. The advisory text is therefore worded conditionally —
     it names the git-remote default **and** the serve-time project escape
     hatch, naming both forms (`ENGRAM_PROJECT_ID` **and** a per-call
     `project_id` argument, since the latter also wins even when the env var is
     unset — `mcp_tools.py:51-55`) — rather than asserting an unconditional
     outcome install time cannot verify.
   - Rejected: teach `mcp-install` to derive/persist a `repository_url` at
     install time — one line, wrong layer; routing is a per-serve, per-cwd
     decision that `resolve_runtime` already makes.
   - Rejected: keep requiring `project_id` and document a manual config edit —
     pushes friction onto every repo-url operator for no safety gain.

2. **Expose `request_id` on all six schemas + wire the two gaps.** Add
   `request_id` (string, optional, not required) to every `inputSchema` in
   `list_tools`. Add `request_id` forwarding to `search_memory` (into the POST
   payload) and `list_observations` (into the GET params) using the existing
   `_new_request_id` helper, matching the four handlers that already do it.
   - Rejected: expose `request_id` only on the four mutation-ish tools — leaves
     the schema silent for search/observations even though the backend supports
     stable ids there; uneven surface for agents.
   - Rejected: a new client-side idempotency cache — the value's purpose here is
     wire-level correlation and (where the endpoint supports it) idempotent
     retry, not universal server-side deduplication. `request_id` is not a
     dedup key across the six-tool surface: the search view echoes it and uses
     it for scope/audit but re-runs retrieval every call
     (`search/views.py:22,41`, `search/services.py:76`); the observations
     listing does not pass it into the listing service at all
     (`observations/views.py`); memory-link idempotence keys on
     `(memory, link_type, target)`, not `request_id`
     (`memory/services.py:1022`). A client-side cache would still be redundant
     state layered on top of a stable id the caller already controls.

3. **Per-call `team_id` override, mirroring `project_override`.** Add `team_id`
   (string, optional) to all six schemas. Thread a `team_override` argument
   through `resolve_runtime` → `_require_runtime` → `_require_runtime_for_arguments`,
   sourced from `as_string(arguments.get('team_id'))`. Precedence mirrors
   `project_id`: **call arg > `ENGRAM_TEAM_ID` env > config**. Because
   `_scope_payload` and `list_observations` already read `runtime.team_id`, no
   further wiring is needed once the override reaches `McpRuntime.team_id`.
   - **Blank / null semantics (explicit).** `as_string` coerces a non-string
     (`None`, number) or absent key to `''` (`config.py:98-102`), and the
     `team_override or env or config` chain treats `''` as absent — so a call
     with `team_id` omitted, `null`, or `""` does **not** clear scope and does
     **not** forward a blank value; it falls back to env/config, exactly as
     `project_override` behaves. There is deliberately **no** way to clear an
     env/config team to org-wide scope via a per-call arg; that mirrors
     `project_id` and keeps a blank arg from silently HTTP-400ing. "Forwarded
     verbatim" (see Error Handling) therefore applies only to a **non-empty**
     `team_id` string.
   - Rejected: mutate the payload directly in each handler — six edits, bypasses
     the single resolution point, drifts from the `project_override` pattern.
   - Rejected: call arg loses to env — inconsistent with `project_id`, which
     lets the explicit call arg win over env (`mcp_tools.py:51-55`).
   - Rejected: treat blank `team_id` as "clear scope to org-wide" — introduces a
     second, `team_id`-only semantic the `project_id` path does not have, and the
     backend has no distinct blank-clears contract; kept symmetric instead.

Bundle discipline: `mcp_server.py`, `mcp_tools.py`, and `commands.py` are all
touched, so both plugin copies must be re-synced byte-for-byte (see checklist).

## API and Schema Changes

### mcp-install behavior (`commands.py` `run_mcp_install`)

Replace the gate at `commands.py:1810-1817`:

```python
server_url = as_string(config.get('server_url'))
if not server_url:
    raise CliError(
        'missing_config',
        'Engram config is incomplete',
        remediation_for('missing_config'),
    )

project_id = as_string(config.get('project_id'))
if not project_id:
    stderr.write(
        'warning: no project_id configured; MCP memory will route by the '
        'git remote of the working directory unless a project is selected at '
        'serve time (ENGRAM_PROJECT_ID, or a per-call project_id argument).\n'
    )
```

- `project_id` is no longer required.
- The advisory goes to `stderr` (consistent with the existing skipped-target
  warnings at `commands.py:1839-1843`); `stdout` success lines are unchanged.
- No change to `build_engram_mcp_entry` / `write_engram_mcp_entry` output.

### Tool inputSchemas (`mcp_server.py` `list_tools`)

Add these two properties to the `properties` object of **all six** tools
(`engram_search`, `engram_context`, `engram_memory_link`,
`engram_observations`, `engram_memory_version`, `engram_memory_feedback`).
Neither is added to any `required` list.

```json
"request_id": {"type": "string"},
"team_id": {"type": "string"}
```

### Handler forwarding (`mcp_tools.py`)

`search_memory` payload gains (add to the `payload.update({...})` at
`mcp_tools.py:143-150`):

```python
'request_id': _new_request_id(arguments),
```

`list_observations` params gain (in the block at `mcp_tools.py:263-269`):

```python
params['request_id'] = _new_request_id(arguments)
```

`team_id` override plumbing:

```python
def resolve_runtime(
    config_dir: str | None = None,
    *,
    project_override: str = '',
    repository_override: str | None = None,
    team_override: str = '',
) -> McpRuntime | None:
    ...
    team_id = (
        team_override
        or os.environ.get('ENGRAM_TEAM_ID')
        or as_string(config.get('team_id'))
    )
```

`_require_runtime` forwards `team_override`; `_require_runtime_for_arguments`
sources it:

```python
return _require_runtime(
    config_dir,
    project_override=as_string(arguments.get('project_id')),
    repository_override=repository_override,
    team_override=as_string(arguments.get('team_id')),
)
```

### Rendered examples

`mcp-install` project-less run (stdout unchanged, one stderr line added). The
example pins `--agent claude_code` so exactly one target is written and the
rendered output is deterministic — `--agent` defaults to `both`
(`main.py:124-129`), which also resolves a `claude_desktop` target and would
emit an extra `wrote …`/`skipped claude_desktop …` line depending on whether the
Desktop config path is writable (`commands.py:1822-1848`):

```
$ engram mcp-install --agent claude_code --config-dir ~/.engram   # config has server_url, no project_id
# stderr:
warning: no project_id configured; MCP memory will route by the git remote of the working directory unless a project is selected at serve time (ENGRAM_PROJECT_ID, or a per-call project_id argument).
# stdout:
wrote engram MCP server to /home/u/.claude.json
installed engram MCP server.
# exit 0
```

`tools/list` schema for `engram_search` (new keys shown):

```json
{
  "name": "engram_search",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "file_paths": {"type": "array", "items": {"type": "string"}},
      "symbols": {"type": "array", "items": {"type": "string"}},
      "limit": {"type": "integer"},
      "project_id": {"type": "string"},
      "request_id": {"type": "string"},
      "team_id": {"type": "string"}
    },
    "required": ["query"]
  }
}
```

Per-call `team_id` overriding a config team, env unset (both are UUIDs because
the backend serializer is a `UUIDField`; the key must be authorized to select
the team — see the authorization precondition above):

```
# config team_id = 11111111-1111-1111-1111-111111111111
tools/call engram_search {"query": "x", "team_id": "22222222-2222-2222-2222-222222222222"}
  → POST /v1/search/  body includes  "team_id": "22222222-2222-2222-2222-222222222222"
    (not the config UUID)
  → backend authorizes via _team_ids; an unauthorized key gets HTTP 403
    team_scope_denied, surfaced verbatim by _error_text. (HTTP 400 is reserved
    for the serializer rejecting a non-UUID team_id before authorization runs.)
```

## Data Flow

1. Agent calls `tools/call` with optional `request_id` / `team_id` in
   `arguments`.
2. `handle_request` (`mcp_server.py:229-257`) strips the internal repo-url key,
   attaches codex repo scope if present, and invokes the bound handler with the
   raw `arguments` dict.
3. Handler calls `_require_runtime_for_arguments`, which now also reads
   `arguments['team_id']` and passes it as `team_override`.
4. `resolve_runtime` computes `team_id` with precedence
   `team_override > ENGRAM_TEAM_ID > config.team_id` and stores it on
   `McpRuntime`.
5. `_scope_payload` (memory/context/search) and the `list_observations` param
   builder read `runtime.team_id`, so the resolved value ships on the wire.
6. `request_id`: every one of the **six** handlers now resolves
   `_new_request_id(arguments)` — caller-supplied value if present, else a fresh
   `mcp-<uuid4>` — and forwards it (payload for POST tools, params for the
   observations GET). S2's `engram_memory_get`/`engram_audit` are read GETs that
   do not forward `request_id` (see the cross-slice note at the top).
7. Install: `run_mcp_install` writes the `mcpServers` entry regardless of
   `project_id`; at serve time `resolve_runtime` decides project-vs-repo routing
   per working directory.

## Error Handling

- **Install with no `server_url`**: unchanged — `CliError('missing_config', ...)`,
  exit 1, remediation `Run \`engram connect\` first.`
  (`commands.py:76`).
- **Install with no `api_key`**: unchanged — `CliError('missing_credential', ...)`
  at `commands.py:1804-1809`, exit 1.
- **Install with `server_url` present, `project_id` absent**: success (exit 0)
  plus the stderr advisory. This is the new allowed path.
- **Serve with neither project nor repo resolvable** (project-less install +
  cwd has no git remote, and not codex-scoped): `resolve_runtime` returns `None`
  (`mcp_tools.py:64-68`), so every handler that *reaches runtime resolution*
  returns `NOT_CONFIGURED_MESSAGE` (`mcp_tools.py:18-21`) as tool text. Note the
  ordering: `fetch_context`, `create_memory_link`, `update_memory_version`, and
  `submit_memory_feedback` validate their required arguments **before** calling
  `_require_runtime_for_arguments` (`mcp_tools.py:181-183,224-225,301-302,339-343`),
  so a call missing `session_id` / the three link fields / `body` / a valid
  `action` returns its tool-specific validation message first — the
  `NOT_CONFIGURED_MESSAGE` fallback applies only to otherwise-valid calls.
  `search_memory` and `list_observations` have no pre-runtime arg gate, so they
  always surface `NOT_CONFIGURED_MESSAGE` in this state. Either way this path is
  unchanged — the relaxed install gate does not weaken serve-time safety.
- **Codex scope (`ENGRAM_MCP_CODEX_SCOPE=1`) with empty per-turn workspace**:
  `_codex_repository_url` returns `''` (`mcp_server.py:51-76`), the internal
  repo-url argument is set to `''`, `resolve_runtime` finds no project and no
  repo → `NOT_CONFIGURED_MESSAGE`. Clean, unchanged.
- **Non-empty invalid `team_id` string** (not a UUID): forwarded verbatim;
  backend returns HTTP 400, surfaced by `_error_text` as
  `Engram call failed: HTTP 400 <code>: <detail>` (`mcp_tools.py:400-407`).
  Client does no UUID validation — consistent with `project_id` handling today.
- **Blank / null / omitted `team_id`**: not forwarded; falls back to
  env/config per the blank/null semantics in Design §3 (never sent as an empty
  string, never clears scope).
- **Well-formed `team_id` the key may not select**: backend `_team_ids`
  (`access/services.py:344-374`) returns `None`, raising
  `AccessDeniedError('team_scope_denied', ...)` at `services.py:166`, which
  `ACCESS_STATUS` maps to **HTTP 403** (`services.py:63`) → `_error_text` renders
  `Engram call failed: HTTP 403 team_scope_denied: <detail>`. This is the
  expected outcome for an ordinary agent key (no team-admin capability); the
  client does not pre-check capabilities. (HTTP 400 is distinct — it applies only
  when the serializer rejects a non-UUID `team_id` before authorization runs, per
  the invalid-`team_id` case above.)
- **Empty results / capability denials**: unchanged — existing
  `No memory matched the search.`, `No observations found.`,
  `PROJECT_NOT_FOUND_MESSAGE`, and `_error_text` mapping continue to apply.

## Test Plan

Client tests are `unittest`-discovered `*_tests.py` modules. Per the repo rule
(`CLAUDE.md`) and the CLI test lane (`packages/cli/README.md:71-80`), run them
inside the backend container from the repository root, not on the host:

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro \
  -v /usr/lib/git-core:/usr/lib/git-core:ro \
  -e PYTHONPATH=/workspace/packages/cli --entrypoint python3 api \
  -m unittest discover -s packages/cli -p '*_tests.py' -v
```

The container is required for `git`-touching paths (`workspace_repository_url`)
and for a consistent interpreter/deps; do not assert a host-only lane. Follow
TDD: write the failing assertion first, then implement.

**Bundle suites are a separate lane.** The two `bundle_sync_tests.py` regression
guards live in `packages/claude-plugin/` and `packages/codex-plugin/`, **outside**
the `-s packages/cli` discovery root, and they assert more than byte drift
(plugin/marketplace version match, required hook events, hook-shim entrypoint —
`bundle_sync_tests.py:12-15,28,44+`). `sync_plugin_bundle.py --check` verifies
byte-equality only and does **not** execute those manifest/hook assertions, so the
`discover -s packages/cli` command above cannot establish the checklist step-6
gate on its own. Run them explicitly (same container lane), each discovered from
its own package root:

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro \
  -v /usr/lib/git-core:/usr/lib/git-core:ro \
  --entrypoint python3 api -m unittest discover \
  -s packages/claude-plugin -t packages/claude-plugin -p 'bundle_sync_tests.py' -v

docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro \
  -v /usr/lib/git-core:/usr/lib/git-core:ro \
  --entrypoint python3 api -m unittest discover \
  -s packages/codex-plugin -t packages/codex-plugin -p 'bundle_sync_tests.py' -v
```

### Item 1 — project-less mcp-install (`cli_lifecycle_tests.py`)

Evidence correction vs. brief: the `mcp-install` tests live in
`packages/cli/engram_cli/cli_lifecycle_tests.py` (near line 3166), **not**
`install_tests.py` (which covers `run_install`). Add there, beside the existing
`test_mcp_install_*` cases.

- `test_mcp_install_succeeds_without_project_id_warns_repo_routing` — write
  `config.json` with `server_url` only (no `project_id`) and `credentials.json`
  with `api_key`; run
  `mcp-install --agent claude_code --config-dir ... --claude-code-config ...`
  (pin `--agent claude_code` so the default `both` does not also resolve a
  `claude_desktop` target and add a `skipped`/second-`wrote` line —
  `main.py:124-129`, `commands.py:1822-1848`); assert exit 0,
  `mcpServers.engram` written, and the stderr advisory contains
  `route by the git remote`, `ENGRAM_PROJECT_ID`, **and** `per-call project_id`
  (so the advisory names both serve-time escape hatches — the env var and the
  per-call argument that also wins when the env var is unset — not just the
  default). (Fails today: `missing_config`, exit 1.)
- `test_mcp_install_still_fails_without_server_url` — config missing
  `server_url`; assert exit 1 and `missing_config` in stderr (guards the
  narrowed gate).
- `test_mcp_install_with_project_id_omits_repo_routing_warning` — write
  `config.json` **with** a `project_id` (plus `server_url`) and `credentials.json`
  with `api_key`; run `mcp-install --agent claude_code --claude-code-config ...`
  (same single-target pin as above); assert exit 0, `mcpServers.engram` written,
  and the stderr advisory string `route by the git remote` is **absent**. This
  proves the warning is conditional, not unconditional. (The existing
  configured-project case at `cli_lifecycle_tests.py:3166` only checks the API
  key does not leak to stderr and does not cover the advisory.)

### Item 2 — request_id / team_id on every schema (`mcp_server_tests.py`)

Beside `test_tools_list_all_six_schemas_expose_optional_project_id`
(`mcp_server_tests.py:114`):

- `test_tools_list_all_six_schemas_expose_optional_request_id` — for each of the
  six tools assert `properties['request_id'] == {'type': 'string'}` (full
  definition, not mere membership — mirrors the `project_id` assertion at
  `mcp_server_tests.py:126`) and `'request_id' not in required`. Asserting the
  complete `{'type': 'string'}` is required so a wrong-typed (`integer`,
  `boolean`) or unconstrained schema fails the test.
- `test_tools_list_all_six_schemas_expose_optional_team_id` — same for `team_id`:
  assert `properties['team_id'] == {'type': 'string'}` and
  `'team_id' not in required`.

### Item 2/3 — passthrough + precedence (`mcp_tools_tests.py`)

Add to `McpToolsTests` (setup already isolates env at `mcp_tools_tests.py:44-56`
and writes local config via `write_local_config`):

These are client-unit tests through a `StubTransport`: they assert the captured
payload/params the client *builds*, not backend authorization. Backend
`team_id` authorization (`_team_ids`, `access/services.py:344-374`) is out of
scope for this client slice — it is exercised by the backend access tests. Use
real UUID strings for `team_id` so the fixtures match the serializer's
`UUIDField` contract and no example implies a non-UUID would round-trip. Let
`CFG = 11111111-1111-1111-1111-111111111111`,
`ENVT = 33333333-3333-3333-3333-333333333333`,
`CALL = 22222222-2222-2222-2222-222222222222`.

- `test_search_forwards_request_id_argument` — call `search_memory` with
  `request_id='req-1'` through a `StubTransport`; assert the captured payload
  has `request_id == 'req-1'`.
- `test_search_generates_request_id_when_absent` — assert payload `request_id`
  starts with `mcp-`.
- `test_observations_forwards_request_id_argument` — call `list_observations`
  with an explicit `request_id`; assert that value is present in the GET params.
- `test_observations_generates_request_id_when_absent` — call `list_observations`
  **without** `request_id`; assert the GET params carry a `request_id` starting
  with `mcp-`. This is the test that actually proves the new
  `_new_request_id(arguments)` wiring in the observations handler (the
  explicit-value test alone would pass even if the handler forwarded only
  caller-supplied ids).
- `test_team_id_argument_overrides_config` — `write_local_config(team_id=CFG)`;
  call `search_memory` with `team_id=CALL`; assert payload `team_id == CALL`.
- `test_team_id_blank_argument_falls_back_to_config` — `write_local_config(team_id=CFG)`;
  call `search_memory` with `team_id=''` and separately with `team_id=None`;
  assert payload `team_id == CFG` in both (blank/null treated as absent, never
  forwarded as `''`).
- `test_team_id_falls_back_to_config_when_absent` — `write_local_config(team_id=CFG)`;
  call without `team_id`; assert payload `team_id == CFG`.
- `test_team_id_env_wins_over_config_and_loses_to_argument` — set
  `ENGRAM_TEAM_ID=ENVT`, config `team_id=CFG`; call with `team_id=CALL`
  → payload `CALL`; call without → payload `ENVT`.
- `test_observations_team_id_argument_overrides_config` — same override check on
  the observations GET params.

### Bundle sync guard

`packages/claude-plugin/bundle_sync_tests.py` and
`packages/codex-plugin/bundle_sync_tests.py` already assert byte-equality of
every runtime module. They fail until the bundles are re-synced; no new test
needed — they are the regression guard for the checklist step.

### Implementation checklist (order)

1. Add failing tests above (items 1, 2, 3).
2. `commands.py`: relax `run_mcp_install` gate + stderr advisory.
3. `mcp_server.py`: add `request_id` + `team_id` to all six `inputSchema`s.
4. `mcp_tools.py`: `team_override` plumbing; `request_id` forwarding in
   `search_memory` and `list_observations`.
5. Re-sync both plugin bundles with the canonical generator, not by hand:
   `python scripts/sync_plugin_bundle.py` (recreates each
   `hooks/engram_cli/` directory and copies the full canonical module set,
   removing any stale files — `sync_plugin_bundle.py:40-47`, documented at
   `packages/claude-plugin/README.md:105-107`). Then assert lockstep with
   `python scripts/sync_plugin_bundle.py --check` (exit 0). Do **not**
   selectively copy individual files: a manual copy bypasses the generator and
   leaves stale/unexpected files in place.
6. Run (inside the backend container, per the Test Plan lane) the CLI suites
   `mcp_server_tests.py`, `mcp_tools_tests.py`, `cli_lifecycle_tests.py` via the
   `discover -s packages/cli` command, **and** both `bundle_sync_tests.py` via the
   separate per-package discover commands in the Test Plan (they are outside the
   `packages/cli` root and are not run by `--check`); all green.

## Out of Scope

- **Wiring error/decision hooks into plugin manifests** — no matching runtime
  hook event exists for those; deferred, no manifest change here.
- **Server-side `trace_id` drop in the search view** — a backend concern; filed
  as a follow-up note, not touched by this client-only slice.
- **`observations` default-limit unification** — S1 owns the observations schema;
  S6 leaves the existing `limit or 10` default (`mcp_tools.py:263`) untouched to
  avoid clobbering S1.
- **Client-side UUID validation of `team_id` / `project_id`** — kept
  permissive; the backend is the validation authority.
- **`config.py` changes** — none; the file was mis-cited as a gate.
- **`request_id`/`team_id` in S2's `engram_memory_get` / `engram_audit`
  schemas** — those two schemas and their exact-schema tests are owned by S2
  (`2026-07-19-mcp-read-tools-design.md:941-943`); S6 does not touch them. The
  per-call `team_id` override still reaches both tools functionally through the
  shared `resolve_runtime` path (see the cross-slice reconciliation note); only
  the schema-level advertisement of `team_id` on those two is deferred to S2 or
  a trivial follow-up. `request_id` is intentionally omitted there because their
  read-only GET handlers do not forward it.
- **New `mcpServers` entry fields** (e.g. embedding a `repository_url` or
  `team_id` into the generated entry) — routing stays per-serve/per-cwd.
- **Backend team↔resolved-project linkage recheck** — for repo-url-routed calls
  the `_team_ids` `ProjectTeam` check runs against every org project (no
  `project_id` is sent), so an admin/operator key can pass a `team_id` linked to a
  different project than the one the `repository_url` later resolves to, and
  session validation only checks org equality (see the linkage-granularity caveat
  in Design §3). This is a **pre-existing** backend gap that predates this slice —
  `team_id` already reaches the backend from env/config on repo-routed calls — and
  the per-call override transport neither introduces nor widens it. Closing it
  (re-validate the requested team against the repository-resolved project after
  routing) is a **backend** change filed as a follow-up, out of scope for this
  client-only slice.

## Review Reconciliation

(append-only)

- round 1, no findings: refuted:false-positive — Codex companion failed to load
  configuration (`failed to load configuration: No such file or directory (os
  error 2)`) on both attempts; no task ran and no findings were produced, so
  there is nothing to reconcile. Spec unchanged.
- round 2, no findings: refuted:false-positive — Codex companion again failed to
  load configuration (`failed to load configuration: No such file or directory
  (os error 2)`); the invocation produced no numbered findings, so there is
  nothing to verify, fix, or refute. Spec unchanged.
- round 3, no findings: refuted:false-positive — the review task was forwarded to
  Codex but did not complete within the 600s foreground timeout and was moved to
  background (ID `bsot9nxq6`); no results were polled and no numbered findings
  were produced, so there is nothing to verify, fix, or refute. Spec unchanged.
- round 4, no findings: refuted:false-positive — the Codex review run timed out in
  the foreground and was moved to background (ID `b25hx8qbb`); its captured output
  ends mid-exploration (still opening repo files and issuing `wait` calls) with no
  numbered-findings block ever emitted, so there is nothing to verify, fix, or
  refute. Spec unchanged.
- round 5, finding 1: fixed — confirmed `_team_ids` (`access/services.py:344-374`)
  denies a per-call `team_id` to any key without team-admin (`teams:*`/`policy:admin`)
  or a matching bound team, and the agent key (`engram_bootstrap_golden_path.py:26-33`)
  has neither; `team_id` is a `UUIDField` so non-UUID examples 400 pre-authz. Added the
  authorization precondition to §3, converted `CFG`/`T-CALL`/`ENVT` examples and tests
  to real UUIDs, and documented `team_scope_denied` in Error Handling.
- round 5, finding 2: fixed — confirmed the repo rule + `packages/cli/README.md:71-80`
  mandate the containerized unittest lane; replaced "run without Docker" with the
  documented `docker compose … unittest discover` command and rationale.
- round 5, finding 3: fixed — confirmed search re-runs retrieval and only echoes
  `request_id` (`search/views.py:22,41`), observations does not thread it into the
  listing service, and link idempotence keys on `(memory, link_type, target)`; reworded
  the rejected client-cache alternative to say correlation/idempotent-retry, not universal
  server dedup.
- round 5, finding 4: fixed — confirmed `fetch_context`/`create_memory_link`/
  `update_memory_version`/`submit_memory_feedback` validate args before runtime resolution
  (`mcp_tools.py:181-183,224-225,301-302,339-343`); qualified the `NOT_CONFIGURED_MESSAGE`
  claim to otherwise-valid calls and noted search/observations always reach it.
- round 5, finding 5: fixed — confirmed `as_string` coerces non-strings to `''`
  (`config.py:98-102`) and the `or` chain treats `''` as absent; specified blank/null
  semantics (falls back to env/config, never forwarded blank, never clears scope) in
  Design §3 and Error Handling, plus a `test_team_id_blank_argument_falls_back_to_config`.
- round 5, finding 6: fixed — confirmed `scripts/sync_plugin_bundle.py` is the canonical
  generator (rmtree+full copy, `--check` mode) documented at
  `packages/claude-plugin/README.md:105-107`; replaced the manual three-file copy in
  checklist step 5 with `sync_plugin_bundle.py` + `--check`.
- round 5, finding 7: fixed — the prior suite tested observations `request_id` only with an
  explicit value, which a caller-only forwarder would pass; added
  `test_observations_generates_request_id_when_absent` to prove the `_new_request_id` wiring.
- round 5, finding 8: fixed — the prior suite never asserted the advisory is absent when a
  project is configured (existing `cli_lifecycle_tests.py:3166` only checks key non-leak);
  added `test_mcp_install_with_project_id_omits_repo_routing_warning`.
- round 6, finding 1: fixed — confirmed `ACCESS_STATUS['team_scope_denied'] = HTTP_403`
  (`access/services.py:63`), raised at `services.py:166`, and `_error_text`
  (`mcp_tools.py:400-407`) renders `HTTP {status} {code}` from the backend response, so the
  wire result is `HTTP 403 team_scope_denied`, not 400. Corrected both occurrences (the
  `engram_search` worked example and the Error Handling "key may not select" case) to HTTP
  403 and noted HTTP 400 is reserved for the serializer rejecting a non-UUID `team_id` before
  authz.
- round 6, finding 2: fixed — confirmed the existing `project_id` regression asserts the full
  `{'type': 'string'}` definition (`mcp_server_tests.py:126`) while the proposed
  `request_id`/`team_id` tests only checked membership, which would pass an integer/boolean/
  unconstrained schema; strengthened both test descriptions to assert
  `properties[key] == {'type': 'string'}`.
- round 7, finding 1 (major): fixed — confirmed for a repo-url-routed call `_project_ids`
  resolves `project_ids` to every org project (`access/services.py:296-299`), so the
  `_team_ids` `ProjectTeam` linkage check (`services.py:357-372`) passes on linkage to *any*
  org project, not the repo-resolved one; repository resolution runs post-authz and
  `Session.clean` validates only team org-equality (`core/models.py:401-406`). Corrected the
  §3 authorization precondition (was overpromising "matching `ProjectTeam` linkage"), added a
  linkage-granularity caveat noting the transport is pre-existing (team_id already flows from
  env/config on repo-routed calls, so no new reach), and filed the backend team↔resolved-project
  recheck in Out of Scope as a backend follow-up.
- round 7, finding 2 (minor): fixed — confirmed serve-time precedence
  `project_override > ENGRAM_PROJECT_ID > config.project_id` and that any effective project sets
  `repository_url = ''` (`mcp_tools.py:51-59`), so a serve-time env/override falsifies an
  unconditional "route by the git remote" advisory install time cannot verify; reworded the
  advisory (Design §1, code block, rendered example) to name the `ENGRAM_PROJECT_ID` escape hatch
  and strengthened `test_mcp_install_succeeds_without_project_id_warns_repo_routing` to assert the
  advisory contains `ENGRAM_PROJECT_ID`.
- round 7, finding 3 (minor): fixed — confirmed both `bundle_sync_tests.py` live in
  `packages/claude-plugin/` and `packages/codex-plugin/`, outside `-s packages/cli`, and assert
  manifest/hook-shim invariants beyond byte drift (`bundle_sync_tests.py:12-15,28,44+`) that
  `sync_plugin_bundle.py --check` does not execute; added two explicit per-package
  `unittest discover` commands to the Test Plan and updated checklist step 6 to run them as a
  separate lane so the stated gate is actually established.
- round 8, finding 1 (major): fixed — confirmed S2 adds `engram_memory_get`+`engram_audit`
  (`2026-07-19-mcp-read-tools-design.md:941-943` asserts an 8-tool list + exact schemas for both,
  neither carrying `request_id`/`team_id`), and that both S2 tools resolve runtime via the shared
  `_require_runtime_for_arguments` and already forward `runtime.team_id` (S2:116,506-512,625). So
  S6's `team_override` reaches all eight tools functionally at the runtime layer while its schema
  edits/tests own only the six original tools, and `request_id` is inert on the two read GETs
  (their handlers never call `_new_request_id`). Added a "Cross-slice tool-count reconciliation
  (S2)" note fixing "all six/every handler" to the six named tools, tightened the Data Flow
  `request_id` step, and added an Out-of-Scope item deferring `team_id`/`request_id` schema
  advertisement on the two S2 tools — removing the six-or-eight ambiguity.
- round 8, finding 2 (minor): fixed — confirmed a per-call `project_id` (`project_override`,
  `mcp_tools.py:51-55`) also suppresses repo routing even when `ENGRAM_PROJECT_ID` is unset, so the
  advisory naming only the env var was incomplete/false; broadened the advisory (Design §1 prose,
  code block, rendered example) to name both escape hatches (`ENGRAM_PROJECT_ID` and a per-call
  `project_id` argument) and strengthened the test to assert `per-call project_id` in the text.
- round 8, finding 3 (minor): fixed — confirmed `--agent` defaults to `both` (`main.py:124-129`),
  so `resolve_mcp_targets` also yields a `claude_desktop` target that either writes a second
  `wrote …` line or emits `skipped claude_desktop …` stderr + `skipped:` stdout
  (`commands.py:1822-1848`), contradicting the single-target rendered example; pinned
  `--agent claude_code` in the rendered example and both item-1 test invocations (with the
  rationale inline) so the asserted one-line output is deterministic.
- round 9: no findings — reviewer returned AIRTIGHT; no dispositions to record. Spec unchanged
  this round (Review Reconciliation entry only).
