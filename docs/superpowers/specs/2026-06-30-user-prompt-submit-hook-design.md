# Design: UserPromptSubmit hook (per-turn context injection + prompt capture)

> Branch `feat/user-prompt-submit-hook`, off current master. Roadmap Слой 3 agent-loop
> closure (after SessionEnd #51). Scout-verified surface. Tests on postgres+pgvector.

## Problem
Engram injects a context bundle only at `SessionStart`; it goes stale as the session
evolves, and the user's prompts (high-signal intent) are never captured. claude-mem used
`UserPromptSubmit` to (a) record prompt/session state and (b) inject per-turn context.
The whole retrieval stack already exists — only a per-turn entry point is missing.

## Target
A `UserPromptSubmit` hook that, on each user prompt: ingests the prompt as an observation
AND returns a fresh context bundle for injection (`additionalContext`), reusing the exact
SessionStart retrieval/ranking. **No model change** (`ContextBundle.purpose` is a free
`CharField(max_length=80)` — verified `core/models.py:684`; hook `event_type` has no enum,
validated per-view via `expected_event_type` — verified `hooks/views.py`).

## Design (mirror the SessionStart path everywhere)

### Backend
1. `hooks/views.py`: add `class UserPromptSubmitView(HookIngestView): expected_event_type = 'user_prompt_submit'` (mirror `SessionStartHookView`).
2. `hooks/urls.py`: `path('user-prompt-submit', UserPromptSubmitView.as_view(), name='hook-user-prompt-submit')`.
3. `context/views.py`: add `class UserPromptSubmitContextView(ContextView): purpose = 'user_prompt_submit'` (mirror `SessionStartContextView`, which is `purpose = 'session_start'`).
4. `context/urls.py`: `path('user-prompt-submit', UserPromptSubmitContextView.as_view(), name='context-user-prompt-submit')`.
5. `context/services.py` `ContextBundleResult.to_response()` (~line 102-109): add an
   `elif self.bundle.purpose == 'user_prompt_submit':` branch emitting
   `{'hookEventName': 'UserPromptSubmit', 'additionalContext': rendered_context}`. The
   existing `session_start` branch stays BYTE-IDENTICAL; default (other purposes) unchanged.
6. Observation: `_get_or_create_observation` derives `observation_type = type or event_type`
   (`hooks/services.py:326`) → a `user_prompt_submit` event yields
   `observation_type='user_prompt_submit'`. **No code change, no model change.**

### CLI (`packages/cli`) — DOUBLE quotes (package convention)
7. `main.py:104`: add `"user-prompt-submit"` to the hook subparser tuple.
8. `commands.py` `run_hook`: add an `elif args.hook_command == "user-prompt-submit":` branch
   mirroring the `session-start` branch — POST `/v1/hooks/user-prompt-submit`
   (`event_type="user_prompt_submit"`) via `send_hook_event`; if non-2xx → raise; then
   `post_json` to `/v1/context/user-prompt-submit` with
   `build_user_prompt_submit_payload(config, runtime, input_payload)`. Sets `status, body`
   and falls through the shared tail (status check + `format_hook_response`).
9. `commands.py`: add `build_user_prompt_submit_payload(...)` = a copy of
   `build_session_start_payload` (same fields: session_id, request_id, query, file_paths,
   symbols, limit, token_budget, optional strings).
10. `commands.py` `format_hook_response`: add an `elif hook_command == "user-prompt-submit":`
    branch mirroring the `session-start` branch but with `hookEventName="UserPromptSubmit"`
    (claude-code: `{systemMessage, hookSpecificOutput{...}}`; codex: `{continue, systemMessage,
    hookSpecificOutput{...}}`).

### Plugins + contract + CI
11. `packages/claude-plugin/hooks/hooks.json`: add `UserPromptSubmit` (matcher `"*"`,
    timeout 60, command `engram hook user-prompt-submit --agent claude_code --response-format claude-code`).
12. `packages/codex-plugin/plugin/hooks/codex-hooks.json`: add `UserPromptSubmit` (matcher
    `".*"`, command `engram hook user-prompt-submit --agent codex --response-format codex`).
13. Both contract tests: `REQUIRED_HOOK_EVENTS += ("UserPromptSubmit",)` → 6 events; the
    `len(REQUIRED_HOOK_EVENTS)==len(commands)` assertion enforces 6 commands in lockstep.
14. CI plugin-contract steps already exist (added in #51) — they'll cover the new event.
15. `write_local_state` commands dict (`commands.py`): add a `"UserPromptSubmit"` entry
    mirroring `"SessionStart"` so `engram connect` writes it into the runtime manifest. If a
    CLI test asserts the connect-written manifest command set, update it.

### Docs
16. Update the plugin READMEs hook tables + lifecycle note (mirror the SessionEnd doc edit):
    `UserPromptSubmit` fires per prompt and injects a fresh bundle.

## TDD / tests (postgres+pgvector)
- Backend `hooks/hook_ingest_tests.py`: a `user_prompt_submit` ingest → 202, creates an
  Observation with `observation_type='user_prompt_submit'` on the right session (mirror the
  session_start ingest test).
- Backend `context/context_api_tests.py`: POST `/v1/context/user-prompt-submit` with a query
  over a seeded memory → `rendered_context` non-empty and the response
  `hook_specific_output.hookEventName == 'UserPromptSubmit'` (mirror the session-start
  context test).
- CLI `cli_lifecycle_tests.py`: `engram hook user-prompt-submit` posts BOTH calls
  (`/v1/hooks/user-prompt-submit` then `/v1/context/user-prompt-submit`) and emits
  `hookSpecificOutput.hookEventName=='UserPromptSubmit'` with the rendered context; a non-2xx
  hook response → exit 1 (mirror the session-start lifecycle test, which uses `FakeTransport`
  with two queued responses).
- Plugin contract tests pass with 6 events.

## Out of scope
`PreToolUse`, Cursor adapter (per user). No reranking/retrieval changes — pure reuse.

## Gate (all must pass; baseline 692 backend / CLI 53 / contract 2+2 from master)
Backend (`engram-prod` + `engram-pg`): `ruff check .`, `ruff format --check .`, `migrate`,
`makemigrations --check --dry-run` → **No changes detected** (no model change), `pytest -q`.
CLI + plugins (throwaway container, repo root mounted):
`PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py'`,
and the same for `packages/claude-plugin` and `packages/codex-plugin`.
