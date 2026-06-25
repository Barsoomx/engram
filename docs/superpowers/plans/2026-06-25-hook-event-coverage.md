# Hook Event Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the first-parity-gate hook/client coverage gaps for
session-start capture, error events, decision events, runnable hook commands,
Codex response shape, and Compose E2E evidence.

**Architecture:** Reuse the existing `HookIngestView`, `HookEventSerializer`,
and `IngestHookEvent` service for new event-specific backend endpoints. Extend
the thin Python CLI rather than adding local workers, and add package-local
Codex plugin manifests as contract fixtures. Record exact verification evidence
instead of claiming parity from implementation presence.

**Tech Stack:** Python 3.12, Django REST Framework, existing Engram hook/context
services, stdlib CLI, unittest, pytest, Docker Compose, GitHub Actions.

## Global Constraints

- Work on branch `feat/parity-14-hook-event-coverage`.
- Do not implement MCP, frontend/admin UI, semantic retrieval, provider/model
  generation, plugin marketplace publishing, managed installer writes, or full
  Claude Code native support.
- Keep clients thin: no local memory database, local worker, local queue,
  cached context bundle, embeddings, or provider secrets.
- Preserve existing default `engram hook session-start` server JSON output for
  Compose E2E compatibility.
- Add `--response-format server|codex`; default is `server`.
- Raw API keys and bearer tokens must not appear in stdout, stderr, generated
  public config, plugin manifests, context responses, or test logs.
- Use the existing `django-celery-outbox` dependency through Celery task
  `.delay(...)`; do not add a new outbox framework, relay, model, or local
  worker path in this slice.
- Use TDD where code behavior changes.
- Use single quotes in Python.
- Run backend Python commands inside Docker Compose when the command exercises
  migrations or PostgreSQL-only behavior.

---

### Task 1: Planning Checkpoint

**Files:**

- Create:
  `docs/superpowers/specs/2026-06-25-hook-event-coverage-design.md`
- Create: `docs/superpowers/plans/2026-06-25-hook-event-coverage.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/parity/claude-mem-parity-map.md`, audit findings,
  current CLI/backend hook contracts.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write design and plan**

  Capture the selected scope:

  - backend `/v1/hooks/session-start`, `/v1/hooks/error`,
    `/v1/hooks/decision`;
  - CLI `hook error` and `hook decision`;
  - `session-start` event capture before context request;
  - `--response-format codex`;
  - local hook manifest command fix;
  - package-local Codex plugin manifests;
  - explicit Claude Code defer classification;
  - Compose E2E evidence recording.

- [ ] **Step 2: Run docs sanity checks**

  Run:

  ```bash
  python3 scripts/repository_quality.py
  git diff --check HEAD
  ```

  Expected: both commands exit 0.

- [ ] **Step 3: Commit**

  Commit:

  ```bash
  git add docs/superpowers/specs/2026-06-25-hook-event-coverage-design.md docs/superpowers/plans/2026-06-25-hook-event-coverage.md
  git commit -m "chore: add hook event coverage plan"
  ```

### Task 2: Backend Hook Event Endpoints

**Files:**

- Modify: `apps/backend/engram/hooks/urls.py`
- Modify: `apps/backend/engram/hooks/views.py`
- Modify: `apps/backend/engram/hooks/hook_ingest_tests.py`

**Interfaces:**

- Consumes: `HookIngestView.expected_event_type`,
  `IngestHookEvent.execute(data: HookEventInput) -> HookIngestResult`.
- Produces:
  - `POST /v1/hooks/session-start`;
  - `POST /v1/hooks/error`;
  - `POST /v1/hooks/decision`.

