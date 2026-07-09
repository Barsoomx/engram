# Engram Plugin Distribution

Engram ships separate native packages and marketplace manifests for Claude Code
and Codex. This directory documents distribution only; runtime code stays in
`packages/`.

| Runtime | Plugin package | Marketplace manifest |
| --- | --- | --- |
| Claude Code | `packages/claude-plugin` | `.claude-plugin/marketplace.json` |
| Codex | `packages/codex-plugin` | `.agents/plugins/marketplace.json` |

Both plugins bundle the same thin Python connector and MCP bridge. They require
`python3 >= 3.12` on `PATH`, but do not require a separate `engram` executable
for the hook or MCP hot path. They read the connection created under
`~/.engram`; neither package contains credentials, a memory database, provider
secrets, a local worker, or a vector index.

## Golden install flow

The supported one-step command connects Engram and installs the selected native
plugin:

```bash
uvx engram-connect install --agent both \
  --server URL --api-key KEY --project PROJECT
```

`--agent` accepts `claude-code`, `codex`, or `both`. Direct native installation
is also supported:

```bash
# Claude Code
claude plugin marketplace add Barsoomx/engram
claude plugin install engram@engram-marketplace

# Codex
codex plugin marketplace add Barsoomx/engram --json
codex plugin add engram@engram-marketplace --json
```

After a Codex install, open `/hooks`, review the Engram commands, approve them
if they match the installed package, and start a new thread. The installer does
not bypass Codex hook trust.

## Package contracts

The Claude Code package uses its `.claude-plugin/plugin.json`, `.mcp.json`, and
`hooks/hooks.json` contracts. Its hooks invoke:

```text
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py" hook <event> \
  --agent claude_code --response-format claude-code
```

The Codex package uses `.codex-plugin/plugin.json`, `.mcp.json`, and Codex's
default `hooks/hooks.json` discovery path. Its hooks invoke:

```text
python3 "$PLUGIN_ROOT/hooks/hook.py" hook <event> \
  --agent codex --response-format codex
```

Codex maps `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop`; `Stop`
calls the existing Engram `session-end` adapter as a turn checkpoint. Codex has
no native `Error`, `Decision`, or `SessionEnd` hook.

The bundled `hooks/engram_cli/` directories are generated from
`packages/cli/engram_cli/` by `scripts/sync_plugin_bundle.py`. Contract tests
reject missing files, extra files, or byte drift.

## Publishing and versioning

For Claude Code, keep the package version and its entry in
`.claude-plugin/marketplace.json` in lockstep. For Codex, bump
`packages/codex-plugin/.codex-plugin/plugin.json`; the Codex marketplace points
at the local package and reads package metadata from that manifest.

Before publishing:

1. sync both generated connector bundles;
2. validate both plugin manifests and marketplace files;
3. run CLI, package-contract, MCP, and real-agent E2E tests in containers;
4. run the focused plugin/install security review;
5. tag the reviewed commit so installations can pin a known revision.

Codex removal is owned by Codex:

```bash
codex plugin remove engram@engram-marketplace --json
```

See `packages/claude-plugin/README.md` and `packages/codex-plugin/README.md` for
the runtime-specific contracts.
