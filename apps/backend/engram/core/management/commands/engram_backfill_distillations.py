from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from engram.memory.distillation_backfill import (
    DEFAULT_FAILURE_CODES,
    redrive_target,
    select_targets,
)

logger = structlog.get_logger(__name__)

_SKIP_ALREADY_REDISPATCHED = 'already_redispatched'


class Command(BaseCommand):
    help = (
        'Re-drive v1 SESSION_DISTILLATION works whose latest run failed with a target code '
        'by resetting execution-state bookkeeping and queuing a fresh attempt.'
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--failure-codes', type=str, default=','.join(DEFAULT_FAILURE_CODES), dest='failure_codes')
        parser.add_argument('--limit', type=int, default=100, dest='limit')
        parser.add_argument('--sleep', type=float, default=0.0, dest='sleep')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument('--organization', type=str, default=None, dest='organization_id')
        parser.add_argument('--project', type=str, default=None, dest='project_id')

    def handle(self, *args: Any, **options: Any) -> None:
        codes = tuple(code.strip() for code in options['failure_codes'].split(',') if code.strip())
        if not codes:
            raise CommandError('at least one failure code is required')

        limit = options['limit']
        sleep = options['sleep']
        dry_run = options['dry_run']
        organization_id = uuid.UUID(options['organization_id']) if options['organization_id'] else None
        project_id = uuid.UUID(options['project_id']) if options['project_id'] else None

        logger.info(
            'distill_backfill_started',
            failure_codes=list(codes),
            limit=limit,
            dry_run=dry_run,
            organization_id=str(organization_id) if organization_id else '',
            project_id=str(project_id) if project_id else '',
        )

        targets = select_targets(
            failure_codes=codes,
            limit=limit,
            organization_id=organization_id,
            project_id=project_id,
        )

        if dry_run:
            for target in targets:
                self.stdout.write(
                    f'work={target.work_id} session={target.session_id} '
                    f'state={target.execution_state} code={target.failure_code} '
                    f'latest_run={target.latest_run_id}'
                )
            self.stdout.write(f'selected={len(targets)} dispatched=0 skipped=0 dry_run=1')
            logger.info(
                'distill_backfill_summary',
                selected=len(targets),
                dispatched=0,
                skipped=0,
                dry_run=True,
            )

            return

        dispatched: list[uuid.UUID] = []
        skipped: list[tuple[uuid.UUID, str]] = []
        for index, target in enumerate(targets):
            if sleep > 0 and index > 0:
                time.sleep(sleep)

            run_id = redrive_target(work_id=target.work_id, failure_codes=codes, now=timezone.now())
            if run_id is None:
                skipped.append((target.work_id, _SKIP_ALREADY_REDISPATCHED))

                continue

            dispatched.append(run_id)

        self.stdout.write(f'selected={len(targets)} dispatched={len(dispatched)} skipped={len(skipped)}')
        for work_id, reason in skipped:
            self.stdout.write(f'skipped work={work_id} reason={reason}')

        logger.info(
            'distill_backfill_summary',
            selected=len(targets),
            dispatched=len(dispatched),
            skipped=len(skipped),
            dry_run=False,
        )
