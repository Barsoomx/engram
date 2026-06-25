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
    'apps/backend/engram/core/models.py',
    'apps/backend/engram/core/migrations/0001_initial.py',
    'apps/backend/engram/core/migrations/0002_remove_outboxevent_core_outbox_unique_idempotency_key_per_event_and_more.py',
    'apps/backend/engram/health/views.py',
    'apps/backend/Dockerfile',
    'apps/frontend/README.md',
    'deploy/compose/.env.example',
    'deploy/compose/docker-compose.yml',
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
