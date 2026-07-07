---
name: mem-search
description: Search Engram's shared engineering memory for prior work, decisions, and gotchas. Use BEFORE starting any non-trivial task (bug fix, feature, refactor) to check whether the team already solved something similar, and when the user asks "did we solve this before?" or "how did we do X last time?".
---

# Search Engram memory

Before diving into a task, check whether Engram already holds relevant engineering
memory for this project, so you build on prior work instead of repeating it.

## How to search

Prefer the `engram_search` MCP tool — call it directly with a short query. Fall
back to the `engram` CLI (configured by `engram install` / `engram connect`)
only when the MCP tool is unavailable:

```bash
engram search --query "<what you're looking for>" --limit 5
```

Bias the query toward the exact anchors Engram indexes well:

- file paths: `--file-path apps/backend/engram/hooks/services.py`
- symbols / identifiers: `--symbol resolve_or_create_project`
- error strings, commands, and ticket ids — put them directly in `--query`.

Add `--json` for machine-readable output when you need to parse results.

## Using results

Each hit includes a citation, title, and body. Treat memory as evidence, not
ground truth: verify a cited file/symbol/flag still exists before relying on it,
and prefer recent, specific memories. Cite the memory you used in your reply so
the reasoning stays traceable.
