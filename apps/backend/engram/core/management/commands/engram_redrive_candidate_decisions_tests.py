from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkExecutionState,
)
from engram.memory.work_backfill_tests import (
    _STREAK_LIMIT,
    _UNEXPECTED_STEP,
    _decision_work,
    _fail_decision_work,
)

_DECISION_TASK = 'engram.memory.process_candidate_decision_work_v1'
_HISTORIC_CODE = 'unexpected_exception'


def _terminal_decision_work(suffix: str) -> WorkflowWork:
    work, seed = _decision_work(suffix)
    _fail_decision_work(
        work,
        seed_run=seed,
        code=_HISTORIC_CODE,
        failure_class='unexpected',
        times=_STREAK_LIMIT,
        now=timezone.now(),
        step=_UNEXPECTED_STEP,
    )

    return work


def _queued_count(work: WorkflowWork) -> int:
    return WorkflowRun.objects.filter(
        work_id=work.id,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    ).count()


@pytest.mark.django_db
def test_dry_run_prints_selection_without_state_change() -> None:
    work = _terminal_decision_work('cmd-cd-dry')
    state_before = WorkflowWork.objects.get(id=work.id).execution_state
    CeleryOutbox.objects.all().delete()
    out = StringIO()

    call_command(
        'engram_redrive_candidate_decisions',
        '--dry-run',
        '--failure-codes',
        _HISTORIC_CODE,
        stdout=out,
    )

    output = out.getvalue()
    assert f'work={work.id}' in output
    assert f'candidate={work.subject_id}' in output
    assert 'selected=1 dispatched=0 skipped=0 dry_run=1' in output
    assert WorkflowWork.objects.get(id=work.id).execution_state == state_before
    assert _queued_count(work) == 0
    assert CeleryOutbox.objects.filter(task_name=_DECISION_TASK).count() == 0


@pytest.mark.django_db
def test_command_revives_terminal_works() -> None:
    work_a = _terminal_decision_work('cmd-cd-a')
    work_b = _terminal_decision_work('cmd-cd-b')
    assert WorkflowWork.objects.get(id=work_a.id).execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    out = StringIO()

    call_command('engram_redrive_candidate_decisions', '--failure-codes', _HISTORIC_CODE, stdout=out)

    assert 'selected=2 dispatched=2 skipped=0' in out.getvalue()
    assert _queued_count(work_a) == 1
    assert _queued_count(work_b) == 1
    assert WorkflowWork.objects.get(id=work_a.id).execution_state == WorkflowWorkExecutionState.READY


@pytest.mark.django_db
def test_default_codes_do_not_match_historic_failures() -> None:
    _terminal_decision_work('cmd-cd-default')
    out = StringIO()

    call_command('engram_redrive_candidate_decisions', '--dry-run', stdout=out)

    assert 'selected=0 dispatched=0 skipped=0 dry_run=1' in out.getvalue()


@pytest.mark.django_db
def test_limit_bounds_selection() -> None:
    _terminal_decision_work('cmd-cd-limit-a')
    _terminal_decision_work('cmd-cd-limit-b')
    out = StringIO()

    call_command(
        'engram_redrive_candidate_decisions',
        '--dry-run',
        '--limit',
        '1',
        '--failure-codes',
        _HISTORIC_CODE,
        stdout=out,
    )

    assert 'selected=1 dispatched=0 skipped=0 dry_run=1' in out.getvalue()


@pytest.mark.django_db
def test_empty_failure_codes_rejected() -> None:
    with pytest.raises(CommandError):
        call_command('engram_redrive_candidate_decisions', '--failure-codes', ' , ')
