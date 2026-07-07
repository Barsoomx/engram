from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from engram.core.models import Memory, MemoryStatus, Project
from engram.memory.tasks import daily_digest_window_start, generate_daily_digest


class Command(BaseCommand):
    help = 'Enqueue daily digest tasks for every project with recent approved memories.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            '--window-days',
            type=int,
            default=None,
        )

    def handle(self, *args: Any, **options: Any) -> None:
        override_days = options['window_days']

        enqueued_projects = 0
        enqueued_memories = 0
        skipped_projects = 0

        for project in Project.objects.all():
            if override_days is None:
                window_start = daily_digest_window_start(project)
            else:
                window_start = timezone.now() - timedelta(days=int(override_days))

            memory_ids = list(
                Memory.objects.filter(
                    organization_id=project.organization_id,
                    project=project,
                    status=MemoryStatus.APPROVED,
                    updated_at__gte=window_start,
                )
                .exclude(kind='digest')
                .values_list('id', flat=True),
            )
            if not memory_ids:
                skipped_projects += 1

                continue

            generate_daily_digest.delay(
                str(project.organization_id),
                str(project.id),
                [str(value) for value in memory_ids],
            )
            enqueued_projects += 1
            enqueued_memories += len(memory_ids)

        self.stdout.write(
            f'enqueued_projects={enqueued_projects} '
            f'enqueued_memories={enqueued_memories} '
            f'skipped_projects={skipped_projects}',
        )
