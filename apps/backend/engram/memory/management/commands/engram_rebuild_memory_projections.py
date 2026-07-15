from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from engram.memory.consistency import RebuildMemoryProjections, RebuildProjectionInput


def _as_of(raw: object) -> datetime:
    if raw is None:
        return timezone.now()
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError as error:
            raise CommandError('--as-of must be a valid ISO 8601 timestamp') from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise CommandError('--as-of must be timezone-aware')

    return value


def _uuid(value: object, *, option: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise CommandError(f'{option} must be a UUID') from error


class Command(BaseCommand):
    help = 'Reconcile one deterministic page of exact or embedding memory projections.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization', required=True)
        parser.add_argument('--project', required=True)
        parser.add_argument('--kind', choices=('exact', 'embedding'), required=True)
        parser.add_argument('--as-of', default=None)
        parser.add_argument('--after-id', default=None)
        parser.add_argument('--batch-size', type=int, default=200)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_false', dest='apply')
        mode.add_argument('--apply', action='store_true', dest='apply')
        parser.set_defaults(apply=False)
        parser.add_argument('--format', choices=('text', 'json'), default='text')

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = _uuid(options['organization'], option='--organization')
        project_id = _uuid(options['project'], option='--project')
        after_id = _uuid(options['after_id'], option='--after-id') if options['after_id'] is not None else None
        try:
            result = RebuildMemoryProjections().execute(
                RebuildProjectionInput(
                    organization_id=organization_id,
                    project_id=project_id,
                    as_of=_as_of(options['as_of']),
                    kind=options['kind'],
                    apply=bool(options['apply']),
                    after_id=after_id,
                    batch_size=options['batch_size'],
                )
            )
        except ValueError as error:
            raise CommandError(str(error)) from error

        payload = {
            'organization_id': str(result.organization_id),
            'project_id': str(result.project_id),
            'as_of': result.as_of.isoformat(),
            'kind': result.kind,
            'apply': result.apply,
            'scanned': result.scanned,
            'changed': result.changed,
            'skipped': result.skipped,
            'next_after_id': str(result.next_after_id) if result.next_after_id is not None else None,
        }
        if options['format'] == 'json':
            self.stdout.write(json.dumps(payload, sort_keys=True, separators=(',', ':')))

            return
        self.stdout.write(f'organization_id={payload["organization_id"]}')
        self.stdout.write(f'project_id={payload["project_id"]}')
        self.stdout.write(f'as_of={payload["as_of"]}')
        self.stdout.write(f'kind={result.kind}')
        self.stdout.write(f'apply={str(result.apply).lower()}')
        self.stdout.write(f'scanned={result.scanned}')
        self.stdout.write(f'changed={result.changed}')
        self.stdout.write(f'skipped={result.skipped}')
        self.stdout.write(f'next_after_id={payload["next_after_id"] or ""}')

        return
