# Plug-and-Play Install — Claude Code (Slice 1)

Date: 2026-07-02
Status: Approved (design), implementation in progress
Branch: `feat/plug-and-play-claude-code-install`

## Goal

Make connecting a Claude Code harness to a running Engram backend feel like
claude-mem: one command, "just paste the API key". Concretely:

1. A developer clicks **Connect agent** in the admin dashboard, which issues a
   narrowly-scoped API key and renders a ready-to-paste install command.
2. The developer runs that command; it installs the Engram Claude Code plugin
   into the harness, wires hooks, writes local credentials, and verifies health.
3. The next Claude Code session receives context bundles and sends observations
   out of the box.

Backend bootstrap-from-zero (creating org/project/first key on a fresh backend)
is explicitly **out of scope** — an operator already has a backend + can issue
keys via the console.

## Decisions (locked)

- **Client shape:** keep the existing pure-stdlib Python `engram` CLI, but
  **bundle the hook runtime inside the plugin** so hooks call
  `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py" ...` — no separate `engram`
  on PATH is required for the hot path. PyPI is used only to launch the
  one-time installer (`uvx --from engram-connect engram install ...`).
- **Harness scope:** Claude Code first. Codex is a follow-up slice using the
  same template.
- **Install mechanism:** native `claude plugin marketplace add` +
  `claude plugin install`. The bundled hook resolves via `${CLAUDE_PLUGIN_ROOT}`.
- **Frontend:** wire the existing dashboard **Connect agent** button
  (`apps/frontend/src/app/(admin)/page.tsx:469`, currently
  `router.push('/api-keys')`) to open a modal that issues a scoped key and
  renders the command. No new route, no new backend endpoint.

## Components

### A. Root marketplace manifest — `.claude-plugin/marketplace.json`

New file at repo root (this is where `claude plugin marketplace add <owner/repo>`
looks). Shape:

```json
{
  "name": "engram-marketplace",
  "owner": { "name": "Engram" },
  "metadata": { "description": "Engram agent memory plugins" },
  "plugins": [
    {
      "name": "engram",
      "source": "./packages/claude-plugin",
      "description": "Thin Engram hook adapter for Claude Code.",
      "version": "0.1.0",
      "category": "Productivity",
      "metadata": { "author": "Engram", "license": "MIT" }
    }
  ]
}
```

`plugins[].version` MUST equal `packages/claude-plugin/.claude-plugin/plugin.json`
`version`. `plugin-repository/README.md` is updated to point at this real file as
the canonical manifest.

### B. Bundle the CLI hook runtime into the plugin

Under `packages/claude-plugin/hooks/`:

- `hook.py` — thin shim:
  ```python
  import os, sys
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  from engram_cli.main import main
  raise SystemExit(main())
  ```
- `engram_cli/` — bundled copy of the runtime modules from
  `packages/cli/engram_cli/` (`__init__.py`, `__main__.py`, `main.py`,
  `commands.py`, `config.py`, `http.py`). Test files (`*_tests.py`) are excluded.
- `hooks.json` — rewrite every command from `engram hook <event> ...` to
  `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py" hook <event> --agent claude_code --response-format claude-code`.
  Matchers/timeouts unchanged.

Sync + drift guard:

- `scripts/sync_plugin_bundle.py` — copies the runtime module set from
  `packages/cli/engram_cli/` into `packages/claude-plugin/hooks/engram_cli/`
  (excluding `*_tests.py`), deterministically and idempotently. This is the
  single source of truth for the committed bundle.
- `packages/claude-plugin/bundle_sync_tests.py` — asserts (1) every bundled
  runtime file byte-matches its `packages/cli/engram_cli/` source, (2) no extra
  files in the bundle, (3) `plugin.json` version == root `marketplace.json`
  version. Prevents silent drift.

Requirement documented in the plugin README: `python3 >= 3.12` on PATH for the
hook to run (not `engram`).

### C. New CLI command — `engram install`

`packages/cli/engram_cli/commands.py` gains `run_install`; `main.py` registers
the `install` subparser. `connect` stays as-is (credentials-only path).

Args: `--server`, `--api-key`, `--project`, `--team`, `--agent`
(default `claude-code` for this slice), `--agent-version`, `--config-dir`,
plus install-specific: `--marketplace-source` (default `Barsoomx/engram`),
`--marketplace-name` (default `engram-marketplace`), `--plugin-name`
(default `engram`), `--claude-bin` (default `claude`), `--skip-plugin-install`.

Flow (`run_install`):

1. **Connect first (fail fast):** reuse the existing connect flags path —
   `normalize_server_url`, `require_dry_run_ok`, `write_local_state`,
   `credential_fingerprint`. This validates server + key + project scope and
   writes `~/.engram/{config,credentials,hooks.*}.json` before touching the
   harness. On failure → exit 1 with existing `CliError` taxonomy.
2. **Preflight harness:** unless `--skip-plugin-install`, resolve `--claude-bin`
   on PATH (`shutil.which`). Missing → `CliError('claude_cli_not_found', ...)`.
3. **Install plugin:** via an injected `Runner` (default wraps `subprocess.run`,
   `run(cmd) -> (returncode, stdout, stderr)`):
   `claude plugin marketplace add <marketplace-source>` then
   `claude plugin install <plugin-name>@<marketplace-name>`. Non-zero exit →
   `CliError('plugin_install_failed', <redacted stderr>, ...)`. Idempotent
   (re-add/update tolerated).
