from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from engram.imports.services import ClaudeMemImporter, ClaudeMemImportError, ClaudeMemImportInput


class Command(BaseCommand):
    help = 'Import useful claude-mem memory artifacts into an existing Engram scope.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('source_root')
        parser.add_argument('--organization-id', required=True)
        parser.add_argument('--project-id', required=True)
        parser.add_argument('--team-id')
        parser.add_argument('--source-store-id', required=True)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        apply_import = self._apply_import(options)
        try:
            report = ClaudeMemImporter().execute(
                ClaudeMemImportInput(
                    source_root=Path(str(options['source_root'])),
                    organization_id=self._uuid_option(options, 'organization_id'),
                    project_id=self._uuid_option(options, 'project_id'),
                    team_id=self._optional_uuid_option(options, 'team_id'),
                    source_store_id=str(options['source_store_id']),
                    apply=apply_import,
                ),
            )
        except ClaudeMemImportError as error:
            raise CommandError(str(error)) from error

        if options['as_json']:
            self.stdout.write(json.dumps(report, sort_keys=True))

            return

        self.stdout.write(f'mode={report["mode"]}')
        self.stdout.write(f'source_store_id={report["source"]["source_store_id"]}')
        self.stdout.write(f'redacted={report["redactions"]["redacted"]}')
        for section in ('created', 'duplicates'):
            values = report[section]
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                self.stdout.write(f'{section}.{key}={value}')

    def _apply_import(self, options: dict[str, Any]) -> bool:
        if options['dry_run'] and options['apply']:
            raise CommandError('Pass only one of --dry-run or --apply')

        return bool(options['apply'])

    def _uuid_option(self, options: dict[str, Any], key: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(options[key]))
        except ValueError as error:
            option_name = key.replace('_', '-')
            raise CommandError(f'Invalid --{option_name}: {options[key]}') from error

    def _optional_uuid_option(self, options: dict[str, Any], key: str) -> uuid.UUID | None:
        if not options.get(key):
            return None

        return self._uuid_option(options, key)
