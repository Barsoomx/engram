from __future__ import annotations

from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.celeryconfig import beat_schedule, task_routes
from engram.core.models import (
    Memory,
    MemoryStatus,
    Organization,
    Project,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.digest_visibility_tests import make_source_memory
from engram.memory.tasks import generate_weekly_digest, run_scheduled_weekly_digests

_WEEKLY_WORK_TASK_NAME = 'engram.memory.generate_weekly_digest_work_v1'


def test_weekly_beat_and_work_routing_registered_once() -> None:
    weekly_beats = [
        key for key, entry in beat_schedule.items() if entry['task'] == 'engram.memory.run_scheduled_weekly_digests'
    ]
    assert weekly_beats == ['weekly-digest']

    entry = beat_schedule['weekly-digest']
    assert isinstance(entry['schedule'], crontab)
    assert 1 in entry['schedule'].day_of_week
    assert 3 in entry['schedule'].hour
    assert 0 in entry['schedule'].minute
    assert entry['options']['queue'] == 'engram-batch'
    assert task_routes[_WEEKLY_WORK_TASK_NAME] == {'queue': 'engram-batch'}


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Weekly Schedule Org', slug='weekly-schedule-org')


@pytest.fixture
def f_weekly_source_project(f_org: Organization) -> Project:
    project = Project.objects.create(organization=f_org, name='weekly-source', slug='weekly-source')
    memory = make_source_memory(f_org, project, title='approved-mem', body='body')
    Memory.objects.filter(id=memory.id).update(created_at=timezone.now() - timedelta(days=7))

    return project


@pytest.fixture
def f_project_empty(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='project-empty', slug='project-empty')


@pytest.fixture
def f_project_digest_only(f_org: Organization) -> Project:
    project = Project.objects.create(organization=f_org, name='project-digest-only', slug='project-digest-only')
    memory = Memory.objects.create(
        organization=f_org,
        project=project,
        title='digest-mem',
        body='body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'kind': 'digest'},
    )
    Memory.objects.filter(id=memory.id).update(created_at=timezone.now() - timedelta(days=7))

    return project


@pytest.mark.django_db
def test_run_scheduled_weekly_digests_returns_aggregate_and_id_only_signal(
    f_weekly_source_project: Project,
    f_project_empty: Project,
) -> None:
    result = run_scheduled_weekly_digests()

    assert set(result) == {
        'scheduled_projects',
        'required_work',
        'no_input_projects',
        'task_enqueued',
        'failed_projects',
    }
    assert result['required_work'] == 1
    assert result['no_input_projects'] == 1
    assert result['task_enqueued'] == 1
    work = WorkflowWork.objects.get(
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        disposition=WorkflowWorkDisposition.REQUIRED,
    )
    queued = CeleryOutbox.objects.get(task_name=_WEEKLY_WORK_TASK_NAME)
    assert queued.task_name == _WEEKLY_WORK_TASK_NAME
    assert queued.args == [str(work.id)]
    assert queued.kwargs == {}


@pytest.mark.django_db
def test_run_scheduled_weekly_digests_excludes_digest_only_project(
    f_weekly_source_project: Project,
    f_project_digest_only: Project,
) -> None:
    result = run_scheduled_weekly_digests()

    assert result['required_work'] == 1
    assert result['no_input_projects'] >= 1
    work = WorkflowWork.objects.get(
        project=f_weekly_source_project,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        disposition=WorkflowWorkDisposition.REQUIRED,
    )
    queued = CeleryOutbox.objects.get(task_name=_WEEKLY_WORK_TASK_NAME)
    assert queued.args == [str(work.id)]
    assert WorkflowWork.objects.get(project=f_project_digest_only).disposition == WorkflowWorkDisposition.NO_OP


@pytest.mark.django_db
def test_generate_weekly_digest_builds_digest_and_returns_memory_id(
    f_weekly_source_project: Project,
) -> None:
    org_id = str(f_weekly_source_project.organization_id)
    project_id = str(f_weekly_source_project.id)

    result_id = generate_weekly_digest.run(org_id, project_id)

    assert result_id is not None
    run = WorkflowRun.objects.filter(
        organization_id=f_weekly_source_project.organization_id,
        project=f_weekly_source_project,
        run_type=WorkflowRunType.WEEKLY_DIGEST,
        status=WorkflowRunStatus.SUCCEEDED,
    ).first()
    assert run is not None
    assert str(run.result_memory_id) == result_id
