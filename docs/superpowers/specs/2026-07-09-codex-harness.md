# Native Codex harness

Operator request 2026-07-09: add full Codex harness support around the
already-working Engram connector. This checkpoint makes the existing Engram
memory loop installable through current Codex plugin surfaces. It does not
change backend domain models, API endpoints, retrieval, or worker behavior.

Start state:

- branch base: `2f6b77e6464adf04269059a57565f53598c5972c`;
- validated runtime: `codex-cli 0.144.0`;
- current Codex package is a four-file contract stub, not an installable
  harness;
- current Codex contract tests are false-green because they require hook names
  that Codex does not emit.

## User-visible behavior

After connecting Engram, a developer can install the native plugin either with
the combined command:

```bash
uvx engram-connect install --agent codex --server URL --api-key KEY --project PROJECT
```

or with the native Codex marketplace flow:

```bash
codex plugin marketplace add Barsoomx/engram --json
codex plugin add engram@engram-marketplace --json
```

The installed plugin:

- loads cited Engram context on `SessionStart`;
- submits the prompt and injects focused context on `UserPromptSubmit`;
- captures supported tool inputs, outputs, and command failures on
  `PostToolUse`;
- treats Codex `Stop` as an Engram turn-completion checkpoint by calling the
  existing `session-end` connector command;
- exposes the six existing Engram MCP tools without a separate MCP install;
- ships the same runtime-neutral memory skills as the Claude Code plugin;
- uses the existing `~/.engram` connection and credential files and stores no
  provider secrets, memory database, vector index, or durable event queue.

Codex requires users to review changed non-managed hooks. Installation output
and docs must tell the user to open `/hooks`, review Engram, and start a new
thread. The harness must not bypass hook trust.

## Native lifecycle mapping

The package uses only current Codex hook events that advance the Engram memory
loop:

| Codex event | Engram connector command | Notes |
| --- | --- | --- |
| `SessionStart` | `hook session-start` | matcher `startup|resume|clear|compact`; returns Codex developer context |
| `UserPromptSubmit` | `hook user-prompt-submit` | no matcher because Codex ignores it for this event |
| `PostToolUse` | `hook post-tool-use` | captures Bash, `apply_patch`, and MCP results supported by Codex |
| `Stop` | `hook session-end` | turn-scoped checkpoint; later activity reactivates the server session |

`Error`, `Decision`, and `SessionEnd` are not Codex hook events and must not
appear in the Codex hook manifest. Tool failures arrive in
`PostToolUse.tool_response`. There is no dedicated decision hook.

The connector must include native occurrence identifiers such as `turn_id` and
`tool_use_id` in stable event material. Repeating the same prompt or command in
one session must not collapse into a duplicate event, while replaying the same
Codex hook payload must remain idempotent.

The live connector's explicit hook HTTP timeout remains `10.0` seconds. The
general CLI timeout is 30 seconds, but hook ingest/context calls keep the
smaller bound so two-call lifecycle hooks fit their 60-second handler budget.

## Package and marketplace layout

The repository adds the current Codex-native layout:

```text
.agents/plugins/marketplace.json
packages/codex-plugin/
  .codex-plugin/plugin.json
  .mcp.json
  README.md
  hooks/
    hooks.json
    hook.py
    mcp.py
    engram_cli/
  skills/
    how-it-works/SKILL.md
    learn-codebase/SKILL.md
    mem-search/SKILL.md
```

`hooks/hooks.json` uses Codex's default discovery path, so `plugin.json` omits
the `hooks` field. This is accepted by current Codex and by the locally
installed plugin validator, whose schema still rejects the documented
manifest-level `hooks` key.

The manifest points only to components that exist, uses plugin-root-relative
`./` paths, and contains publisher and install-surface metadata required by the
validator. The MCP manifest launches the bundled Python bridge from the plugin
root. Because that process cwd is the plugin cache, each Codex `tools/call`
derives repository scope from the request's per-turn workspace metadata after
matching its outer and inner thread/session ids. Explicit project scope wins;
missing, mismatched, or ambiguous metadata fails closed and is never cached
across threads. The canonical connector modules remain under
`packages/cli/engram_cli`;
the plugin copy is byte-for-byte generated and checked for drift.

