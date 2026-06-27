# Engram Plugin Repository

This directory is the distribution index for the Engram agent plugins. It hosts
the Claude Code plugin marketplace manifest and documents how the
`packages/claude-plugin` and `packages/codex-plugin` packages are published,
versioned, and installed. It owns no runtime code.

## Role in distribution

Engram ships two thin agent adapters that wire AI coding agents into Engram
memory by registering hook events:

- `packages/claude-plugin` - Claude Code plugin (`.claude-plugin/plugin.json`).
- `packages/codex-plugin` - Codex plugin (`.codex-plugin/plugin.json`).

This repository is the install source that agent marketplaces point at. A user
runs `engram connect` to materialize local credentials under `~/.engram`, then
installs the plugin from this repository so the agent's hook events call back
into `engram hook <event> --agent <runtime>`.

## Marketplace manifest format

Claude Code plugin marketplaces are described by a
`.claude-plugin/marketplace.json` manifest. The manifest is a JSON object with
two top-level fields:

- `name` - marketplace identifier (string).
- `owner` - human-readable owner name (object or string).
- `plugins` - array of plugin entries that this marketplace distributes.

Each entry in the `plugins` array has this shape:

```json
{
  "name": "<plugin-name>",
  "source": "./<path-to-plugin-package>",
  "description": "<short description>",
  "version": "<semver>",
  "category": "<category>",
  "metadata": {
    "author": "<author>",
    "repository": "<repo-url>",
    "license": "<license-id>"
  }
}
```

A concrete example covering both Engram plugins:

```json
{
  "name": "engram-marketplace",
  "owner": {
    "name": "Engram"
  },
  "plugins": [
    {
      "name": "engram",
      "source": "../packages/claude-plugin",
      "description": "Thin Engram hook adapter for Claude Code.",
      "version": "0.1.0",
      "category": "Productivity",
      "metadata": {
        "author": "Engram",
        "repository": "<repo-url>",
        "license": "MIT"
      }
    },
    {
      "name": "engram-codex",
      "source": "../packages/codex-plugin",
      "description": "Thin Engram hook adapter for Codex.",
      "version": "0.1.0",
      "category": "Productivity",
      "metadata": {
        "author": "Engram",
        "repository": "<repo-url>",
        "license": "MIT"
      }
    }
  ]
}
```

Field reference:

| Field                  | Type   | Notes                                                              |
| ---------------------- | ------ | ------------------------------------------------------------------ |
| `name`                 | string | Marketplace identifier.                                            |
| `owner`                | object | `{"name": "..."}`.                                                 |
| `plugins[].name`       | string | Plugin install name (`claude plugin install <name>`).              |
| `plugins[].source`     | string | Relative path or URL to the plugin package directory.              |
| `plugins[].description`| string | One-line description shown in marketplace listings.               |
| `plugins[].version`    | string | Semver version; must match the plugin's own `plugin.json`.         |
| `plugins[].category`   | string | Marketplace category, e.g. `Productivity`.                         |
| `plugins[].metadata`   | object | Optional `author`, `repository`, `license`.                        |

## Adding and versioning plugins

To add a new plugin to the marketplace:

1. Author the plugin package with its own `<agent>-plugin/plugin.json` and
   hook manifest (see `packages/claude-plugin` and `packages/codex-plugin` for
   reference).
2. Add an entry to the `plugins` array in
   `.claude-plugin/marketplace.json`. Set `source` to the relative path of the
   package directory.
3. Keep the entry's `version` field in lockstep with the plugin's own
   `plugin.json` `version` field.

To publish a new version of an existing plugin:

1. Bump `version` in the plugin's own `plugin.json`
   (`packages/<plugin>/.<agent>-plugin/plugin.json`).
2. Bump the matching `version` in
   `.claude-plugin/marketplace.json`.
3. Tag the release in git so installs can pin to a specific revision.

Both fields must match. Mismatches cause the marketplace to advertise a
different version than the plugin actually ships, which breaks pinning and
updates.

## Install flow

End-to-end, installing an Engram plugin is:

1. `engram connect` - interactive wizard that writes `~/.engram/config.json`,
   `~/.engram/credentials.json`, and `~/.engram/hooks.<runtime>.json` for each
   selected runtime. Required before any hook can fire.
2. `engram mcp install --runtime <runtime>` - (optional) registers the Engram
   MCP server with the agent for inline memory queries.
3. Install the plugin from this marketplace:

   ```bash
   # Claude Code
   claude plugin install engram@engram-marketplace
   # Codex
   codex plugin install engram-codex@engram-marketplace
   ```

Once installed, the agent's `SessionStart`, `PostToolUse`, `Error`, and
`Decision` hook events call
`engram hook <event> --agent <runtime> --response-format <format>`, which
forwards each event to the Engram server. See each plugin's README for the
exact hook table:

- `packages/claude-plugin/README.md`
- `packages/codex-plugin/README.md`

## Scope and non-goals

This repository is a docs-and-manifest distribution index only. It must not
ship installer automation, signing material, generated packages, or release
artifacts beyond the marketplace manifest documented above. CI/release
automation is out of scope for the current checkpoint.