4. **Doctor:** reuse `run_doctor` logic to verify config, credentials, hook
   manifests, server health, and dry-run. Surface a compact summary.

New error codes added to `ERROR_REMEDIATION`: `claude_cli_not_found`,
`plugin_install_failed`, `python_runtime_missing` (best-effort `python3 >=3.12`
check, warn-only).

Testing (TDD, unittest, `packages/cli/engram_cli/install_tests.py`):

- happy path: stub `Transport` (dry-run ok, health ok) + stub `Runner` records
  the two `claude plugin ...` calls in order; asserts `~/.engram` written; exit 0.
- `claude` missing → exit 1, `claude_cli_not_found`, remediation printed.
- plugin install non-zero → exit 1, `plugin_install_failed`, stderr redacted.
- bad key (dry-run 401) → exit 1 at connect step, `Runner` never called.
- `--skip-plugin-install` → `Runner` not called; connect + doctor only; exit 0.
- idempotent second run → exit 0.

### D. PyPI packaging for `engram-connect`

`packages/cli/pyproject.toml` gains publish metadata: `readme`, `license`,
`authors`, `urls` (Homepage/Repository), classifiers, keywords. Entry point
`engram = engram_cli.main:console_main` already exists. `packages/cli/README.md`
documents:

```bash
uvx --from engram-connect engram install --server <URL> --api-key <KEY> --project <ID>
# or, before PyPI publish:
uvx --from "git+https://github.com/Barsoomx/engram.git#subdirectory=packages/cli" \
  engram install --server <URL> --api-key <KEY> --project <ID>
# or: pipx install engram-connect
```

The actual PyPI publish (build + twine upload) is a credentialed **release
action** performed by the maintainer; it is documented, not executed here. A
`git+`/`pipx`/local fallback keeps the flow usable pre-publish.

### G. Frontend — activate the dashboard "Connect agent" button

- `apps/frontend/src/lib/build-connect-command.ts` — pure helper:
  `buildConnectCommand({ serverUrl, apiKey, projectId }) -> string` producing the
  `uvx --from engram-connect engram install --server ... --api-key ... --project ...`
  line. Isolated so it is trivially correct and unit-testable later.
- `apps/frontend/src/components/connect/connect-agent-modal.tsx`:
  - Project selector (default = active project from `useProjectStore`, options
    from `listProjects`).
  - Editable **Server URL** field, prefilled from
    `process.env.NEXT_PUBLIC_ENGRAM_API_URL` (fallback `window.location.origin`).
    Editable because the console's own API base may be an internal Compose URL.
  - **Generate command** → `useIssueApiKey` with capabilities
    `['memories:read','observations:write','search:query']` and name
    `claude-code · <project-slug>`.
  - On success, render the command via `buildConnectCommand` with copy-to-clipboard
    and the same "shown once" warning as `IssueModal`; a collapsible fallback shows
    `claude plugin install engram@engram-marketplace` + `engram connect ...`.
- `apps/frontend/src/app/(admin)/page.tsx` — the existing button opens this modal
  instead of navigating. If the viewer lacks `api_keys:issue`, keep the current
  `router.push('/api-keys')` behavior (graceful degradation).

Reuses `useIssueApiKey`, the plaintext-once pattern, `CapabilityGate`/`hasCapability`,
project store. No backend change.

Frontend verification: `pnpm typecheck` + `pnpm lint` (repo has no JS test runner;
introducing one is out of scope for this slice). Command-building logic lives in the
pure helper to keep correctness inspectable.

## Data flow (the "paste key" path)

```
Dashboard "Connect agent" → ConnectAgentModal
  → POST /v1/admin/api-keys/ (scoped: memories:read, observations:write, search:query)
  → render: uvx --from engram-connect engram install --server S --api-key K --project P
Developer runs command →
  engram install:
    connect(S,K,P)  → validate (dry-run) + write ~/.engram/{config,credentials,hooks}
    claude plugin marketplace add Barsoomx/engram
    claude plugin install engram@engram-marketplace   (bundles hook.py + engram_cli)
    doctor          → health + hook dry-run OK
Next Claude Code session:
    SessionStart → python3 ${CLAUDE_PLUGIN_ROOT}/hooks/hook.py hook session-start ...
      → reads ~/.engram → POSTs server → context injected
```

## Boundaries / non-goals

- Codex harness — next slice (same template).
- Backend bootstrap-from-zero — out of scope.
- MCP install — unchanged.
- Hosted `install.sh`/Vercel distribution — later; entry point is `uvx`/`pipx`.
- Actual PyPI publish — maintainer release action, documented not executed.

## Verification matrix

| Area | Command | Where |
|------|---------|-------|
| CLI unit (install + existing) | `python -m unittest discover -s packages/cli -p '*_tests.py'` | docker `python:3.12` |
| Plugin bundle drift | `python -m unittest discover -s packages/claude-plugin -p '*_tests.py'` | docker `python:3.12` |
| Claude plugin contract | existing `claude_plugin_contract_tests.py` | docker `python:3.12` |
| Frontend types | `pnpm typecheck` | host |
| Frontend lint | `pnpm lint` | host |

Python runs in docker only (host-python is disallowed). One git owner; feature
branch merged into master; no force-push; report `.md` files at repo root are
not committed.
