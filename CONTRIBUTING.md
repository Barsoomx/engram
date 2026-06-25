# Contributing

This repository is currently in a docs-first architecture phase for the
server-only rewrite.

## Expectations

- Keep `upstream` as the clean source snapshot.
- Target product changes at `master`.
- Preserve required Apache License 2.0 notices.
- Do not add local-worker requirements to the target architecture.
- Keep provider secrets server-side.
- Add or update tests when implementation code changes.

## Commit Messages

Use short English messages with conventional prefixes:

- `docs: describe rbac scope model`
- `feat: add hook ingestion endpoint`
- `fix: prevent api key scope expansion`
- `test: cover team memory isolation`

Do not add AI co-author trailers.
