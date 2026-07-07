from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from engram.core.export import export_memories


class Command(BaseCommand):
    help = 'Export an organization/project approved memories to a JSON backup file.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization-id', required=True)
        parser.add_argument('--project-id', required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--team-id', required=False)
        parser.add_argument('--all-statuses', action='store_true')

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = uuid.UUID(str(options['organization_id']))
        project_id = uuid.UUID(str(options['project_id']))
        team_id = uuid.UUID(str(options['team_id'])) if options['team_id'] else None
        output_path = Path(str(options['output']))

        payload = export_memories(
            organization_id=organization_id,
            project_id=project_id,
            team_id=team_id,
            all_statuses=bool(options['all_statuses']),
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('w', encoding='utf-8') as target:
            json.dump(payload, target, indent=2, sort_keys=True)

        self.stdout.write(f'memory_count={payload["memory_count"]}')
