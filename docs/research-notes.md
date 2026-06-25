# Research Notes

## Inputs

- Upstream project: `thedotmack/claude-mem`, imported into the `upstream` branch.
- Official Claude Code hooks documentation: lifecycle hooks can run commands,
  HTTP endpoints, and prompt hooks around session, prompt, and tool events.
- Official Codex hooks documentation: hooks are an extensibility framework with
  managed hook configuration, trust review, event scopes, and JSON event I/O.
- Repository baseline: `Barsoomx/django-celery-outbox` branch protection,
  workflow naming, Dependabot, CodeQL, documentation, and contribution hygiene.
- Local product requirements: server-only runtime, company-owned deployment,
  Sentry-like teams and scopes, team-owned model keys, and simple enterprise
  administration.

## Upstream Findings

- Keep the idea of observation capture from agent lifecycle events.
- Keep memory search and context injection as the core UX.
- Keep useful server-beta concepts such as API events, sessions, memories,
  search, and context endpoints.
- Remove local worker startup, local SQLite as authoritative state, local vector
  database runtime, desktop-only admin assumptions, and old npm publish
  automation from the target branch.

## Hook Findings

Hooks should be the deterministic memory control plane:

- session start retrieves memory;
- prompt/tool hooks provide focused retrieval and guidance;
- post-tool hooks capture observations;
- session end distills unresolved work and memory updates;
- explicit tools let agents search, observe, update, and explain memory.

Policy enforcement can reuse hook surfaces, but it is separate from memory
guidance. Authorization and policy decisions remain server-side.

## Architecture Findings

The first production version should remain intentionally small:

- one server-side source of truth;
- one reusable scope model;
- one hook API family per agent type;
- one secret/model policy resolver;
- PostgreSQL first;
- exact search first;
- vector search as an adapter;
- domain events and durable outbox for asynchronous work.

This keeps the system understandable while leaving room for SaaS and on-premise
growth.
