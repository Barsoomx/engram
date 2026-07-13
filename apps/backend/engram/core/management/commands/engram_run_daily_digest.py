from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from engram.core.models import Project, WorkflowWorkDisposition
from engram.memory.digest_scheduler import (
    daily_bucket,
    daily_window_days_default,
    daily_window_days_max,
    digest_max_sources,
    schedule_daily_project,
)


class Command(BaseCommand):
    help = 'Create daily digest work for every project with a frozen closed window.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            '--window-days',
            type=int,
            default=None,
        )

    def _resolve_window_days(self, override: int | None) -> int:
        if override is None:
            return daily_window_days_default()
        if override < 0 or override > daily_window_days_max():
            raise CommandError(f'--window-days must be between 0 and {daily_window_days_max()}')

        return override

    def handle(self, *args: Any, **options: Any) -> None:
        window_days = self._resolve_window_days(options['window_days'])
        bucket = daily_bucket(as_of=timezone.now(), window_days=window_days)
        max_sources = digest_max_sources()

        scheduled_projects = 0
        no_input_projects = 0

        for project in Project.objects.order_by('id'):
            result = schedule_daily_project(
                project_id=project.id,
                bucket=bucket,
                max_sources=max_sources,
            )
            if result.disposition == WorkflowWorkDisposition.REQUIRED:
                scheduled_projects += 1
            else:
                no_input_projects += 1

        self.stdout.write(
            f'scheduled_projects={scheduled_projects} no_input_projects={no_input_projects}',
        )
