# CLI

Owns the first `engram` command-line client for connecting a developer machine
to an already-running Engram server.

Current command surface:

```bash
python -m engram_cli connect --server URL --api-key KEY --project PROJECT
python -m engram_cli doctor
python -m engram_cli hook post-tool-use < hook.json
python -m engram_cli hook session-start < context.json
python -m engram_cli hook error < hook.json
python -m engram_cli hook decision < hook.json
python -m engram_cli hook session-start --response-format codex < hook.json
python -m engram_cli disconnect
```

The package declares two console scripts in `pyproject.toml` that share the same
entrypoint: `engram` (short, for typed commands) and `engram-connect` (matches the
distribution name, so `uvx engram-connect ...` works without `--from`).

## Plug-and-play install (`engram install`)

`engram install` is the one-command onboarding path for Claude Code, Codex, or
both.
It validates the server, API key, and project scope (same dry-run as `connect`),
writes local credentials under `~/.engram`, installs the selected native
plugin marketplace package, and finishes with a `doctor` health check.

Run it without cloning the repo via `uvx`:

```bash
uvx engram-connect install --agent both \
  --server <URL> --api-key <KEY>
```

Before the package is published to PyPI, install straight from git or `pipx`:

```bash
uvx --from "git+https://github.com/Barsoomx/engram.git#subdirectory=packages/cli" \
  engram install --agent codex --server <URL> --api-key <KEY>

# or:
pipx install engram-connect
engram install --agent codex --server <URL> --api-key <KEY>
```

`--agent` accepts `claude-code`, `codex`, or `both`. Claude Code uses
`claude plugin marketplace add` plus `claude plugin install`; Codex uses
`codex plugin marketplace add --json` plus `codex plugin add --json`. Override
the binaries with `--claude-bin` and `--codex-bin`. Pass
`--skip-plugin-install` to write credentials and run `doctor` without touching
either harness. The bundled plugin hooks require `python3 >= 3.12` on PATH.

Codex users must review the Engram commands in `/hooks` and start a new thread.
The installer does not bypass native hook trust.

Local state defaults to `$ENGRAM_HOME` when set, otherwise `~/.engram`.
`connect` writes only Engram-owned config, credential, and hook-manifest files.
`doctor` is read-only. `disconnect` removes only Engram-owned files.
Hook commands read one JSON object from stdin, merge the connected project,
team, runtime, and credential metadata, and call the Engram server. By default
they print the server JSON response. Codex response mode prints the Codex hook
response instead.

The CLI must not introduce a persistent local memory service, local database,
provider secrets, embeddings, cached memory bundles, durable event queue, or
background summarization behavior.

Run tests from the repository root inside the backend container:

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro \
  -v /usr/lib/git-core:/usr/lib/git-core:ro \
  -e PYTHONPATH=/workspace/packages/cli --entrypoint python3 api \
  -m unittest discover -s packages/cli -p '*_tests.py' -v
```
