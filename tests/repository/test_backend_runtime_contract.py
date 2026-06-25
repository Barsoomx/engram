import json
from pathlib import Path
import sqlite3
import unittest

from scripts.repository_layout import REQUIRED_PATHS, missing_paths


ROOT = Path(__file__).resolve().parents[2]


class BackendRuntimeLayoutTests(unittest.TestCase):
    expected = {
        'apps/backend/manage.py',
        'apps/backend/pyproject.toml',
        'apps/backend/pytest.ini',
        'apps/backend/engram/celery_app.py',
        'apps/backend/engram/celery_bootsteps.py',
        'apps/backend/engram/celeryconfig.py',
        'apps/backend/settings/settings.py',
        'apps/backend/settings/logs.py',
        'apps/backend/settings/test_settings.py',
        'apps/backend/settings/urls.py',
        'apps/backend/engram/access/models.py',
        'apps/backend/engram/access/services.py',
        'apps/backend/engram/access/access_scope_tests.py',
        'apps/backend/engram/access/migrations/0001_initial.py',
        'apps/backend/engram/access/migrations/0002_seed_default_roles.py',
        'apps/backend/engram/core/models.py',
        'apps/backend/engram/core/application_foundation_tests.py',
        'apps/backend/engram/core/domain/__init__.py',
        'apps/backend/engram/core/domain/event_dispatcher.py',
        'apps/backend/engram/core/domain/event_store.py',
        'apps/backend/engram/core/domain/events.py',
        'apps/backend/engram/core/domain/singleton.py',
        'apps/backend/engram/core/domain/types.py',
        'apps/backend/engram/core/domain/usecases/base.py',
        'apps/backend/engram/core/domain/usecases/errors.py',
        'apps/backend/engram/core/domain/usecases/transactional_base.py',
        'apps/backend/engram/core/middlewares/domain_exception.py',
        'apps/backend/engram/core/middlewares/drf_exception_handler.py',
        'apps/backend/engram/core/middlewares/request_response_logging.py',
        'apps/backend/engram/core/observability/logs.py',
        'apps/backend/engram/core/observability/sentryconfig.py',
        'apps/backend/engram/core/redis_sentinel.py',
        'apps/backend/engram/core/retryable_django_task.py',
        'apps/backend/engram/core/retries_checker.py',
        'apps/backend/engram/core/golden_path_tests.py',
        'apps/backend/engram/core/migrations/0001_initial.py',
        'apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py',
        'apps/backend/engram/hooks/apps.py',
        'apps/backend/engram/hooks/serializers.py',
        'apps/backend/engram/hooks/services.py',
        'apps/backend/engram/hooks/urls.py',
        'apps/backend/engram/hooks/views.py',
        'apps/backend/engram/hooks/hook_ingest_tests.py',
        'apps/backend/engram/imports/__init__.py',
        'apps/backend/engram/imports/apps.py',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/manifest.json',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/claude_mem_minimal.sql',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/settings.json',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/transcript-watch.json',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/transcript-watch-state.json',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/corpora/deferred.corpus.json',
        'apps/backend/engram/imports/fixtures/claude_mem_minimal/vector-db/.keep',
        'apps/backend/engram/memory/apps.py',
        'apps/backend/engram/memory/services.py',
        'apps/backend/engram/memory/tasks.py',
        'apps/backend/engram/memory/memory_worker_tests.py',
        'apps/backend/engram/memory/management/commands/engram_promote_memory_candidate.py',
        'apps/backend/engram/model_policy/apps.py',
        'apps/backend/engram/model_policy/models.py',
        'apps/backend/engram/model_policy/serializers.py',
        'apps/backend/engram/model_policy/services.py',
        'apps/backend/engram/model_policy/urls.py',
        'apps/backend/engram/model_policy/views.py',
        'apps/backend/engram/model_policy/model_policy_tests.py',
        'apps/backend/engram/model_policy/migrations/0001_initial.py',
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
        self.assertFalse(
            [
                path
                for path in REQUIRED_PATHS
                if path.startswith('apps/backend/engram/core/migrations/')
                and 'outboxevent' in path.lower()
            ],
        )

    def test_import_app_is_installed(self) -> None:
        settings = (ROOT / 'apps/backend/settings/settings.py').read_text(encoding='utf-8')

        self.assertIn("'engram.imports'", settings)

    def test_application_foundation_is_installed(self) -> None:
        settings = (ROOT / 'apps/backend/settings/settings.py').read_text(encoding='utf-8')
        logs = (ROOT / 'apps/backend/settings/logs.py').read_text(encoding='utf-8')
        pyproject = (ROOT / 'apps/backend/pyproject.toml').read_text(encoding='utf-8')

        self.assertIn("'django_structlog'", settings)
        self.assertIn("'django_structlog.middlewares.RequestMiddleware'", settings)
        self.assertIn("'engram.core.middlewares.ExceptionHandlingMiddleware'", settings)
        self.assertIn("'EXCEPTION_HANDLER': 'engram.core.middlewares.custom_exception_handler'", settings)
        self.assertIn('configure_logger(', settings)
        self.assertIn('DJANGO_STRUCTLOG_CELERY_ENABLED = True', settings)
        self.assertIn("'BACKEND': 'django_redis.cache.RedisCache'", settings)
        self.assertIn("'LOCATION': ENGRAM_REDIS_URL", settings)
        self.assertIn('send_default_pii=False', logs)
        self.assertIn('django-redis', pyproject)
        self.assertIn('cryptography', pyproject)
        self.assertIn("'engram.model_policy'", settings)

    def test_celery_foundation_uses_sla_queues_and_confirm_publish(self) -> None:
        celeryconfig = (ROOT / 'apps/backend/engram/celeryconfig.py').read_text(encoding='utf-8')
        celery_app = (ROOT / 'apps/backend/engram/celery_app.py').read_text(encoding='utf-8')
        settings = (ROOT / 'apps/backend/settings/settings.py').read_text(encoding='utf-8')

        for queue_name in (
            'QUEUE_REALTIME',
            'QUEUE_NEAR_REALTIME',
            'QUEUE_BATCH',
            'QUEUE_HIGHMEMORY',
            'QUEUE_DOMAIN_EVENTS',
        ):
            self.assertIn(queue_name, celeryconfig)

        self.assertIn("'confirm_publish': True", celeryconfig)
        self.assertIn("task_cls='engram.core.retryable_django_task.RetryableTask'", celery_app)
        self.assertIn('DjangoStructLogInitStep', celery_app)
        self.assertIn('LivenessProbe', celery_app)
        self.assertIn("'amqp://engram:engram@rabbitmq:5672/engram'", settings)
        self.assertNotIn('CELERY_BROKER_URL = os.environ.get(\'ENGRAM_CELERY_BROKER_URL\', ENGRAM_REDIS_URL)', settings)

    def test_claude_mem_fixture_is_text_reviewable_and_sanitized(self) -> None:
        fixture_root = ROOT / 'apps/backend/engram/imports/fixtures/claude_mem_minimal'
        manifest = json.loads((fixture_root / 'manifest.json').read_text(encoding='utf-8'))
        sql = (fixture_root / 'claude_mem_minimal.sql').read_text(encoding='utf-8')

        self.assertEqual('fixture-store', manifest['source_store_id'])
        self.assertEqual(
            {
                'sdk_sessions': 1,
                'user_prompts': 1,
                'observations': 1,
                'session_summaries': 1,
                'pending_messages': 1,
                'observation_feedback': 1,
            },
            manifest['expected'],
        )
        self.assertIn('CREATE TABLE sdk_sessions', sql)
        self.assertIn('CREATE TABLE user_prompts', sql)
        self.assertIn('CREATE TABLE observations', sql)
        self.assertIn('CREATE TABLE session_summaries', sql)
        self.assertIn('CREATE TABLE pending_messages', sql)
        self.assertIn('CREATE TABLE observation_feedback', sql)
        self.assertNotIn('SQLite format 3', sql)
        self.assertEqual(1, sql.count('sk-test_fake_import_token_1234567890'))

        connection = sqlite3.connect(':memory:')
        try:
            connection.executescript(sql)
            for table_name, expected_count in manifest['expected'].items():
                row_count = connection.execute(f'SELECT COUNT(*) FROM {table_name}').fetchone()[0]
                self.assertEqual(expected_count, row_count)
        finally:
            connection.close()

        fixture_files = [path for path in fixture_root.rglob('*') if path.is_file()]
        self.assertNotIn('.env', {path.name for path in fixture_files})

        fixture_text = '\n'.join(path.read_text(encoding='utf-8') for path in fixture_files)
        self.assertNotIn('OPENAI_API_KEY', fixture_text)
        self.assertNotIn('ANTHROPIC_API_KEY', fixture_text)
        self.assertNotIn('DATABASE_URL', fixture_text)


class BackendComposeContractTests(unittest.TestCase):
    def test_compose_declares_backend_runtime_services(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')
        readme = (ROOT / 'deploy/compose/README.md').read_text(encoding='utf-8')

        for service_name in (
            'api:',
            'relay:',
            'worker-realtime:',
            'worker-near-realtime:',
            'worker-batch:',
            'worker-highmemory:',
            'worker-domain-events:',
            'postgres:',
            'redis:',
            'rabbitmq:',
        ):
            self.assertIn(service_name, compose)

        self.assertIn('RabbitMQ broker', readme)
        self.assertIn('Redis result/cache backend', readme)
        self.assertIn('queue-specific Celery workers', readme)
        self.assertNotIn('Redis-compatible broker', readme)

    def test_compose_uses_healthchecks_relay_and_real_worker(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        self.assertIn('condition: service_healthy', compose)
        self.assertIn('/-/readyz/', compose)
        self.assertIn('pg_isready', compose)
        self.assertIn('redis-cli', compose)
        self.assertIn('rabbitmq-diagnostics', compose)
        self.assertIn('amqp://engram:engram@rabbitmq:5672/engram', compose)
        self.assertIn('python manage.py celery_outbox_relay', compose)
        self.assertIn('celery -A engram.celery_app worker', compose)

    def test_compose_routes_workers_to_engram_sla_queues(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')
        worker_queues = {
            'worker-realtime': 'engram-realtime',
            'worker-near-realtime': 'engram-near-realtime',
            'worker-batch': 'engram-batch',
            'worker-highmemory': 'engram-highmemory',
            'worker-domain-events': 'engram-domain-events',
        }

        for service_name, queue_name in worker_queues.items():
            self.assertIn(f'  {service_name}:', compose)
            self.assertIn(
                f'celery -A engram.celery_app worker --loglevel=info -Q {queue_name}',
                compose,
            )
            self.assertEqual(1, compose.count(f'-Q {queue_name}'))

        self.assertNotIn(
            'celery -A engram.celery_app worker --loglevel=info"',
            compose,
        )

    def test_compose_relay_is_package_transport_not_domain_outbox_processing(self) -> None:
        compose = (ROOT / 'deploy/compose/docker-compose.yml').read_text(encoding='utf-8')

        self.assertIn('python manage.py celery_outbox_relay', compose)
        self.assertNotIn('engram_process_observation_outbox', compose)

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
        self.assertNotIn('engram_promote_memory_candidate', script)
        self.assertIn('Waiting for worker-created retrieval document', script)

    def test_golden_path_proves_current_hook_observation_created_context_memory(self) -> None:
        script = (ROOT / 'scripts/e2e_golden_path.py').read_text(encoding='utf-8')

        self.assertIn("run_id = secrets.token_hex(8)", script)
        self.assertIn("progress('Clearing Compose state')", script)
        self.assertLess(
            script.index("progress('Clearing Compose state')"),
            script.index("progress('Starting Compose services')"),
        )
        self.assertIn("run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key)", script)
        self.assertIn('post_tool_use_payload(run_id)', script)
        self.assertIn('session_start_payload(run_id)', script)
        self.assertIn('wait_for_worker_memory(project_id, run_id, api_key)', script)
        self.assertIn('worker_memory_query(project_id, run_id)', script)
        self.assertIn('def memory_title(run_id: str) -> str:', script)
        self.assertIn('def memory_body(run_id: str) -> str:', script)
        self.assertIn("'session_id': f'e2e-session-observation-{run_id}'", script)
        self.assertIn("'event_id': f'e2e-hook-event-{run_id}'", script)
        self.assertIn("'idempotency_key': f'e2e-hook-idempotency-{run_id}'", script)
        self.assertIn("'request_id': f'e2e-hook-request-{run_id}'", script)
        self.assertIn("'session_id': f'e2e-session-context-{run_id}'", script)
        self.assertIn("'request_id': f'e2e-context-request-{run_id}'", script)
        self.assertIn("'source_observation__raw_event'", script)
        self.assertIn('client_event_id = {json.dumps(client_event_id)}', script)
        self.assertIn('request_id = {json.dumps(request_id)}', script)
        self.assertIn('raw_event.client_event_id != client_event_id', script)
        self.assertIn('raw_event.request_id != request_id', script)
        self.assertIn('str(version.source_observation_id) not in document.source_observation_ids', script)
        self.assertNotIn("'session_id': 'e2e-session-observation'", script)
        self.assertNotIn("'event_id': 'e2e-hook-event-1'", script)


if __name__ == '__main__':
    unittest.main()
