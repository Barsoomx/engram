# Monorepo Skeleton And CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tested Engram monorepo skeleton and replace brittle repository-quality CI with checked Python scripts.

**Architecture:** The skeleton is a repository contract only. Python scripts own layout and quality checks; tests exercise their importable functions, and GitHub Actions invokes the same entrypoints.

**Tech Stack:** Python stdlib, `unittest`, GitHub Actions, Markdown.

## Global Constraints

- Keep backend, frontend, Compose, and package runtime scaffolds out of this slice.
- Do not add developer-machine memory workers, local SQLite/Chroma authority, or local summarization services.
- Use `pnpm` for future JavaScript work; this slice does not install JavaScript dependencies.
- Do not use npm or yarn install commands.
- Keep existing `.gitignore` local modification unstaged.
- Commit messages must use an allowed prefix from local AGENTS rules.
- No real secrets or provider credentials may be added.
- Private reference names are allowed only in explicitly allowlisted reference documentation.

---

## File Structure

- Create `apps/backend/README.md`: backend ownership contract.
- Create `apps/frontend/README.md`: frontend ownership contract.
- Create `packages/cli/README.md`: CLI ownership contract.
- Create `packages/mcp/README.md`: MCP bridge ownership contract.
- Create `packages/claude-plugin/README.md`: Claude Code plugin ownership contract.
- Create `packages/codex-plugin/README.md`: Codex plugin ownership contract.
- Create `plugin-repository/README.md`: plugin manifest distribution contract.
- Create `deploy/compose/README.md`: Compose deployment contract.
- Create `scripts/repository_layout.py`: required path contract and CLI check.
- Create `scripts/repository_quality.py`: text scans and CLI check.
- Create `tests/repository/test_repository_layout.py`: layout contract tests.
- Create `tests/repository/test_repository_quality.py`: quality scanner tests.
- Create `tests/repository/test_repository_quality_workflow.py`: workflow contract tests.
- Modify `.github/workflows/repository-quality.yml`: call Python checks.
- Modify `docs/verification-matrix.md`: record this checkpoint's commands.

## Task 1: Repository Layout Contract

**Files:**

- Create: `tests/repository/test_repository_layout.py`
- Create: `scripts/repository_layout.py`
- Create: `apps/backend/README.md`
- Create: `apps/frontend/README.md`
- Create: `packages/cli/README.md`
- Create: `packages/mcp/README.md`
- Create: `packages/claude-plugin/README.md`
- Create: `packages/codex-plugin/README.md`
- Create: `plugin-repository/README.md`
- Create: `deploy/compose/README.md`

**Interfaces:**

- Produces: `scripts.repository_layout.REQUIRED_PATHS: tuple[str, ...]`
- Produces: `scripts.repository_layout.missing_paths(root: Path) -> list[str]`
- Produces: `scripts.repository_layout.main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write the failing layout test**

```python
from pathlib import Path
import unittest

from scripts.repository_layout import REQUIRED_PATHS, missing_paths


ROOT = Path(__file__).resolve().parents[2]


class RepositoryLayoutTests(unittest.TestCase):
    def test_required_paths_are_present_in_checkout(self) -> None:
        self.assertEqual([], missing_paths(ROOT))

    def test_required_paths_cover_product_boundaries(self) -> None:
        expected = {
            'apps/backend/README.md',
            'apps/frontend/README.md',
            'packages/cli/README.md',
            'packages/mcp/README.md',
            'packages/claude-plugin/README.md',
            'packages/codex-plugin/README.md',
            'plugin-repository/README.md',
            'deploy/compose/README.md',
        }

        self.assertTrue(expected.issubset(set(REQUIRED_PATHS)))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the layout test and verify red**

Run: `python -m unittest tests.repository.test_repository_layout -v`

Expected: fails with `ModuleNotFoundError` or missing path assertions.

- [ ] **Step 3: Add minimal layout script**

```python
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


REQUIRED_PATHS: tuple[str, ...] = (
    'apps/backend/README.md',
    'apps/frontend/README.md',
    'packages/cli/README.md',
    'packages/mcp/README.md',
    'packages/claude-plugin/README.md',
    'packages/codex-plugin/README.md',
    'plugin-repository/README.md',
    'deploy/compose/README.md',
)


def missing_paths(root: Path) -> list[str]:
    return [path for path in REQUIRED_PATHS if not (root / path).exists()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    args = parser.parse_args(argv)

    missing = missing_paths(Path(args.root))
    if missing:
        for path in missing:
            print(f'missing required path: {path}')

        return 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Add skeleton README files**

Each README must state the directory owner, the out-of-scope runtime work, and
the later activation gate. Keep all files short and specific to their path.

- [ ] **Step 5: Run the layout test and script**

Run: `python -m unittest tests.repository.test_repository_layout -v`

Expected: `OK`.

Run: `python scripts/repository_layout.py`

Expected: exit `0` with no output.

- [ ] **Step 6: Commit layout contract**

```bash
git add apps packages plugin-repository deploy scripts/repository_layout.py tests/repository/test_repository_layout.py
git commit -m "chore: add monorepo layout contract"
```

## Task 2: Repository Quality Scanner

**Files:**

- Create: `tests/repository/test_repository_quality.py`
- Create: `scripts/repository_quality.py`

**Interfaces:**

- Consumes: tracked text files under a repository root.
- Produces: `scripts.repository_quality.Finding`
- Produces: `scripts.repository_quality.scan_root(root: Path) -> list[Finding]`
- Produces: `scripts.repository_quality.scan_text(path: str, text: str) -> list[Finding]`
- Produces: `scripts.repository_quality.main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write failing quality tests**