- [ ] **Step 1: Add failing endpoint tests**

  Add tests to `apps/backend/engram/hooks/hook_ingest_tests.py`:

  ```python
  @pytest.mark.django_db
  def test_session_start_hook_persists_lifecycle_event_and_queues_worker_task() -> None:
      organization, project, team, raw_key = create_hook_scope()
      payload = valid_hook_payload(
          project,
          team,
          event_type='session_start',
          event_id='session-start-event-1',
          idempotency_key='session-start-idempotency-1',
          payload={'trigger': 'startup', 'cwd': '/workspace/engram'},
          observation={
              'type': 'session_start',
              'title': 'Session started',
              'body': 'Agent session started for backend work.',
              'files_read': [],
              'files_modified': [],
          },
      )

      response = APIClient().post('/v1/hooks/session-start', payload, format='json', **auth_headers(raw_key))

      assert response.status_code == 202
      body = response.json()
      assert RawEventEnvelope.objects.get().event_type == 'session_start'
      assert Observation.objects.get().observation_type == 'session_start'
      queued = CeleryOutbox.objects.get()
      assert queued.task_name == 'engram.memory.process_observation_recorded_outbox'
      assert queued.args == [body['outbox_event_id']]
  ```

  Add equivalent tests for:

  - `/v1/hooks/error` with `event_type='error'`;
  - `/v1/hooks/decision` with `event_type='decision'`;
  - mismatched endpoint/event type returns 400;
  - replay returns duplicate without new hook records or queued Celery transport
    messages;
  - wrong project is denied before hook records or queued Celery transport
    messages are written.

- [ ] **Step 2: Run tests to verify RED**

  Run:

  ```bash
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py::test_session_start_hook_persists_lifecycle_event_and_queues_worker_task -v"
  ```

  Expected before implementation: failure with HTTP 404 for
  `/v1/hooks/session-start`.

- [ ] **Step 3: Add view classes**

  Add to `apps/backend/engram/hooks/views.py`:

  ```python
  class SessionStartHookView(HookIngestView):
      expected_event_type = 'session_start'


  class ErrorHookView(HookIngestView):
      expected_event_type = 'error'


  class DecisionHookView(HookIngestView):
      expected_event_type = 'decision'
  ```

- [ ] **Step 4: Add URL routes**

  Update `apps/backend/engram/hooks/urls.py`:

  ```python
  from engram.hooks.views import (
      DecisionHookView,
      ErrorHookView,
      HookDryRunView,
      PostToolUseView,
      SessionEndView,
      SessionStartHookView,
  )

  urlpatterns = [
      path('dry-run', HookDryRunView.as_view(), name='hook-dry-run'),
      path('post-tool-use', PostToolUseView.as_view(), name='hook-post-tool-use'),
      path('session-start', SessionStartHookView.as_view(), name='hook-session-start'),
      path('error', ErrorHookView.as_view(), name='hook-error'),
      path('decision', DecisionHookView.as_view(), name='hook-decision'),
      path('session-end', SessionEndView.as_view(), name='hook-session-end'),
  ]
  ```

- [ ] **Step 5: Verify backend hook tests**

  Run:

  ```bash
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py -v"
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check engram/hooks"
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff format --check engram/hooks"
  ```

  Expected: all commands exit 0.

- [ ] **Step 6: Commit**

  Commit:

  ```bash
  git add apps/backend/engram/hooks/urls.py apps/backend/engram/hooks/views.py apps/backend/engram/hooks/hook_ingest_tests.py
  git commit -m "feat: add hook event endpoints"
  ```

### Task 3: CLI Hook Commands And Codex Response Adapter

**Files:**

- Modify: `packages/cli/engram_cli/main.py`
- Modify: `packages/cli/engram_cli/commands.py`
- Modify: `packages/cli/engram_cli/cli_lifecycle_tests.py`
- Modify: `packages/cli/README.md`

**Interfaces:**

- Consumes:
  - `post_json(transport, server_url, path, api_key, payload)`;
  - local config from `write_local_state(...)`;
  - backend endpoints from Task 2.
- Produces:
  - `engram hook error`;
  - `engram hook decision`;
  - `engram hook session-start --response-format codex`;
  - valid local hook manifest command metadata.

- [ ] **Step 1: Add failing CLI regression tests**

  Add tests to `packages/cli/engram_cli/cli_lifecycle_tests.py`:

  ```python
  def test_connect_writes_event_specific_hook_commands(self) -> None:
      with tempfile.TemporaryDirectory() as tmp:
          config_dir = Path(tmp)
          self.connect(config_dir)

          codex_hook = read_json(config_dir / 'hooks' / 'codex.json')

          self.assertEqual(
              'engram hook session-start --agent codex',
              codex_hook['commands']['SessionStart'],
          )
          self.assertEqual(
              'engram hook post-tool-use --agent codex',
              codex_hook['commands']['PostToolUse'],
          )
          self.assertEqual('engram hook error --agent codex', codex_hook['commands']['Error'])
          self.assertEqual('engram hook decision --agent codex', codex_hook['commands']['Decision'])
  ```

  Add tests that:

  - `hook error` posts to `/v1/hooks/error` with `event_type='error'`;
  - `hook decision` posts to `/v1/hooks/decision` with
    `event_type='decision'`;
  - `hook session-start` first posts `/v1/hooks/session-start`, then posts
    `/v1/context/session-start`;
  - `hook session-start --response-format codex` emits top-level
    `hookSpecificOutput`, `systemMessage`, and `continue`;
  - invalid `--response-format xml` exits 2 through argparse;
  - stdout/stderr do not contain `RAW_KEY`.

