# CLI

Owns the first `engram` command-line client for connecting a developer machine
to an already-running Engram server.

Current command surface:

```bash
python -m engram_cli connect --server URL --api-key KEY --project PROJECT
python -m engram_cli doctor
python -m engram_cli disconnect
```

The package also declares an `engram` console script in `pyproject.toml`.

Local state defaults to `$ENGRAM_HOME` when set, otherwise `~/.engram`.
`connect` writes only Engram-owned config, credential, and hook-manifest files.
`doctor` is read-only. `disconnect` removes only Engram-owned files.

The CLI must not introduce a persistent local memory service, local database,
provider secrets, embeddings, cached memory bundles, durable event queue, or
background summarization behavior.

Run tests from the repository root:

```bash
PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v
```
