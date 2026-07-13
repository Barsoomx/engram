from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from engram.core.models import Project
from engram.memory.work_reconciliation import (
    ReconciliationFinding,
    ReconciliationReport,
    build_reconciliation_report,
)

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = 'Report scoped work-reconciliation findings without mutating any row.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--organization-id', type=uuid.UUID, required=True)
        parser.add_argument('--project-id', type=uuid.UUID, required=True)
        parser.add_argument('--as-of', type=str, default=None)
        parser.add_argument('--format', choices=('text', 'json'), default='text')

    def _resolve_as_of(self, raw: str | None) -> datetime:
        if raw is None:
            return timezone.now()

        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as error:
            raise CommandError('--as-of must be a valid ISO 8601 timestamp') from error

        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CommandError('--as-of must be timezone-aware')

        return parsed

    def _resolve_scope(self, organization_id: uuid.UUID, project_id: uuid.UUID) -> None:
        exists = Project.objects.filter(id=project_id, organization_id=organization_id).exists()
        if not exists:
            raise CommandError(f'project {project_id} does not belong to organization {organization_id}')

        return

    def handle(self, *args: Any, **options: Any) -> None:
        organization_id = options['organization_id']
        project_id = options['project_id']
        as_of = self._resolve_as_of(options['as_of'])
        self._resolve_scope(organization_id, project_id)

        report = build_reconciliation_report(
            organization_id=organization_id,
            project_id=project_id,
            as_of=as_of,
        )

        if options['format'] == 'json':
            self.stdout.write(json.dumps(_serialize_report(report)))
        else:
            self._write_text(report)

        logger.info(
            'work_reconciliation_audit',
            organization_id=str(organization_id),
            project_id=str(project_id),
            as_of=as_of.isoformat(),
            finding_count=len(report.findings),
            counts_by_code=[list(entry) for entry in report.counts_by_code],
        )

        return

    def _write_text(self, report: ReconciliationReport) -> None:
        self.stdout.write(f'as_of={report.as_of.isoformat()}')
        self.stdout.write(f'finding_count={len(report.findings)}')
        for code, count in report.counts_by_code:
            self.stdout.write(f'{code}={count}')

        return


def _serialize_finding(finding: ReconciliationFinding) -> dict[str, Any]:
    return {
        'invariant_id': finding.invariant_id,
        'code': finding.code,
        'organization_id': str(finding.organization_id),
        'project_id': str(finding.project_id),
        'entity_type': finding.entity_type,
        'entity_id': finding.entity_id,
        'work_id': str(finding.work_id) if finding.work_id is not None else None,
        'workflow_run_id': str(finding.workflow_run_id) if finding.workflow_run_id is not None else None,
        'observed_at': finding.observed_at.isoformat(),
        'proposed_action': finding.proposed_action,
        'auto_repair_eligible': finding.auto_repair_eligible,
    }


def _serialize_report(report: ReconciliationReport) -> dict[str, Any]:
    return {
        'organization_id': str(report.organization_id),
        'project_id': str(report.project_id),
        'as_of': report.as_of.isoformat(),
        'findings': [_serialize_finding(finding) for finding in report.findings],
        'counts_by_code': [list(entry) for entry in report.counts_by_code],
        'work_counts_by_type_state': [list(entry) for entry in report.work_counts_by_type_state],
        'oldest_age_seconds_by_code': [list(entry) for entry in report.oldest_age_seconds_by_code],
    }
