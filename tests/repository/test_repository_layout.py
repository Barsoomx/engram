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
            '.github/workflows/compose-e2e.yml',
            'scripts/e2e_golden_path.py',
            'packages/cli/README.md',
            'packages/cli/pyproject.toml',
            'packages/cli/engram_cli/main.py',
            'packages/cli/engram_cli/cli_lifecycle_tests.py',
            'packages/mcp/README.md',
            'packages/claude-plugin/README.md',
            'packages/codex-plugin/README.md',
            'plugin-repository/README.md',
            'deploy/compose/README.md',
        }

        self.assertTrue(expected.issubset(set(REQUIRED_PATHS)))


class RepositoryContractDocumentationTests(unittest.TestCase):
    historical_outbox_docs = (
        'docs/superpowers/specs/2026-06-25-memory-worker-design.md',
        'docs/superpowers/plans/2026-06-25-memory-worker.md',
        'docs/superpowers/specs/2026-06-25-compose-golden-path-design.md',
        'docs/superpowers/plans/2026-06-25-compose-golden-path.md',
    )

    def test_historical_outbox_docs_are_superseded_by_package_transport_notes(self) -> None:
        for relative_path in self.historical_outbox_docs:
            text = (ROOT / relative_path).read_text(encoding='utf-8')
            with self.subTest(path=relative_path):
                self.assertIn('Supersession note (2026-06-25):', text)
                self.assertIn('django-celery-outbox package transport', text)
                self.assertIn('Celery task `.delay(...)`', text)

    def test_hook_event_plan_queues_observation_ids_through_package_transport(self) -> None:
        plan = (ROOT / 'docs/superpowers/plans/2026-06-25-hook-event-coverage.md').read_text(
            encoding='utf-8',
        )

        self.assertIn("assert queued.task_name == 'engram.memory.process_observation_recorded'", plan)
        self.assertIn("assert queued.args == [body['observation_id']]", plan)
        self.assertNotIn('process_observation_recorded_outbox', plan)
        self.assertNotIn("body['outbox_event_id']", plan)

    def test_live_evidence_names_package_transport_not_custom_domain_outbox(self) -> None:
        verification = (ROOT / 'docs/verification-matrix.md').read_text(encoding='utf-8')
        security_rollup = (
            ROOT / 'docs/security/reviews/2026-06-25-first-parity-gate-rollup.md'
        ).read_text(encoding='utf-8')
        architecture = (ROOT / 'docs/architecture.md').read_text(encoding='utf-8')
        backend_contracts = (ROOT / 'docs/backend-contracts.md').read_text(encoding='utf-8')
        operations = (ROOT / 'docs/operations-and-deployment.md').read_text(encoding='utf-8')

        for name, text in {
            'verification matrix': verification,
            'security roll-up': security_rollup,
            'architecture': architecture,
            'backend contracts': backend_contracts,
            'operations': operations,
        }.items():
            with self.subTest(document=name):
                self.assertIn('django-celery-outbox', text)
                self.assertNotIn('engram_process_observation_outbox', text)


if __name__ == '__main__':
    unittest.main()
