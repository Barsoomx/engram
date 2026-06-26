# Claude Code Plugin

Active native Claude Code plugin package for Engram hook capture.

The package exposes the currently implemented Engram hook events:

- `SessionStart`;
- `PostToolUse`;
- `Error`;
- `Decision`.

The hook manifest uses `engram hook` as a thin client with
`--agent claude_code` and `--response-format claude-code`. This package must not
introduce a local worker, local database, provider secret storage, or product
naming outside agent-integration boundaries.
