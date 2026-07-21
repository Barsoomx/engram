from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.management.commands import engram_backfill_distillations as command_module
from engram.core.models import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkExecutionState,
)
from engram.memory.distillation_backfill_tests import (
    _STREAK_LIMIT,
    SessionScope,
    _current_work,
    _fail_work,
)
from engram.memory.observation_work_tests import create_scope

_DISTILL_TASK = 'engram.memory.distill_session_work_v1'


def _malformed_work(suffix: str, *, times: int = 1) -> WorkflowWork:
    scope: SessionScope = create_scope(suffix)
    work = _current_work(scope, sequence=1)
    _fail_work(
        work,
        code='provider_output_malformed',
        failure_class='provider_transient',
        times=times,
        now=timezone.now(),
    )

    return work


def _queued_count(work: WorkflowWork) -> int:
    return WorkflowRun.objects.filter(
        work_id=work.id,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
    ).count()


@pytest.mark.django_db
def test_dry_run_prints_selection_no_state_change() -> None:
    work = _malformed_work('cmd-dry-run')
    state_before = WorkflowWork.objects.get(id=work.id).execution_state
    latest_run_id = (
        WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
        .order_by('-created_at', '-id')
        .first()
        .id
    )
    CeleryOutbox.objects.all().delete()
    out = StringIO()

    call_command('engram_backfill_distillations', '--dry-run', stdout=out)

    output = out.getvalue()
    assert f'work={work.id}' in output
    assert f'session={work.subject_id}' in output
    assert f'state={state_before}' in output
    assert 'code=provider_output_malformed' in output
    assert f'latest_run={latest_run_id}' in output
    assert 'selected=1 dispatched=0 skipped=0 dry_run=1' in output
    assert WorkflowWork.objects.get(id=work.id).execution_state == state_before
    assert _queued_count(work) == 0
    assert CeleryOutbox.objects.filter(task_name=_DISTILL_TASK).count() == 0


@pytest.mark.django_db
def test_command_dispatches_and_summary() -> None:
    work_a = _malformed_work('cmd-dispatch-a', times=_STREAK_LIMIT)
    work_b = _malformed_work('cmd-dispatch-b', times=_STREAK_LIMIT)
    assert WorkflowWork.objects.get(id=work_a.id).execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    out = StringIO()

    call_command('engram_backfill_distillations', stdout=out)

    assert 'selected=2 dispatched=2 skipped=0' in out.getvalue()
    assert _queued_count(work_a) == 1
    assert _queued_count(work_b) == 1


@pytest.mark.django_db
def test_command_limit_throttle() -> None:
    work_a = _malformed_work('cmd-limit-a')
    work_b = _malformed_work('cmd-limit-b')
    work_c = _malformed_work('cmd-limit-c')

    call_command('engram_backfill_distillations', '--limit', '1', stdout=StringIO())

    dispatched_first = [work for work in (work_a, work_b, work_c) if _queued_count(work) == 1]
    assert len(dispatched_first) == 1

    call_command('engram_backfill_distillations', '--limit', '1', stdout=StringIO())

    dispatched_second = [work for work in (work_a, work_b, work_c) if _queued_count(work) == 1]
    assert len(dispatched_second) == 2


@pytest.mark.django_db
def test_command_sleep_arg_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _malformed_work('cmd-sleep-a')
    _malformed_work('cmd-sleep-b')
    calls: list[float] = []

    def m_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(command_module.time, 'sleep', m_sleep)
    out = StringIO()

    call_command('engram_backfill_distillations', '--sleep', '2', stdout=out)

    assert 'selected=2 dispatched=2 skipped=0' in out.getvalue()
    assert calls == [2.0]


@pytest.mark.django_db
def test_command_custom_failure_codes() -> None:
    scope_truncated: SessionScope = create_scope('cmd-codes-truncated')
    work_truncated = _current_work(scope_truncated, sequence=1)
    _fail_work(
        work_truncated,
        code='provider_output_truncated',
        failure_class='provider_transient',
        times=1,
        now=timezone.now(),
    )
    work_malformed = _malformed_work('cmd-codes-malformed')
    out = StringIO()

    call_command(
        'engram_backfill_distillations',
        '--failure-codes',
        'provider_output_truncated',
        stdout=out,
    )

    assert 'selected=1 dispatched=1 skipped=0' in out.getvalue()
    assert _queued_count(work_truncated) == 1
    assert _queued_count(work_malformed) == 0


@pytest.mark.django_db
def test_command_empty_failure_codes_errors() -> None:
    work = _malformed_work('cmd-empty-codes')

    with pytest.raises(CommandError, match='at least one failure code is required'):
        call_command('engram_backfill_distillations', '--failure-codes', '', stdout=StringIO())

    assert _queued_count(work) == 0
