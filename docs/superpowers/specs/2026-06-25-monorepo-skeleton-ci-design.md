# Monorepo Skeleton And CI Design

## Context

Engram has completed the first documentation gate with a committed upstream
parity map and reference-gates summary. The next roadmap item is a monorepo
skeleton and honest repository-quality CI before backend runtime code begins.

Current live state for this design:

- branch: `feat/parity-02-monorepo-skeleton-ci`;
- base checkpoint: `8fc82d8eafb98965ea34c6b478a1a42da2d5ff76`;
- `origin/master`: `5be75e333fd982f1ecb82a0277c7e273559a28e6`;
- `upstream`: `3fe0725a97e18b5edf3e61cde60e181ab2b6c997`;
- pre-existing local dirty file: `.gitignore`.

The existing `.github/workflows/repository-quality.yml` is too brittle for the
new docs state. Its shell pattern matches ordinary words such as `health`, and
it cannot distinguish forbidden branding leakage from intentional reference
documentation.

## Approved Direction

Build a narrow skeleton/CI checkpoint:

- create the top-level monorepo directories required by `goal.md`;
- add short README contracts to each empty product area so the directories are
  reviewable and testable;
- replace shell-only workflow checks with tested Python repository checks;
- keep backend, frontend, Compose, package manifests, and runtime code out of
  this slice.

The continuation request keeps the active goal moving and serves as approval to
proceed with this narrow option.

## Alternatives Considered

### Full Backend Stub In This Slice

This would add Django health checks, Compose, and service scaffolding now. It
would move faster toward a runnable system, but it mixes the monorepo skeleton
gate with the first backend/runtime gate. That makes review harder and bypasses
the planned TDD boundary for Django behavior.

### Documentation-Only Checkpoint

This would commit only a spec and plan. It is low risk, but it leaves CI known
to be inaccurate and leaves the repository without enforceable structure before
code-bearing work starts.

### Tested Skeleton And CI Repair

This is the selected design. It produces a reviewable repository contract,
fixes the existing quality gate, and gives the next backend slice a stable base.

## Architecture

The skeleton is a repository contract, not runtime scaffolding.

Top-level directories:

- `apps/backend`: future Django and DRF backend.
- `apps/frontend`: future Next.js admin console.
- `packages/cli`: future `engram` CLI.
- `packages/mcp`: future thin MCP bridge.
- `packages/claude-plugin`: future Claude Code plugin package.
- `packages/codex-plugin`: future Codex plugin package.
- `plugin-repository`: future installable plugin manifest distribution.
- `deploy/compose`: future local/self-hosted Compose runtime.
- `scripts`: repository-local validation scripts.
- `tests/repository`: stdlib tests for repository contracts.

Each product directory gets a README with three facts:

- what the directory will own;
- what it must not introduce yet;
- which later gate makes it active.

Repository checks are implemented as importable Python modules plus thin CLI
entrypoints. Tests exercise the Python functions directly. The GitHub workflow
calls the same scripts so local and CI behavior stay aligned.

## Quality Checks

The CI checkpoint enforces three low-dependency checks:

- required skeleton paths exist;
- repository text has no incomplete-work markers in tracked source, docs,
  scripts, tests, and workflow files;
- internal/private reference terms are exact-match scanned with explicit
  path-based allowlists.

The private-reference scan allows the existing reference-gates document to name
the private backend reference. It does not allow those terms to appear in new
runtime, package, workflow, or generic docs paths.

The scan does not ban legitimate upstream product names inside parity,
migration, fork-boundary, or architecture docs. The first runtime slice will add
a separate runtime-visible naming check once runtime files exist.

## Testing Strategy

Use stdlib `unittest` because no Python project scaffold exists yet.

The TDD loop for implementation:

1. Add repository layout tests and watch them fail because skeleton paths do not
   exist.
2. Add the skeleton files and watch layout tests pass.
3. Add repository quality tests and watch them fail because scripts do not
   exist.
4. Add the quality scripts and watch tests pass.
5. Add workflow contract tests and watch them fail until the workflow calls the
   scripts.
6. Run the full local equivalents used by CI.

## Deferred Work

This checkpoint intentionally does not add:

- Django project files;
- DRF APIs;
- PostgreSQL or Celery wiring;
- Docker Compose services;
- Next.js project files;
- package manifests;
- plugin installer behavior;
- CodeQL or dependency scans beyond existing Dependabot configuration.

Those belong to later gates after this repository shell is proven.

## Success Criteria

- Required monorepo paths are present and documented.
- Local repository checks run with Python only.
- GitHub repository-quality workflow calls the checked scripts.
- Existing reference-gates private-reference mention is explicitly allowed.
- New skeleton/docs/spec/plan files do not add runtime-visible old product names
  or private reference names outside the allowlist.
- Verification commands and exit codes are recorded in
  `docs/verification-matrix.md`.
