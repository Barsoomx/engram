from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class RepositoryQualityWorkflowTests(unittest.TestCase):
    def test_workflow_calls_repository_checks(self) -> None:
        workflow = (ROOT / ".github/workflows/repository-quality.yml").read_text(
            encoding="utf-8",
        )

        self.assertIn("python3 scripts/repository_layout.py", workflow)
        self.assertIn("python3 scripts/repository_quality.py", workflow)
        self.assertIn("python3 -m unittest discover -s tests", workflow)
        self.assertIn(
            "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v",
            workflow,
        )

    def test_workflow_does_not_use_brittle_shell_grep(self) -> None:
        workflow = (ROOT / ".github/workflows/repository-quality.yml").read_text(
            encoding="utf-8",
        )

        self.assertNotIn("grep -RInE", workflow)


if __name__ == "__main__":
    unittest.main()
