from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.celeryconfig import beat_schedule, task_routes
from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    Team,
    VisibilityScope,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.memory.tasks import run_scheduled_digests

_DAILY_WORK_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'


def create_organization_project_team(slug: str = 'daily') -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name=f'{slug} org', slug=f'{slug}-org')
    team = Team.objects.create(organization=organization, name='Platform', slug=f'{slug}-platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug=f'{slug}-backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )

    return organization, team, project


def create_approved_memory(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    title: str,
    in_window: bool = True,
) -> Memory:
    body = f'{title} body detail.'
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=memory.current_version,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )
    if in_window:
        Memory.objects.filter(id=memory.id).update(updated_at=timezone.now() - timedelta(days=1))

    return memory


@pytest.mark.django_db
def test_run_scheduled_digests_returns_producer_aggregate_and_id_only_signal() -> None:
    organization, team, project = create_organization_project_team(slug='delta')
    memory = create_approved_memory(organization, project, team, title='Delta source')

    result = run_scheduled_digests()

    assert set(result) == {
        'scheduled_projects',
        'required_work',
        'no_input_projects',
        'task_enqueued',
        'failed_projects',
    }
    assert result['required_work'] == 1
    assert result['no_input_projects'] == 0
    assert result['scheduled_projects'] == 1
    assert result['task_enqueued'] == 1
    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)
    queued = CeleryOutbox.objects.get()
    assert queued.task_name == _DAILY_WORK_TASK_NAME
    assert queued.args == [str(work.id)]
    assert queued.kwargs == {}
    assert str(memory.id) not in repr(queued.args)


@pytest.mark.django_db
def test_run_scheduled_digests_empty_project_creates_no_input_terminal_without_signal() -> None:
    create_organization_project_team(slug='epsilon')

    result = run_scheduled_digests()

    assert result == {
        'scheduled_projects': 1,
        'required_work': 0,
        'no_input_projects': 1,
        'task_enqueued': 0,
        'failed_projects': 0,
    }
    assert not CeleryOutbox.objects.filter(task_name=_DAILY_WORK_TASK_NAME).exists()
    assert WorkflowWork.objects.get().disposition == WorkflowWorkDisposition.NO_OP


@pytest.mark.django_db
def test_run_scheduled_digests_excludes_digest_kind_memories() -> None:
    organization_a, team_a, project_a = create_organization_project_team(slug='zeta')
    Memory.objects.create(
        organization=organization_a,
        project=project_a,
        team=team_a,
        title='Existing digest',
        body='Existing digest body.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'kind': 'digest'},
    )
    organization_b, team_b, project_b = create_organization_project_team(slug='eta')
    normal_memory = create_approved_memory(organization_b, project_b, team_b, title='Normal source')

    result = run_scheduled_digests()

    assert result['required_work'] == 1
    assert result['no_input_projects'] == 1
    work = WorkflowWork.objects.get(project=project_b, disposition=WorkflowWorkDisposition.REQUIRED)
    queued = CeleryOutbox.objects.get()
    assert queued.args == [str(work.id)]
    assert str(normal_memory.id) not in repr(queued.args)
    assert WorkflowWork.objects.get(project=project_a).disposition == WorkflowWorkDisposition.NO_OP


@pytest.mark.django_db
def test_run_scheduled_digests_second_run_reuses_occurrence_without_second_signal() -> None:
    organization, team, project = create_organization_project_team(slug='theta')
    create_approved_memory(organization, project, team, title='Theta source')

    run_scheduled_digests()
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1

    second = run_scheduled_digests()

    assert second['task_enqueued'] == 0
    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


def test_daily_beat_and_work_routing_registered_once() -> None:
    daily_beats = [
        key for key, entry in beat_schedule.items() if entry['task'] == 'engram.memory.run_scheduled_digests'
    ]
    assert daily_beats == ['daily-digest']

    entry = beat_schedule['daily-digest']
    assert isinstance(entry['schedule'], crontab)
    assert 2 in entry['schedule'].hour
    assert 0 in entry['schedule'].minute
    assert entry['options']['queue'] == 'engram-batch'
    assert task_routes[_DAILY_WORK_TASK_NAME] == {'queue': 'engram-batch'}
