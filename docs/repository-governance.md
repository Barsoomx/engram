# Repository Governance

## Branch Strategy

- `upstream`: clean imported snapshot from `thedotmack/claude-mem`.
- `master`: product, architecture, and rewrite branch.

The repository default branch should be `master`.

## Required Repository Settings

- Ruleset protecting the default branch.
- Block branch deletion.
- Block non-fast-forward pushes.
- Require linear history.
- Require pull request review once implementation work starts.
- Require repository quality workflow before merging.
- Enable secret scanning and Dependabot alerts where GitHub plan supports them.

## Initial Workflows

The docs-first phase should keep CI small and honest:

- repository quality: whitespace checks and forbidden sensitive-term scan;
- CodeQL once the Python/TypeScript source tree stabilizes;
- Dependabot for GitHub Actions and later Python package manifests.

Do not keep upstream npm publish or automation workflows on `master`; they target
the old package runtime.

## Commit Rules

- Use conventional prefixes such as `feat`, `fix`, `docs`, `chore`, `test`,
  `refactor`.
- Use English commit messages.
- Do not include AI co-author trailers.
- Do not force-push protected branches.

## Quality Bar For The Rewrite

- Architecture docs stay ahead of code.
- Each domain service has tests at the service boundary.
- Each hook adapter has contract tests against saved payload fixtures.
- Authorization tests cover organization, team, project, user, service account,
  and API key narrowing.
- Retrieval tests include exact search, permission filtering, stale memory, and
  conflict handling. Semantic expansion tests are required when that adapter is
  enabled.
- Secret tests prove redaction and API-key non-exportability.

## Implementation Test Rules

- TDD is required for implementation changes: add the failing test first.
- Test files use `*_tests.py` for Python implementation.
- Domain services have service-boundary tests.
- Hook adapters have fixture-based contract tests per agent/event.
- RBAC, outbox, and seed-data migrations have migration tests.
- Transactional/race tests are marked and run separately when they require
  sequential execution.
- CI must run Ruff, type checks, and pytest once the Python scaffold exists.

## Sensitive-Term Scan

Repository quality CI owns the forbidden sensitive-term regex. The scan covers
README, docs, governance files, and workflow definitions. Failures should point
to the exact file and line and must be reviewed by the repository owner.
