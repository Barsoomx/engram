# Verification Matrix

This matrix records local commands, CI equivalents, status, and first decisive
failures for each completed Engram slice.

## 2026-06-25: Upstream Parity Audit Docs

Branch: `docs/parity-01-upstream-audit`

Scope:

- `docs/parity/claude-mem-parity-map.md`
- `docs/reference-gates.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none yet | yes | pass | Shows intended new docs plus pre-existing `.gitignore` change. |
| whitespace | `git diff --check` | none yet | yes | pass | Exit 0. |
| placeholder scan | `rg -n "[T]BD|[T]ODO|[F]IXME|[P]LACEHOLDER" docs/parity docs/reference-gates.md docs/verification-matrix.md` | none yet | yes | pass | Exit 1 with no matches. |
| docs content review | `sed -n '1,980p' docs/parity/claude-mem-parity-map.md`, `sed -n '1,700p' docs/reference-gates.md`, and `sed -n '1,200p' docs/verification-matrix.md` | none yet | yes | pass | Manual review completed against `goal.md` parity-map requirements. |

At this checkpoint, CI was not yet implemented on `master` for Engram's new
architecture branch.

## 2026-06-25: Monorepo Skeleton And Repository Quality CI

Branch: `feat/parity-02-monorepo-skeleton-ci`

Scope:

- `apps/backend/README.md`
- `apps/frontend/README.md`
- `packages/cli/README.md`
- `packages/mcp/README.md`
- `packages/claude-plugin/README.md`
- `packages/codex-plugin/README.md`
- `plugin-repository/README.md`
- `deploy/compose/README.md`
- `scripts/repository_layout.py`
- `scripts/repository_quality.py`
- `tests/repository/*`
- `.github/workflows/repository-quality.yml`
- `docs/superpowers/specs/2026-06-25-monorepo-skeleton-ci-design.md`
- `docs/superpowers/plans/2026-06-25-monorepo-skeleton-ci.md`

| Check | Local command | CI job | Required | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| live repo state | `git status --short --branch` | none | yes | pass | Exit 0. Shows branch `feat/parity-02-monorepo-skeleton-ci` plus pre-existing unstaged `.gitignore` edit. |
| layout contract | `python3 scripts/repository_layout.py` | Repository Quality | yes | pass | Exit 0 with no output. |
| repository text quality | `python3 scripts/repository_quality.py` | Repository Quality | yes | pass | Exit 0 with no findings. The private reference path allowlist covers `docs/reference-gates.md`. |
| repository tests | `python3 -m unittest discover -s tests -v` | Repository Quality | yes | pass | Exit 0. Ran 8 tests. |
| workflow contract | `python3 -m unittest tests.repository.test_repository_quality_workflow -v` | Repository Quality | yes | pass | Exit 0. Proves workflow calls the Python checks and no longer uses the brittle shell grep. |
| whitespace | `git diff --check HEAD` | Repository Quality whitespace step | yes | pass | Exit 0. |

First decisive failures fixed during the TDD loop:

- `python` was not available locally; the plan and workflow commands use
  `python3`.
- Layout test first failed with missing `scripts.repository_layout`.
- Quality test first failed with missing `scripts.repository_quality`.
- Test discovery first returned exit 5 with zero tests; package markers now make
  `python3 -m unittest discover -s tests -v` run the repository tests.
- Workflow test first failed because the workflow did not call the new scripts
  and still contained `grep -RInE`.
