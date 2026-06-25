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

The package also declares an `engram` console script in `pyproject.toml`.

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
