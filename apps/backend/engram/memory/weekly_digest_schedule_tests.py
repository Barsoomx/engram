from __future__ import annotations

from unittest import mock

import pytest
from celery.schedules import crontab
from django.utils import timezone

from engram.celeryconfig import beat_schedule
from engram.core.models import (
    Memory,
    MemoryStatus,
    Organization,
    Project,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.tasks import generate_weekly_digest, run_scheduled_weekly_digests


def test_weekly_beat_schedule_is_registered() -> None:
    assert 'weekly-digest' in beat_schedule

    entry = beat_schedule['weekly-digest']

    assert entry['task'] == 'engram.memory.run_scheduled_weekly_digests'

    assert isinstance(entry['schedule'], crontab)


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Weekly Schedule Org', slug='weekly-schedule-org')


@pytest.fixture
def f_project_with_memory(f_org: Organization) -> Project:
    project = Project.objects.create(
        organization=f_org,
        name='project-with-memory',
        slug='project-with-memory',
    )
    Memory.objects.create(
        organization=f_org,
        project=project,
        title='approved-mem',
        body='body',
        status=MemoryStatus.APPROVED,
        updated_at=timezone.now(),
    )

    return project


@pytest.fixture
def f_project_empty(f_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_org,
        name='project-empty',
        slug='project-empty',
    )


@pytest.fixture
def f_project_digest_only(f_org: Organization) -> Project:
    project = Project.objects.create(
        organization=f_org,
        name='project-digest-only',
        slug='project-digest-only',
    )
    Memory.objects.create(
        organization=f_org,
        project=project,
        title='digest-mem',
        body='body',
        status=MemoryStatus.APPROVED,
        updated_at=timezone.now(),
        metadata={'kind': 'digest'},
    )

    return project


@pytest.mark.django_db
def test_run_scheduled_weekly_digests_enqueues_per_project_with_memories(
    f_project_with_memory: Project,
    f_project_empty: Project,
) -> None:
    with mock.patch.object(generate_weekly_digest, 'delay') as m_delay:
        run_scheduled_weekly_digests()

    m_delay.assert_called_once_with(
        str(f_project_with_memory.organization_id),
        str(f_project_with_memory.id),
    )


@pytest.mark.django_db
def test_run_scheduled_weekly_digests_skips_digest_kind_only_project(
    f_project_with_memory: Project,
    f_project_digest_only: Project,
) -> None:
    with mock.patch.object(generate_weekly_digest, 'delay') as m_delay:
        run_scheduled_weekly_digests()

    m_delay.assert_called_once_with(
        str(f_project_with_memory.organization_id),
        str(f_project_with_memory.id),
    )


@pytest.mark.django_db
def test_generate_weekly_digest_builds_digest_and_returns_memory_id(
    f_project_with_memory: Project,
) -> None:
    org_id = str(f_project_with_memory.organization_id)
    project_id = str(f_project_with_memory.id)

    result_id = generate_weekly_digest.run(org_id, project_id)

    assert result_id is not None

    run = WorkflowRun.objects.filter(
        organization_id=f_project_with_memory.organization_id,
        project=f_project_with_memory,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        status=WorkflowRunStatus.SUCCEEDED,
    ).first()

    assert run is not None

    assert str(run.result_memory_id) == result_id