The Codex marketplace is separate from the Claude Code marketplace and points
at `./packages/codex-plugin`. It declares explicit installation and
authentication policy and does not contain credentials.

## Installer behavior

`engram install` dispatches by the normalized `--agent` selection:

- `claude-code`: preserve the current Claude marketplace/install commands;
- `codex`: require the Codex binary, add the marketplace with
  `codex plugin marketplace add ... --json`, then install with
  `codex plugin add engram@engram-marketplace --json`;
- `both`: require both binaries before mutating either plugin installation,
  then install both.

Command failures remain redacted. Successful Codex installation prints native
`codex plugin list --json`, `/hooks`, new-thread, and
`codex plugin remove engram@engram-marketplace --json` guidance.

Codex owns plugin installation and removal state. Engram `disconnect` continues
to remove only Engram-owned connection files. A separate Engram uninstall
wrapper and standalone `doctor` inspection of Codex's plugin cache are deferred;
native `codex plugin list` and the isolated E2E are the authority in this
checkpoint.

## Dashboard installer

The existing **Connect agent** modal remains a thin command generator. It adds
an explicit selector for `Claude Code`, `Codex`, or `Both` and emits one
shell-safe command using the corresponding `--agent` value. Claude Code remains
the default so the current flow does not change for existing users.

The modal also shows runtime-specific completion guidance:

- Claude Code uses its existing marketplace/install commands;
- Codex uses `plugin marketplace add`, `plugin add`, `/hooks` review, and a new
  thread;
- Both shows both sets without asking the user to reconnect or expose the key a
  second time.

The UI does not install a local worker, retain the displayed API key, bypass
agent hook trust, or invent a second installer protocol.

## Verification

TDD contract coverage must prove:

- the manifest, marketplace, hooks, bundled runtime, MCP bridge, and skills are
  present and internally consistent;
- the only Codex hooks are the four mappings above and their response fields
  are valid for those events;
- native `turn_id`/`tool_use_id` participate in event identity;
- `engram install --agent codex` never invokes Claude, and `--agent both`
  resolves both binaries before plugin mutation;
- secrets do not appear in manifests, commands, command errors, or MCP config;
- Codex MCP calls route by the caller repository rather than the plugin cache,
  while ambiguous or spoofed scope metadata fails closed;
- the generated Codex bundle byte-matches the canonical connector modules.

One isolated real-Codex E2E uses temporary `HOME` and `CODEX_HOME`, a local
stub Engram HTTP server, a local deterministic OpenAI-compatible model server,
and the pinned supported Codex CLI to:

1. add the repository marketplace;
2. list and install Engram;
3. start a real Codex thread and submit a prompt whose deterministic model
   response makes Codex run a harmless tool;
4. prove Codex itself emitted `SessionStart`, `UserPromptSubmit`, `PostToolUse`,
   and `Stop` to the stub Engram server, with no direct hook invocation used as
   the ground-truth assertion;
5. initialize the bundled MCP server and list all six tools;
6. remove the plugin with Codex;
7. prove no files under the developer's real Codex profile changed.

CI runs the same E2E with a pinned Codex version, plus CLI/package contract and
bundle-drift tests. The Codex E2E is a required PR check, matching the Claude
Code baseline rather than an optional smoke job.

The classifier-backed security review is operator-deferred as of 2026-07-09.
This checkpoint retains functional negative tests for credential redaction,
hook trust, marketplace isolation, and fail-closed MCP routing, but does not
claim that the formal security-review gate completed.

## Deferred

- `PreToolUse`, `PermissionRequest`, compaction, and subagent hooks: no current
  Engram behavior requires them for the core loop.
- a standalone `engram doctor` plugin-cache/trust parser;
- an Engram-owned plugin uninstall wrapper;
- provider, backend, retrieval, worker, and memory-quality changes beyond the
  deterministic local E2E stub;
- the unrelated pgvector, trigram, curation near-duplicate, and search-debug
  performance backlog.

## References

- [Codex plugin structure](https://developers.openai.com/codex/plugins/build#plugin-structure)
- [Codex bundled MCP and hooks](https://developers.openai.com/codex/plugins/build#bundled-mcp-servers-and-lifecycle-hooks)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Codex marketplace metadata](https://developers.openai.com/codex/plugins/build#marketplace-metadata)