- [ ] **Step 2: Run tests to verify RED**

  Run:

  ```bash
  PYTHONPATH=packages/cli python3 -m unittest engram_cli.cli_lifecycle_tests.CliLifecycleTests.test_connect_writes_event_specific_hook_commands -v
  ```

  Expected before implementation: failure because `commands` is missing or the
  stored command is `engram hook --agent codex`.

- [ ] **Step 3: Extend CLI parser**

  Update `packages/cli/engram_cli/main.py`:

  ```python
  for command in ('post-tool-use', 'session-start', 'error', 'decision'):
      hook_command = hook_subparsers.add_parser(command)
      hook_command.add_argument('--agent', choices=('codex', 'claude-code', 'claude_code'))
      hook_command.add_argument('--config-dir')
      hook_command.add_argument('--response-format', choices=('server', 'codex'), default='server')
  ```

- [ ] **Step 4: Add generic event payload builder**

  In `packages/cli/engram_cli/commands.py`, add:

  ```python
  def build_generic_hook_payload(
      config: dict[str, object],
      runtime: str,
      input_payload: dict[str, object],
      event_type: str,
  ) -> dict[str, object]:
      event_id = payload_string(input_payload, 'event_id') or f'engram-cli-{uuid.uuid4()}'
      payload = dict_value(input_payload.get('payload'))
      observation = dict_value(input_payload.get('observation'))
      request_payload = base_hook_payload(config, runtime, input_payload)
      request_payload.update(
          {
              'session_id': required_payload_string(input_payload, 'session_id'),
              'event_id': event_id,
              'idempotency_key': payload_string(input_payload, 'idempotency_key') or event_id,
              'event_type': event_type,
              'payload_schema_version': payload_string(input_payload, 'payload_schema_version') or 'v1',
              'content_hash': payload_string(input_payload, 'content_hash')
              or stable_content_hash({'payload': payload, 'observation': observation, 'event_id': event_id}),
              'request_id': payload_string(input_payload, 'request_id') or event_id,
              'payload': payload,
          },
      )
      if observation:
          request_payload['observation'] = observation
      copy_optional_strings(
          request_payload,
          input_payload,
          ('agent_external_id', 'correlation_id', 'trace_id', 'repository_url', 'repository_root', 'branch', 'cwd'),
      )

      return request_payload
  ```

  Refactor `build_post_tool_use_payload(...)` to call this helper with
  `event_type='post_tool_use'`.

- [ ] **Step 5: Route hook commands**

  Update `run_hook(...)` so:

  ```python
  if args.hook_command == 'post-tool-use':
      status, body = send_hook_event(..., path='/v1/hooks/post-tool-use', event_type='post_tool_use')
  elif args.hook_command == 'error':
      status, body = send_hook_event(..., path='/v1/hooks/error', event_type='error')
  elif args.hook_command == 'decision':
      status, body = send_hook_event(..., path='/v1/hooks/decision', event_type='decision')
  elif args.hook_command == 'session-start':
      hook_status, hook_body = send_hook_event(..., path='/v1/hooks/session-start', event_type='session_start')
      context_status, context_body = post_json(..., path='/v1/context/session-start', payload=build_session_start_payload(...))
      body = context_body
  ```

  Keep HTTP error handling after every server call.

- [ ] **Step 6: Add Codex response adapter**

  Add:

  ```python
  def format_hook_response(body: dict[str, object], response_format: str, hook_command: str) -> dict[str, object]:
      if response_format == 'server':
          return body
      if hook_command == 'session-start':
          rendered = as_string(body.get('rendered_context'))

          return {
              'continue': True,
              'systemMessage': rendered,
              'hookSpecificOutput': {
                  'hookEventName': 'SessionStart',
                  'additionalContext': rendered,
              },
          }

      return {'continue': True}
  ```

  Use this before writing stdout:

  ```python
  stdout.write(json.dumps(format_hook_response(body, args.response_format, args.hook_command), sort_keys=True) + '\n')
  ```

