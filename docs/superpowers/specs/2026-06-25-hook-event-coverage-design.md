# Hook Event Coverage Design

## Goal

Close the remaining first-parity-gate hook/client gaps without widening into
MCP, frontend, semantic retrieval, package publishing, or full Claude Code
native installation.

The slice makes the current thin CLI usable as a real hook adapter for the
first Codex path, adds missing hook event coverage for session start, errors,
and decisions, fixes the generated hook command contract, and records Compose
E2E evidence in the verification matrix.

## Current Evidence

Current `master` at `aad2ade3d46fdf9f6eecff3feb92380a3203793c` already has:

- hook ingest for `post_tool_use` and `session_end`;
- context retrieval through `/v1/context/session-start`;
- CLI `connect`, `doctor`, `disconnect`, `hook post-tool-use`, and
  `hook session-start`;
- the Compose golden path script and workflow;
- migration compatibility through the upstream import path.

Audits found the remaining gate gaps:

- no committed hook endpoint/client coverage for `session_start`, `error`, or
  `decision` capture;
- generated local hook manifest command is invalid because it writes
  `engram hook --agent ...` instead of an event-specific subcommand;
- Codex native response shape is not adapted because the CLI prints server
  snake_case JSON directly;
- native Codex package manifests are placeholders only;
- Claude Code package remains inactive and must not be claimed as implemented;
- `docs/verification-matrix.md` does not record the successful Compose golden
  path evidence.

## Design

### Backend Hook Events

Keep the existing generic `HookIngestView` and add three narrow URL/view
bindings:

- `POST /v1/hooks/session-start` expects `event_type == "session_start"`;
- `POST /v1/hooks/error` expects `event_type == "error"`;
- `POST /v1/hooks/decision` expects `event_type == "decision"`.

All three reuse `HookEventSerializer` and `IngestHookEvent`. They persist the
same durable hook records as `post-tool-use`: `RawEventEnvelope`,
`AgentSession`, `Observation`, and `ObservationSource`. They also use the same
scope resolution, redaction, idempotency, replay, and cross-project denial
rules.

The worker handoff must stay plug-and-play through the existing
`django-celery-outbox` dependency: the ingest path calls the Celery task's
`.delay(...)` method and the package records the queued transport message. This
slice must not add a new outbox framework, relay, model, or local worker path.

`session_start` capture is separate from context retrieval. The CLI
`session-start` command will submit the hook event first, then request
`/v1/context/session-start`.

### CLI Hook Commands

Extend `engram hook` to support:

```bash
engram hook post-tool-use
engram hook session-start
engram hook error
engram hook decision
```

`post-tool-use`, `error`, and `decision` submit one event to `/v1/hooks/*`.

`session-start` submits a `session_start` event to `/v1/hooks/session-start`
and then submits the context request to `/v1/context/session-start`. The default
stdout remains the server response JSON so existing Compose golden path
assertions continue to work.

Add `--response-format server|codex` with default `server`. `codex` adapts
stdout to a Codex hook-compatible shape. For session start this includes:

```json
{
  "continue": true,
  "systemMessage": "...rendered context...",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "...rendered context..."
  }
}
```

For non-context hook commands the Codex output is:

```json
{
  "continue": true
}
```

Errors continue to use stderr with redaction. No raw API key is printed.

### Local Hook Manifest

Fix the local hook metadata written by `engram connect`. It must no longer write
the invalid command `engram hook --agent ...`.

The local hook manifest should expose event-specific commands:

```json
{
  "commands": {
    "SessionStart": "engram hook session-start --agent codex",
    "PostToolUse": "engram hook post-tool-use --agent codex",
    "Error": "engram hook error --agent codex",
    "Decision": "engram hook decision --agent codex"
  }
}
```

The manifest remains local metadata, not a published native plugin artifact.

### Codex Plugin Package

Add a minimal package-local Codex plugin contract under `packages/codex-plugin`
that is testable but not published:

- `packages/codex-plugin/.codex-plugin/plugin.json`;
- `packages/codex-plugin/plugin/hooks/codex-hooks.json`.

The hooks invoke the Python CLI:

- `SessionStart` uses `engram hook session-start --agent codex --response-format codex`;
- `PostToolUse` uses `engram hook post-tool-use --agent codex --response-format codex`;
- `Error` uses `engram hook error --agent codex --response-format codex`;
- `Decision` uses `engram hook decision --agent codex --response-format codex`.

The plugin package is intentionally a fixture/contract for the parity gate. It
does not install itself into a user profile and does not publish to any plugin
marketplace.

### Claude Code Classification

Do not claim full Claude Code native support in this slice. Update the parity
map to say:

- Codex native hook contract is implemented for the first parity path.
- Claude Code remains `defer` for native plugin installation and
  Claude-specific hook response formatting.
- Claude Code still shares the server API and CLI runtime enum, but the native
  package is not part of this checkpoint.

This keeps `goal.md` honest: the first parity gate remains in progress until
the final evidence report explicitly classifies every runtime path.

### Compose E2E Evidence

Run and record the current Compose golden path evidence with exact commands and
exit codes. Docker is available in the current environment, so local evidence
must be stronger than a workflow-only claim.

Update `docs/verification-matrix.md` with:

- the local `python3 scripts/e2e_golden_path.py` result;
- any first decisive failure and fix;
- GitHub Actions `Compose E2E` status once the PR runs;
- the CLI/backend/repository checks for this slice.

## Security

This slice touches hook input, local credentials, and context returned to agent
runtimes.

Required checks:

- raw API keys and bearer tokens must not appear in stdout, stderr, generated
  local public config, plugin manifests, context responses, or test logs;
- hook payload redaction must still apply before persistence;
- wrong project or team must be denied before writes;
- duplicate hook submissions must not create duplicate raw events,
  observations, queued Celery transport messages, memories, or retrieval
  documents;
- Codex response formatting must not expose server-only audit ids unless they
  are already present in the server JSON and requested through `server` format.

Because this is hook/client trust-boundary work, the implementation must get a
focused independent security review before final PR promotion.

## Verification

Required local commands:

- `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v`
- `python3 -m compileall packages/cli/engram_cli`
- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py -v"`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check ."`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff format --check ."`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"`
- `python3 scripts/e2e_golden_path.py`
- `git diff --check HEAD`

Required CI:

- Backend;
- Repository Quality;
- Compose E2E.

## Out Of Scope

- Claude Code native plugin implementation;
- MCP;
- frontend/admin UI;
- semantic/vector retrieval;
- provider/model generation;
- plugin marketplace publishing;
- managed installer writing user agent configuration;
- Cursor, Gemini, OpenAI Agents, or other runtimes.

## Self-Review

The slice is narrow enough for one checkpoint: it closes hook event breadth,
Codex adapter shape, manifest correctness, and evidence recording. It does not
change storage models or the memory-worker architecture. It keeps Claude Code
honestly deferred rather than faking support through generic CLI state.
