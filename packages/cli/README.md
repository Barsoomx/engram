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

`engram install` is the one-command onboarding path for a Claude Code harness.
It validates the server, API key, and project scope (same dry-run as `connect`),
writes local credentials under `~/.engram`, installs the Engram Claude Code
plugin through the native `claude plugin` marketplace, and finishes with a
`doctor` health check.

Run it without cloning the repo via `uvx`:

```bash
uvx engram-connect install \
  --server <URL> --api-key <KEY>```

Before the package is published to PyPI, install straight from git or `pipx`:

```bash
uvx --from "git+https://github.com/Barsoomx/engram.git#subdirectory=packages/cli" \
  engram install --server <URL> --api-key <KEY># or:
pipx install engram-connect
engram install --server <URL> --api-key <KEY>```

The plugin steps run `claude plugin marketplace add <--marketplace-source>` then
`claude plugin install <--plugin-name>@<--marketplace-name>` using the `claude`
binary on PATH (override with `--claude-bin`). Pass `--skip-plugin-install` to
write credentials and run `doctor` without touching the harness. The bundled
plugin hooks require `python3 >= 3.12` on PATH.

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

Run tests from the repository root:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```
