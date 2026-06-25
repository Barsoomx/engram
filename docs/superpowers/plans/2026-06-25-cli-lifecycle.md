# CLI Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first dependency-free `engram` CLI lifecycle commands:
`connect`, `doctor`, and `disconnect`.

**Architecture:** Implement a Python stdlib package under `packages/cli`.
Commands call existing Engram health and hook dry-run APIs, then manage only
Engram-owned local config, credential, and hook-manifest files.

**Tech Stack:** Python 3.12, stdlib `argparse`, `urllib`, `json`, `unittest`,
GitHub Actions.

## Global Constraints

- Work on branch `feat/parity-11-cli-lifecycle`.
- Keep the pre-existing unstaged `.gitignore` edit out of every commit.
- Use single quotes in Python files.
- Use TDD: write failing tests before production code.
- Use the command name `engram` and module invocation `python -m engram_cli`.
- Do not add local workers, local databases, embeddings, cached memory bundles,
  provider secrets, durable event queues, MCP install, native hook settings
  edits, server token minting, package publishing, or Docker E2E fixtures.
- `doctor` must be read-only and exit nonzero when required checks fail.
- Raw API keys and bearer tokens must not appear in normal output, error
  output, config JSON, hook manifests, or response diagnostics.
- Docker Compose live checks are recorded as blocked while Docker is
  unavailable in this WSL distro.

---

### Task 1: Planning Checkpoint

**Files:**

- Create: `docs/superpowers/specs/2026-06-25-cli-lifecycle-design.md`
- Create: `docs/superpowers/plans/2026-06-25-cli-lifecycle.md`

**Interfaces:**

- Consumes: `goal.md`, `docs/north-star.md`, `docs/v1-scope.md`,
  `docs/product-requirements.md`, `docs/architecture.md`,
  `docs/backend-contracts.md`, `docs/agent-integrations.md`,
  `docs/client-installation.md`, `docs/rbac-and-scopes.md`,
  `docs/secrets-and-model-config.md`, `docs/parity/claude-mem-parity-map.md`,
  and existing hook dry-run API code.
- Produces: committed design and implementation plan.

- [ ] **Step 1: Write design and plan**

Document command contracts, local state, dry-run/health behavior, failure
taxonomy, explicit deferrals, tests, and verification.

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
git add docs/superpowers/specs/2026-06-25-cli-lifecycle-design.md docs/superpowers/plans/2026-06-25-cli-lifecycle.md
git commit -m "chore: add cli lifecycle plan"
```

### Task 2: Failing CLI Lifecycle Tests

**Files:**

- Create: `packages/cli/engram_cli/cli_lifecycle_tests.py`

**Interfaces:**

- Consumes planned public interfaces:
  - `engram_cli.commands.run_connect(args, stdout, stderr, transport) -> int`
  - `engram_cli.commands.run_doctor(args, stdout, stderr, transport) -> int`
  - `engram_cli.commands.run_disconnect(args, stdout, stderr) -> int`
  - `engram_cli.main.main(argv, stdout=None, stderr=None, transport=None) -> int`
- Produces failing tests for the CLI package.

- [ ] **Step 1: Add fake transport and temp config helpers**

Create a `FakeTransport` test helper with queued responses:

```python
class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append(
            {
                'method': method,
                'url': url,
                'headers': headers,
                'payload': payload,
                'timeout': timeout,
            },
        )

        return self.responses.pop(0)
```

Add helpers:

```python
def dry_run_ok(project_id: str) -> dict[str, object]:
    return {
        'status': 'ok',
        'request_id': 'request-1',
        'resolved_actor': {'type': 'api_key', 'id': 'api-key-1'},
        'scope': {
            'organization_id': 'org-1',
            'project_ids': [project_id],
            'team_ids': ['team-1'],
            'capabilities': ['observations:write', 'memories:read'],
        },
        'server': {'health': 'ok'},
    }
