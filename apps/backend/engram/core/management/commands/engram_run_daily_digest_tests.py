from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import WorkflowWork, WorkflowWorkDisposition, WorkflowWorkType
from engram.memory.daily_digest_tests import create_approved_memory, create_organization_project_team
from engram.memory.tasks import run_scheduled_digests

_DAILY_WORK_TASK_NAME = 'engram.memory.generate_daily_digest_work_v1'


@pytest.mark.django_db
def test_command_creates_id_only_digest_work_for_recent_project() -> None:
    organization, team, project = create_organization_project_team(slug='cmd-alpha')
    memory = create_approved_memory(organization, project, team, title='Alpha source')

    call_command('engram_run_daily_digest')

    outbox = CeleryOutbox.objects.filter(task_name=_DAILY_WORK_TASK_NAME)
    assert outbox.count() == 1
    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.DAILY_DIGEST)
    assert outbox.first().args == [str(work.id)]
    assert outbox.first().kwargs == {}
    assert str(memory.id) not in repr(outbox.first().args)


@pytest.mark.django_db
def test_command_empty_project_creates_no_input_terminal_without_signal() -> None:
    create_organization_project_team(slug='cmd-beta')

    call_command('engram_run_daily_digest')

    assert not CeleryOutbox.objects.filter(task_name=_DAILY_WORK_TASK_NAME).exists()
    assert WorkflowWork.objects.get().disposition == WorkflowWorkDisposition.NO_OP


@pytest.mark.django_db
@pytest.mark.parametrize('order', ('command_first', 'task_first'))
def test_command_and_scheduled_task_converge_in_either_order(order: str) -> None:
    organization, team, project = create_organization_project_team(slug=f'cmd-converge-{order}')
    create_approved_memory(organization, project, team, title='Converge source')

    if order == 'command_first':
        call_command('engram_run_daily_digest')
        run_scheduled_digests()
    else:
        run_scheduled_digests()
        call_command('engram_run_daily_digest')

    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1


@pytest.mark.django_db
def test_command_window_days_override_cannot_rewrite_frozen_winner() -> None:
    organization, team, project = create_organization_project_team(slug='cmd-window')
    create_approved_memory(organization, project, team, title='Window source')

    call_command('engram_run_daily_digest', '--window-days', '1')
    original_snapshot = WorkflowWork.objects.get().input_snapshot

    call_command('engram_run_daily_digest', '--window-days', '3')

    assert WorkflowWork.objects.count() == 1
    assert CeleryOutbox.objects.count() == 1
    assert WorkflowWork.objects.get().input_snapshot == original_snapshot


@pytest.mark.django_db
@pytest.mark.parametrize('override', ('999', '-1'))
def test_command_rejects_out_of_range_window_days(override: str) -> None:
    create_organization_project_team(slug=f'cmd-invalid-{override.lstrip("-")}')

    with pytest.raises(CommandError):
        call_command('engram_run_daily_digest', '--window-days', override)
