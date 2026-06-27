# MCP

Thin Model Context Protocol bridge over Engram server APIs. Agents (Claude
Code, Codex, and any MCP client) call Engram memory tools through stdio without
a local memory store, local embeddings, or provider secrets.

## Status

Active. The bridge exposes three tools:
- `engram_search` — `POST /v1/search/`
- `engram_context` — `POST /v1/context/session-start`
- `engram_memory_link` — `POST /v1/memories/<id>/links`

## Run

The bridge is a newline-delimited JSON-RPC server over stdio:

```bash
ENGRAM_SERVER_URL=https://engram.example \
ENGRAM_API_KEY=egk_... \
ENGRAM_PROJECT_ID=11111111-1111-1111-1111-111111111111 \
python -m engram_mcp
```

`ENGRAM_TEAM_ID` is optional. The server speaks MCP `initialize`,
`notifications/initialized`, `tools/list`, and `tools/call` (JSON-RPC 2.0,
newline-delimited).

## Tests

```bash
PYTHONPATH=packages/mcp python3 -m unittest discover -s packages/mcp -p '*_tests.py' -v
```

## Boundaries

This bridge never owns memory, retrieval, embeddings, or provider secrets. It
only forwards authorized calls to the Engram server using the configured API
key. No local authoritative state is introduced.