```

- [ ] **Step 2: Add connect success test**

Add `test_connect_verifies_dry_run_then_writes_redacted_local_state`.

Call:

```python
exit_code = main.main(
    [
        'connect',
        '--server',
        'https://engram.example/',
        '--api-key',
        RAW_KEY,
        '--project',
        PROJECT_ID,
        '--team',
        TEAM_ID,
        '--config-dir',
        str(config_dir),
    ],
    stdout=stdout,
    stderr=stderr,
    transport=transport,
)
```

Assert:

- exit code is `0`;
- two dry-run calls were made, one for `codex` and one for `claude_code`;
- Authorization header is `Bearer {RAW_KEY}` in transport calls;
- `config.json`, `credentials.json`, `hooks/codex.json`, and
  `hooks/claude_code.json` exist;
- `config.json` and hook manifests do not contain `RAW_KEY`;
- `credentials.json` contains `RAW_KEY` and has mode `0o600`;
- stdout contains `connected`, selected runtimes, project id, and credential
  fingerprint;
- stdout and stderr do not contain `RAW_KEY`.

- [ ] **Step 3: Add failed dry-run test**

Add `test_connect_writes_nothing_when_dry_run_fails`.

Fake a `403` response:

```python
(403, {'code': 'project_scope_denied', 'detail': 'API key cannot access requested project'})
```

Assert exit code `1`, stderr contains `project_scope_denied`, and no config,
credential, or hook files exist.

- [ ] **Step 4: Add doctor success and read-only tests**

Create a connected config by running `connect`, snapshot every file's bytes,
then run `doctor` with health and dry-run responses:

```python
transport = FakeTransport([
    (200, {'status': 'ok', 'checks': {'process': 'ok'}}),
    (200, dry_run_ok(PROJECT_ID)),
    (200, dry_run_ok(PROJECT_ID)),
])
```

Assert exit code `0`, stdout contains `All required checks passed`, and all
file bytes are unchanged.

- [ ] **Step 5: Add doctor failure tests**

Add tests for:

- missing config returns exit `1` and `missing_config`;
- missing credential returns exit `1` and `missing_credential`;
- missing hook manifest returns exit `1` and `missing_hook_config`;
- health response with status `unavailable` returns exit `1` and
  `server_unavailable`;
- dry-run response `401 invalid_key` returns exit `1` and `invalid_key`.

- [ ] **Step 6: Add disconnect tests**

Add `test_disconnect_removes_only_engram_owned_state_and_is_idempotent`.

Arrange connected files plus an unrelated `keep.txt` in the config directory.
Run disconnect twice. Assert exit codes `0`, Engram files are gone, `keep.txt`
remains, and the second run reports `nothing connected`.

- [ ] **Step 7: Add argument validation tests**

Use `main.main(...)` and assert exit `1` plus the expected error code for:

- missing `--server` -> `missing_server_url`;
- missing `--api-key` -> `missing_api_key`;
- missing `--project` -> `missing_project`.

- [ ] **Step 8: Run tests and verify first failure**

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

Expected before implementation: tests fail with missing `packages/cli/engram_cli`
package or missing CLI interfaces.

### Task 3: CLI Package And Command Implementation

**Files:**

- Create: `packages/cli/pyproject.toml`
- Create: `packages/cli/engram_cli/__init__.py`
- Create: `packages/cli/engram_cli/__main__.py`
- Create: `packages/cli/engram_cli/main.py`
- Create: `packages/cli/engram_cli/commands.py`
- Create: `packages/cli/engram_cli/config.py`
- Create: `packages/cli/engram_cli/http.py`
- Modify: `packages/cli/README.md`

**Interfaces:**

- Consumes: failing tests from Task 2.
- Produces:
  - command parser in `engram_cli.main`;
  - command runners in `engram_cli.commands`;
  - file helpers in `engram_cli.config`;
  - HTTP helpers in `engram_cli.http`.

- [ ] **Step 1: Create package metadata**

Create `packages/cli/pyproject.toml` with:

```toml
[project]
name = "engram-cli"
version = "0.1.0"
description = "Thin Engram command-line client"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
engram = "engram_cli.main:console_main"
```

- [ ] **Step 2: Create config helpers**

Implement:

```python
@dataclass(frozen=True)
class LocalPaths:
    root: Path
    config: Path
    credentials: Path
    hooks_dir: Path

    def hook_manifest(self, runtime: str) -> Path:
        return self.hooks_dir / f'{runtime}.json'
