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