- [ ] **Step 7: Fix local hook manifest commands**

  In `write_local_state(...)`, replace the single invalid `command` field with:

  ```python
  'commands': {
      'SessionStart': f'engram hook session-start --agent {runtime}',
      'PostToolUse': f'engram hook post-tool-use --agent {runtime}',
      'Error': f'engram hook error --agent {runtime}',
      'Decision': f'engram hook decision --agent {runtime}',
  }
  ```

- [ ] **Step 8: Update CLI README and verify**

  Document:

  ```bash
  python -m engram_cli hook error < hook.json
  python -m engram_cli hook decision < hook.json
  python -m engram_cli hook session-start --response-format codex < hook.json
  ```

  Run:

  ```bash
  PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
  python3 -m compileall packages/cli/engram_cli
  ```

  Expected: both commands exit 0.

- [ ] **Step 9: Commit**

  Commit:

  ```bash
  git add packages/cli
  git commit -m "feat: add cli hook event coverage"
  ```

### Task 4: Codex Plugin Contract

**Files:**

- Create: `packages/codex-plugin/.codex-plugin/plugin.json`
- Create: `packages/codex-plugin/plugin/hooks/codex-hooks.json`
- Modify: `packages/codex-plugin/README.md`
- Create: `packages/codex-plugin/codex_plugin_contract_tests.py`

**Interfaces:**

- Consumes: CLI commands from Task 3.
- Produces: package-local native Codex hook contract fixture.

- [ ] **Step 1: Add failing repository contract tests**

  Add assertions that:

  - `packages/codex-plugin/.codex-plugin/plugin.json` exists;
  - `packages/codex-plugin/plugin/hooks/codex-hooks.json` exists;
  - hook JSON contains `SessionStart`, `PostToolUse`, `Error`, and `Decision`;
  - every command includes `engram hook`, `--agent codex`, and
    `--response-format codex`;
  - no command contains `claude-mem`.

  Run:

  ```bash
  python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v
  ```

  Expected before implementation: failure naming missing Codex plugin files.

- [ ] **Step 2: Add Codex plugin manifest**

  Create `packages/codex-plugin/.codex-plugin/plugin.json`:

  ```json
  {
    "name": "engram",
    "version": "0.1.0",
    "description": "Thin Engram hook adapter for Codex.",
    "hooks": "../plugin/hooks/codex-hooks.json",
    "interface": {
      "displayName": "Engram",
      "shortDescription": "Shared engineering memory for AI coding agents.",
      "longDescription": "Engram captures coding-session activity and injects relevant server-backed context into future agent sessions.",
      "developerName": "Engram",
      "category": "Productivity",
      "capabilities": ["Read", "Write"]
    }
  }
  ```

