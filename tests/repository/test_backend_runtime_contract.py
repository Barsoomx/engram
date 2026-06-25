from pathlib import Path
import unittest

from scripts.repository_layout import REQUIRED_PATHS, missing_paths


ROOT = Path(__file__).resolve().parents[2]


class BackendRuntimeLayoutTests(unittest.TestCase):
    expected = {
        'apps/backend/manage.py',
        'apps/backend/pyproject.toml',
        'apps/backend/pytest.ini',
        'apps/backend/settings/settings.py',
        'apps/backend/settings/test_settings.py',
        'apps/backend/settings/urls.py',
        'apps/backend/engram/health/views.py',
        'apps/backend/Dockerfile',
        'deploy/compose/docker-compose.yml',
        'deploy/compose/.env.example',
    }

    def test_backend_runtime_paths_are_layout_requirements(self) -> None:
        self.assertTrue(self.expected.issubset(set(REQUIRED_PATHS)))

    def test_backend_runtime_paths_exist(self) -> None:
        missing = set(missing_paths(ROOT))

        self.assertFalse(self.expected & missing)


class BackendComposeContractTests(unittest.TestCase):
    def test_compose_declares_backend_runtime_services(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        for service_name in ('api:', 'worker:', 'postgres:', 'redis:'):
            self.assertIn(service_name, compose)

    def test_compose_uses_healthchecks_and_real_worker(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        self.assertIn('condition: service_healthy', compose)
        self.assertIn('/-/readyz/', compose)
        self.assertIn('pg_isready', compose)
        self.assertIn('redis-cli', compose)
        self.assertIn('celery -A engram.celery_app worker', compose)

    def test_backend_dockerfile_uses_poetry_and_backend_project(self) -> None:
        dockerfile = (ROOT / 'apps/backend/Dockerfile').read_text(encoding='utf-8')

        self.assertIn('FROM python:3.12-slim', dockerfile)
        self.assertIn('poetry install --no-interaction', dockerfile)
        self.assertIn('COPY apps/backend', dockerfile)


if __name__ == '__main__':
    unittest.main()
