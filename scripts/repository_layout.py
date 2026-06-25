from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


REQUIRED_PATHS: tuple[str, ...] = (
    'apps/backend/README.md',
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
    'apps/backend/engram/health/views.py',
    'apps/backend/Dockerfile',
    '.github/workflows/compose-e2e.yml',
    'apps/frontend/README.md',
    'deploy/compose/.env.example',
    'deploy/compose/docker-compose.yml',
    'scripts/e2e_golden_path.py',
    'packages/cli/README.md',
    'packages/cli/pyproject.toml',
    'packages/cli/engram_cli/__init__.py',
    'packages/cli/engram_cli/__main__.py',
    'packages/cli/engram_cli/main.py',
    'packages/cli/engram_cli/commands.py',
    'packages/cli/engram_cli/config.py',
    'packages/cli/engram_cli/http.py',
    'packages/cli/engram_cli/cli_lifecycle_tests.py',
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