```python
from pathlib import Path
import tempfile
import unittest

from scripts.repository_quality import scan_root, scan_text


class RepositoryQualityTests(unittest.TestCase):
    def test_incomplete_marker_is_reported(self) -> None:
        marker = 'T' + 'ODO'

        findings = scan_text('docs/example.md', f'{marker}: finish later\n')

        self.assertEqual(1, len(findings))
        self.assertEqual('incomplete-marker', findings[0].code)
        self.assertEqual(1, findings[0].line)

    def test_private_reference_term_requires_allowed_path(self) -> None:
        private_name = 'alt' + 'yn'

        findings = scan_text('docs/new-doc.md', f'{private_name}-backend\n')

        self.assertEqual(1, len(findings))
        self.assertEqual('private-reference-term', findings[0].code)

    def test_private_reference_term_is_allowed_in_reference_gates(self) -> None:
        private_name = 'alt' + 'yn'

        findings = scan_text('docs/reference-gates.md', f'{private_name}-backend\n')

        self.assertEqual([], findings)

    def test_scan_root_reads_text_files(self) -> None:
        marker = 'F' + 'IXME'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs').mkdir()
            (root / 'docs' / 'example.md').write_text(f'{marker}: later\n', encoding='utf-8')

            findings = scan_root(root)

        self.assertEqual(1, len(findings))
        self.assertEqual('docs/example.md', findings[0].path)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run the quality tests and verify red**

Run: `python -m unittest tests.repository.test_repository_quality -v`

Expected: fails with `ModuleNotFoundError` or missing function errors.

- [ ] **Step 3: Add minimal quality scanner**

Implement a `Finding` dataclass with `path`, `line`, `code`, and `message`.
Scan text files under README, docs, governance files, workflow files, scripts,
tests, apps, packages, plugin repository, and deploy. Skip `.git`, `.idea`,
binary files, and `__pycache__`.

Build forbidden marker strings with concatenation so the scanner source does
not contain the scanned markers as contiguous text.

- [ ] **Step 4: Run quality tests and local scan**

Run: `python -m unittest tests.repository.test_repository_quality -v`

Expected: `OK`.

Run: `python scripts/repository_quality.py`

Expected: exit `0` with no findings.

- [ ] **Step 5: Commit quality scanner**

```bash
git add scripts/repository_quality.py tests/repository/test_repository_quality.py
git commit -m "test: add repository quality scanner"
```

## Task 3: Workflow Contract

**Files:**

- Create: `tests/repository/test_repository_quality_workflow.py`
- Modify: `.github/workflows/repository-quality.yml`

**Interfaces:**

- Consumes: `.github/workflows/repository-quality.yml`
- Produces: workflow steps that run `python scripts/repository_layout.py`,
  `python scripts/repository_quality.py`, and `python -m unittest discover -s tests`.

- [ ] **Step 1: Write failing workflow test**

```python
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RepositoryQualityWorkflowTests(unittest.TestCase):
    def test_workflow_calls_repository_checks(self) -> None:
        workflow = (ROOT / '.github/workflows/repository-quality.yml').read_text(
            encoding='utf-8',
        )

        self.assertIn('python scripts/repository_layout.py', workflow)
        self.assertIn('python scripts/repository_quality.py', workflow)
        self.assertIn('python -m unittest discover -s tests', workflow)

    def test_workflow_does_not_use_brittle_shell_grep(self) -> None:
        workflow = (ROOT / '.github/workflows/repository-quality.yml').read_text(
            encoding='utf-8',
        )

        self.assertNotIn('grep -RInE', workflow)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run workflow test and verify red**

Run: `python -m unittest tests.repository.test_repository_quality_workflow -v`

Expected: fails because the workflow still contains the old shell scan and does
not call the new scripts.

- [ ] **Step 3: Update workflow**

Replace the brittle shell scan with three script-backed steps:

```yaml
      - name: Check required repository layout
        run: python scripts/repository_layout.py

      - name: Check repository text quality
        run: python scripts/repository_quality.py

      - name: Run repository tests
        run: python -m unittest discover -s tests
```

- [ ] **Step 4: Run workflow test and full local checks**

Run: `python -m unittest tests.repository.test_repository_quality_workflow -v`

Expected: `OK`.

Run: `python -m unittest discover -s tests -v`

Expected: `OK`.

Run: `git diff --check`

Expected: exit `0`.

- [ ] **Step 5: Commit workflow contract**

```bash
git add .github/workflows/repository-quality.yml tests/repository/test_repository_quality_workflow.py
git commit -m "chore: wire repository quality workflow"
```

## Task 4: Verification Matrix

**Files:**

- Modify: `docs/verification-matrix.md`

**Interfaces:**

- Consumes: command outcomes from Tasks 1-3.
- Produces: auditable record for the skeleton/CI checkpoint.

- [ ] **Step 1: Add checkpoint entry**

Record branch, scope, commands, exit codes, and first decisive failure if any.

- [ ] **Step 2: Run final verification**

Run:

```bash
python scripts/repository_layout.py
python scripts/repository_quality.py
python -m unittest discover -s tests -v
git diff --check HEAD
```

Expected: all commands exit `0`.

- [ ] **Step 3: Commit verification record**

```bash
git add docs/verification-matrix.md
git commit -m "chore: record skeleton ci verification"
```

## Self-Review

- Spec coverage: tasks cover skeleton paths, checked quality scripts, workflow
  wiring, and verification evidence.
- Type consistency: function names and return types match across tasks.
- Scope: no backend, frontend, Compose runtime, package manifest, or plugin
  installer behavior is included.
