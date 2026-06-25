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
        'apps/backend/engram/access/models.py',
        'apps/backend/engram/access/services.py',
        'apps/backend/engram/access/access_scope_tests.py',
        'apps/backend/engram/access/migrations/0001_initial.py',
        'apps/backend/engram/access/migrations/0002_seed_default_roles.py',
        'apps/backend/engram/core/models.py',
        'apps/backend/engram/core/golden_path_tests.py',
        'apps/backend/engram/core/migrations/0001_initial.py',
        'apps/backend/engram/core/migrations/0002_remove_outboxevent_core_outbox_unique_idempotency_key_per_event_and_more.py',
        'apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py',
        'apps/backend/engram/hooks/apps.py',
        'apps/backend/engram/hooks/serializers.py',
        'apps/backend/engram/hooks/services.py',
        'apps/backend/engram/hooks/urls.py',
        'apps/backend/engram/hooks/views.py',
        'apps/backend/engram/hooks/hook_ingest_tests.py',
        'apps/backend/engram/memory/apps.py',
        'apps/backend/engram/memory/services.py',
        'apps/backend/engram/memory/tasks.py',
        'apps/backend/engram/memory/memory_worker_tests.py',
        'apps/backend/engram/memory/management/commands/engram_promote_memory_candidate.py',
        'apps/backend/engram/health/views.py',
        'apps/backend/Dockerfile',
        'deploy/compose/docker-compose.yml',
        'deploy/compose/.env.example',
        'scripts/e2e_golden_path.py',
        '.github/workflows/compose-e2e.yml',
    }

    def test_backend_runtime_paths_are_layout_requirements(self) -> None:
        self.assertTrue(self.expected.issubset(set(REQUIRED_PATHS)))

    def test_backend_runtime_paths_exist(self) -> None:
        missing = set(missing_paths(ROOT))

        self.assertFalse(self.expected & missing)

    def test_backend_runtime_layout_does_not_require_manual_observation_outbox_command(self) -> None:
        self.assertNotIn(
            'apps/backend/engram/memory/management/commands/engram_process_observation_outbox.py',
            REQUIRED_PATHS,
        )


class BackendComposeContractTests(unittest.TestCase):
    def test_compose_declares_backend_runtime_services(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        for service_name in ('api:', 'relay:', 'worker:', 'postgres:', 'redis:'):
            self.assertIn(service_name, compose)

    def test_compose_uses_healthchecks_relay_and_real_worker(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        self.assertIn('condition: service_healthy', compose)
        self.assertIn('/-/readyz/', compose)
        self.assertIn('pg_isready', compose)
        self.assertIn('redis-cli', compose)
        self.assertIn('python manage.py celery_outbox_relay', compose)
        self.assertIn('celery -A engram.celery_app worker', compose)

    def test_backend_dockerfile_uses_poetry_and_backend_project(self) -> None:
        dockerfile = (ROOT / 'apps/backend/Dockerfile').read_text(encoding='utf-8')

        self.assertIn('FROM python:3.12-slim', dockerfile)
        self.assertIn('poetry install --no-interaction', dockerfile)
        self.assertIn('COPY apps/backend', dockerfile)

    def test_compose_e2e_workflow_runs_golden_path_script(self) -> None:
        workflow = (ROOT / '.github/workflows/compose-e2e.yml').read_text(encoding='utf-8')

        self.assertIn('name: Compose E2E', workflow)
        self.assertIn('pull_request:', workflow)
        self.assertIn('push:', workflow)
        self.assertIn('actions/checkout@v4', workflow)
        self.assertIn('actions/setup-python@v5', workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn('python3 scripts/e2e_golden_path.py', workflow)

    def test_golden_path_waits_for_relayed_tasks_instead_of_manual_outbox_processing(self) -> None:
        script = (ROOT / 'scripts/e2e_golden_path.py').read_text(encoding='utf-8')

        self.assertNotIn('engram_process_observation_outbox', script)
        self.assertIn('engram_promote_memory_candidate', script)


if __name__ == '__main__':
    unittest.main()
