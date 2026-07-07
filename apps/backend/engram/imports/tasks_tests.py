from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from engram import celeryconfig
from engram.core.models import Organization, Project
from engram.imports.models import ImportJob, ImportJobStatus
from engram.imports.services import empty_batch_report
from engram.imports.tasks import expire_stale_import_jobs


def test_task_routes_send_expire_stale_import_jobs_to_batch_queue() -> None:
    route = celeryconfig.task_routes['engram.imports.expire_stale_import_jobs']

    assert route['queue'] == celeryconfig.QUEUE_BATCH


def test_beat_schedule_registers_expire_stale_import_jobs() -> None:
    entry = celeryconfig.beat_schedule['expire-stale-import-jobs']

    assert entry['task'] == 'engram.imports.expire_stale_import_jobs'
    assert entry['schedule'] == timedelta(minutes=30)
    assert entry['options'] == {'queue': celeryconfig.QUEUE_BATCH}


@pytest.mark.django_db
def test_expire_stale_import_jobs_task_expires_stale_jobs() -> None:
    organization = Organization.objects.create(name='Task Org', slug='import-task-org')
    project = Project.objects.create(organization=organization, name='Task Project', slug='import-task-project')
    job = ImportJob.objects.create(
        organization=organization,
        project=project,
        source_store_id='task-store',
        status=ImportJobStatus.RECEIVING,
        report=empty_batch_report(),
    )
    ImportJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(hours=25))

    result = expire_stale_import_jobs()

    job.refresh_from_db()
    assert result == {'expired': 1}
    assert job.status == ImportJobStatus.EXPIRED
