from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BackendWorkflowTests(unittest.TestCase):
    def test_backend_workflow_runs_required_commands(self) -> None:
        workflow = (ROOT / '.github/workflows/backend.yml').read_text(encoding='utf-8')

        self.assertIn('working-directory: apps/backend', workflow)
        self.assertIn('poetry install --no-interaction', workflow)
        self.assertIn('poetry run ruff check .', workflow)
        self.assertIn('poetry run ruff format --check .', workflow)
        self.assertIn('poetry run python manage.py makemigrations --check --dry-run --settings=settings.test_settings', workflow)
        self.assertIn('poetry run python manage.py migrate --noinput --settings=settings.test_settings', workflow)
        self.assertIn('poetry run pytest -v', workflow)
        self.assertIn("PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v", workflow)
        self.assertIn('python3 scripts/repository_layout.py', workflow)
        self.assertIn('python3 scripts/repository_quality.py', workflow)


if __name__ == '__main__':
    unittest.main()