- [ ] **Step 3: Add Codex hook manifest**

  Create `packages/codex-plugin/plugin/hooks/codex-hooks.json`:

  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "startup|resume",
          "hooks": [
            {
              "type": "command",
              "command": "engram hook session-start --agent codex --response-format codex",
              "timeout": 60
            }
          ]
        }
      ],
      "PostToolUse": [
        {
          "matcher": ".*",
          "hooks": [
            {
              "type": "command",
              "command": "engram hook post-tool-use --agent codex --response-format codex",
              "timeout": 120
            }
          ]
        }
      ],
      "Error": [
        {
          "matcher": ".*",
          "hooks": [
            {
              "type": "command",
              "command": "engram hook error --agent codex --response-format codex",
              "timeout": 60
            }
          ]
        }
      ],
      "Decision": [
        {
          "matcher": ".*",
          "hooks": [
            {
              "type": "command",
              "command": "engram hook decision --agent codex --response-format codex",
              "timeout": 60
            }
          ]
        }
      ]
    }
  }
  ```

- [ ] **Step 4: Update README**

  Replace placeholder text in `packages/codex-plugin/README.md` with:

  ```markdown
  # Codex Plugin

  Package-local contract fixture for Codex hook integration.

  The hooks call the thin Python CLI and require `engram connect` to have
  written local server credentials first. This package is not published by this
  checkpoint and does not install itself into a user profile.
  ```

- [ ] **Step 5: Verify and commit**

  Run:

  ```bash
  python3 -m unittest discover -s packages/codex-plugin -p '*_tests.py' -v
  python3 scripts/repository_layout.py
  python3 scripts/repository_quality.py
  git diff --check HEAD
  ```

  Expected: all commands exit 0.

  Commit:

  ```bash
  git add packages/codex-plugin
  git commit -m "feat: add codex hook plugin contract"
  ```

### Task 5: Parity Map And Evidence

**Files:**

- Modify: `docs/parity/claude-mem-parity-map.md`
- Modify: `docs/verification-matrix.md`
- Modify: `deploy/compose/README.md`

**Interfaces:**

- Consumes: implementation and verification from Tasks 2-4.
- Produces: committed gate evidence and honest runtime classification.

- [ ] **Step 1: Update parity map**

  In `docs/parity/claude-mem-parity-map.md`, update the first parity gate
  surface:

  - Codex native hook contract: implemented for `SessionStart`, `PostToolUse`,
    `Error`, and `Decision` through `packages/codex-plugin`;
  - Claude Code native package: deferred with rationale that the first path is
    Codex and Claude-specific response formatting/installer writes remain a
    separate checkpoint;
  - `PreToolUse`, `Stop`, and transcript replay remain deferred or handled by
    migration import, not runtime.

- [ ] **Step 2: Update Compose README**

  Replace the stale inactive text in `deploy/compose/README.md` with active
  status:

  ```markdown
  # Compose Deployment

  Local self-hosted profile for the parity backend runtime: API, worker,
  PostgreSQL, and Redis-compatible broker.

  The Compose E2E workflow runs `scripts/e2e_golden_path.py` to prove the
  first CLI/hook-to-context loop.
  ```

- [ ] **Step 3: Run local Compose E2E**

  Run:

  ```bash
  python3 scripts/e2e_golden_path.py
  ```

  Expected: exit 0 with `Compose golden path passed`.

  If the first run fails, record the first decisive failure in
  `docs/verification-matrix.md`, fix only owned slice defects, and rerun.

- [ ] **Step 4: Update verification matrix**

  Append a `2026-06-25: Hook Event Coverage` section with exact commands,
  exit codes, and first decisive failures:

  - CLI tests;
  - backend hook tests;
  - Codex plugin contract tests;
  - repository tests;
  - backend full tests;
  - Compose migration command;
  - local Compose golden path;
  - `git diff --check HEAD`.

- [ ] **Step 5: Verify docs and commit**

  Run:

  ```bash
  python3 scripts/repository_quality.py
  git diff --check HEAD
  ```

  Expected: both commands exit 0.

  Commit:

  ```bash
  git add docs/parity/claude-mem-parity-map.md docs/verification-matrix.md deploy/compose/README.md
  git commit -m "chore: record hook event coverage evidence"
  ```

### Task 6: Final Verification, Security Review, And PR

**Files:**

- No new files unless verification finds a defect.

**Interfaces:**

- Consumes: all prior tasks.
- Produces: reviewed PR ready for merge.

- [ ] **Step 1: Run full local verification**

  Run:

  ```bash
  PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
  python3 -m compileall packages/cli/engram_cli
  python3 scripts/repository_layout.py
  python3 scripts/repository_quality.py
  python3 -m unittest discover -s tests -v
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py -v"
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check ."
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff format --check ."
  docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"
  python3 scripts/e2e_golden_path.py
  git diff --check HEAD
  ```

  Expected: all commands exit 0.

- [ ] **Step 2: Dispatch independent reviews**

  Request read-only reviews for:

  - backend hook event correctness and idempotency;
  - CLI/Codex response and manifest correctness;
  - focused security review for hook input, local credentials, response
    redaction, and cross-scope denial;
  - whole-diff parity gate review.

- [ ] **Step 3: Fix or document findings**

  Critical and important findings must be fixed and re-reviewed. Security
  accepted risks require owner/date in `docs/verification-matrix.md` or a
  focused `docs/security/reviews/` artifact.

- [ ] **Step 4: Open PR and wait for CI**

  Push branch and open a draft PR:

  ```bash
  git push -u origin feat/parity-14-hook-event-coverage
  gh pr create --draft --base master --head feat/parity-14-hook-event-coverage --title "feat: add hook event coverage" --body-file .superpowers/sdd/parity-14-pr-body.md
  gh pr checks <number> --watch
  ```

  Promote PR only after Backend, Repository Quality, and Compose E2E checks are
  green.