```

and helpers:

- `resolve_config_dir(value: str | None) -> Path`;
- `local_paths(config_dir: str | None) -> LocalPaths`;
- `read_json(path: Path) -> dict[str, object]`;
- `write_json(path: Path, payload: dict[str, object]) -> None`;
- `write_secret_json(path: Path, payload: dict[str, object]) -> None`;
- `remove_if_exists(path: Path) -> bool`;
- `credential_fingerprint(raw_key: str) -> str`.

`write_secret_json` must create parent directories, write JSON, and apply mode
`0o600`.

- [ ] **Step 3: Create HTTP helpers**

Implement:

```python
Transport = Callable[
    [str, str, dict[str, str], dict[str, object] | None, float],
    tuple[int, dict[str, object]],
]
```

`urllib_transport(...)` sends JSON requests and returns `(status, body)`. It
parses JSON error bodies when possible and maps network failures to:

```python
{'code': 'server_unavailable', 'detail': 'Server is unavailable'}
```

Implement `post_dry_run(...)` and `get_health(...)` wrappers.

- [ ] **Step 4: Create command runners**

Implement `run_connect`, `run_doctor`, and `run_disconnect`.

`run_connect`:

- validates required args with `missing_server_url`, `missing_api_key`, and
  `missing_project`;
- normalizes runtime values so `claude-code` becomes `claude_code` and `both`
  becomes `('codex', 'claude_code')`;
- calls dry-run before writing files;
- writes config, credentials, and hook manifests after all dry-runs pass;
- prints a redacted success report.

`run_doctor`:

- reads existing local state;
- checks config, credentials, hook manifests, health, and dry-run;
- prints each check as `ok` or `fail`;
- never writes files;
- returns `0` only when all required checks pass.

`run_disconnect`:

- removes only `config.json`, `credentials.json`, selected known hook manifests,
  and an empty hooks directory;
- returns `0` when already disconnected.

- [ ] **Step 5: Create parser and module entry point**

`main.main(argv, stdout, stderr, transport)` dispatches subcommands and returns
an integer exit code. `console_main()` raises `SystemExit(main())`.

`__main__.py` runs:

```python
from engram_cli.main import console_main

console_main()
```

- [ ] **Step 6: Update README**

Replace the inactive skeleton note with the actual first-slice command surface,
config directory, forbidden local state, and test command.

- [ ] **Step 7: Run focused tests**

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

Expected: all CLI tests pass.

- [ ] **Step 8: Commit**

Commit:

```bash
git add packages/cli
git commit -m "feat: add cli lifecycle commands"
```

### Task 4: CI, Layout, And Verification Matrix

**Files:**

- Modify: `.github/workflows/backend.yml`
- Modify: `.github/workflows/repository-quality.yml`
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_repository_layout.py`
- Modify: `tests/repository/test_backend_workflow.py`
- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: passing CLI package tests from Task 3.
- Produces: CI coverage and recorded local evidence for the CLI slice.

- [ ] **Step 1: Add failing workflow/layout tests**

Update repository tests to assert:

- required paths include `packages/cli/pyproject.toml`,
  `packages/cli/engram_cli/main.py`, and
  `packages/cli/engram_cli/cli_lifecycle_tests.py`;
- backend workflow text runs the CLI test command;
- repository quality workflow text runs the CLI test command.

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected before workflow/layout changes: tests fail because the CLI paths or
workflow commands are missing.

- [ ] **Step 2: Update repository layout contract**

Add the CLI package files to `REQUIRED_PATHS` in
`scripts/repository_layout.py`.

- [ ] **Step 3: Update workflows**

Add this step to both Backend and Repository Quality workflows:

```yaml
- name: Run CLI tests
  run: PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```

- [ ] **Step 4: Update verification matrix**

Append the `2026-06-25: CLI Lifecycle` section with branch, scope, required
commands, statuses, CI job names, and Docker unavailability note.

- [ ] **Step 5: Run focused verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

Commit:

```bash
git add .github/workflows/backend.yml .github/workflows/repository-quality.yml scripts/repository_layout.py tests/repository/test_repository_layout.py tests/repository/test_backend_workflow.py docs/verification-matrix.md
git commit -m "test: add cli lifecycle gates"
```

### Task 5: Final Verification And PR

**Files:**

- Verify only.

**Interfaces:**

- Consumes: all prior tasks.
- Produces: local evidence, review notes, and PR.

- [ ] **Step 1: Run full local verification**

Run:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
python3 -m compileall packages/cli/engram_cli
python3 scripts/repository_layout.py
python3 scripts/repository_quality.py
python3 -m unittest discover -s tests -v
cd apps/backend && poetry run pytest -v
cd apps/backend && poetry run ruff check .
cd apps/backend && poetry run ruff format --check .
cd apps/backend && poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings
cd apps/backend && poetry run python manage.py migrate --noinput --settings=settings.test_settings
cd apps/backend && poetry check
git diff --check HEAD
docker compose version
```

Expected: all commands exit 0 except Docker exits 1 in this WSL distro with
`The command 'docker' could not be found in this WSL 2 distro.`

- [ ] **Step 2: Self-review**

Review the branch against:

- no local worker/runtime/database;
- no provider secrets or memory bundles in local files;
- dry-run before writing connect state;
- `doctor` read-only behavior;
- `disconnect` only deletes Engram-owned files;
- raw API key absent from output, config, hook manifests, docs, and CI logs.

- [ ] **Step 3: Open PR**

Push branch and open a PR with summary, verification commands, Docker
unavailability, and known deferrals.

- [ ] **Step 4: Merge only after CI is green**

Check PR status. If Backend and Repository Quality pass, merge the PR with the
normal non-force flow and update local `master`.
