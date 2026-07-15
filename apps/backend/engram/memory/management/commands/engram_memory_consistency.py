from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from engram.memory.consistency import ConsistencyReportInput, MemoryConsistencyReporter


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
    help = 'Report one project memory consistency page without mutating state.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization', required=True)
        parser.add_argument('--project', required=True)
        parser.add_argument('--as-of', default=None)
        parser.add_argument('--after-id', default=None)
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--format', choices=('text', 'json'), default='text')

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = _uuid(options['organization'], option='--organization')
        project_id = _uuid(options['project'], option='--project')
        after_id = _uuid(options['after_id'], option='--after-id') if options['after_id'] is not None else None
        try:
            report = MemoryConsistencyReporter().execute(
                ConsistencyReportInput(
                    organization_id=organization_id,
                    project_id=project_id,
                    as_of=_as_of(options['as_of']),
                    after_id=after_id,
                    sample_limit=options['limit'],
                )
            )
        except ValueError as error:
            raise CommandError(str(error)) from error

        payload = {
            'organization_id': str(report.organization_id),
            'project_id': str(report.project_id),
            'as_of': report.as_of.isoformat(),
            'scanned': report.scanned,
            'issue_count': len(report.issues),
            'counts_by_code': [[code, count] for code, count in report.counts_by_code],
            'issues': [
                {
                    'memory_id': str(issue.memory_id),
                    'code': issue.code,
                    'classification': issue.classification,
                }
                for issue in report.issues
            ],
            'next_after_id': str(report.next_after_id) if report.next_after_id is not None else None,
        }
        if options['format'] == 'json':
            self.stdout.write(json.dumps(payload, sort_keys=True, separators=(',', ':')))

            return
        self.stdout.write(f'organization_id={payload["organization_id"]}')
        self.stdout.write(f'project_id={payload["project_id"]}')
        self.stdout.write(f'as_of={payload["as_of"]}')
        self.stdout.write(f'scanned={report.scanned}')
        self.stdout.write(f'issue_count={len(report.issues)}')
        for code, count in report.counts_by_code:
            self.stdout.write(f'count.{code}={count}')
        for issue in report.issues:
            self.stdout.write(f'issue={issue.memory_id} {issue.code} {issue.classification}')
        self.stdout.write(f'next_after_id={payload["next_after_id"] or ""}')

        return
