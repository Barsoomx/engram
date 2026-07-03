# Upstream Fork Boundary

## Preserve

The fork should reuse upstream ideas where they remain compatible with a
server-only product:

- observation capture from agent lifecycle hooks;
- session and tool-use event concepts;
- memory search and context injection user experience;
- server beta direction where it already moves state off developer machines;
- useful API shapes for events, sessions, memories, search, and context;
- adapter knowledge for Claude Code and Codex.

## Replace

The rewrite should remove or replace:

- local worker startup;
- local SQLite as authoritative state;
- local Chroma/vector database runtime;
- desktop-only viewer as the primary admin surface;
- mutable local settings as the enterprise configuration source;
- old package branding and installer assumptions;
- inherited `npx claude-mem install` behavior that bootstraps local runtime
  dependencies or a local worker;
- automation that publishes the inherited npm package;
- any flow that requires developer hosts to run summarization, embedding, or
  indexing services.

## Migration Strategy

1. Keep `upstream` as the historical source branch.
2. Build the new server architecture on `master`.
3. Port hook payload knowledge into thin adapter packages.
4. Port useful event and memory semantics into Python domain services.
5. Replace local worker calls with authenticated server APIs.
6. Replace the inherited installer with a small `connect`/`doctor`/`disconnect`/
   `install` client package that manages hooks and server credentials, plus
   operator commands for search, observations, memory version/link/links, and
   MCP install/serve.
7. Add import tooling only after the target schema and authorization model are
   stable.

## Compatibility Rule

If an upstream feature requires a local persistent process, it is not compatible
with the target runtime until it is moved server-side.
