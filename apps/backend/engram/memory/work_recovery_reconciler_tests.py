from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory import work_execution
from engram.memory.work_failures import PROVIDER_TRANSIENT, ClassifiedWorkFailure
from engram.memory.work_recovery_reconciler import RecoverStrandedWork
from engram.memory.workflow_work_tests import create_required_work, create_scope

_OBSERVATION_TASK = 'engram.memory.process_observation_work_v1'


def _collect_sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[object, ...]]]:
    sent: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda task_name, *, args, **_kwargs: sent.append((task_name, tuple(args))),
    )

    return sent


@pytest.mark.django_db
def test_recovers_due_retry_and_expired_lease_observation_work(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_work = create_required_work(create_scope('recover-retry'), suffix='recover-retry')
    lease_work = create_required_work(create_scope('recover-lease'), suffix='recover-lease')

    now = timezone.now()
    claimed = work_execution.claim_work(
        work_id=retry_work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='recover:retry',
        now=now,
        lease_for=timedelta(seconds=120),
    )
    work_execution.fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )
    work_execution.claim_work(
        work_id=lease_work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='recover:lease',
        now=now,
        lease_for=timedelta(seconds=1),
    )

    sent = _collect_sent(monkeypatch)
    as_of = now + timedelta(minutes=10)

    result = RecoverStrandedWork().execute(as_of=as_of)

    assert result.queued == 2
    for work in (retry_work, lease_work):
        assert (
            WorkflowRun.objects.filter(
                work=work,
                status=WorkflowRunStatus.QUEUED,
                execution_contract_version=1,
                origin=WorkflowRunOrigin.RECONCILIATION,
            ).count()
            == 1
        )
    assert len(sent) == 2
    assert {name for name, _args in sent} == {_OBSERVATION_TASK}


@pytest.mark.django_db
def test_recovery_ignores_settled_and_future_retry_work(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = create_scope('recover-noop')
    future_work = create_required_work(scope, suffix='recover-future')

    now = timezone.now()
    claimed = work_execution.claim_work(
        work_id=future_work.id,
        expected_work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        lease_owner='recover:future',
        now=now,
        lease_for=timedelta(seconds=120),
    )
    work_execution.fail_work_claim(
        claim=claimed.claim,
        now=now,
        failure=ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code='provider_timeout'),
    )
    stored = WorkflowWork.objects.get(id=future_work.id)
    assert stored.execution_state == WorkflowWorkExecutionState.RETRY_WAIT

    sent = _collect_sent(monkeypatch)

    result = RecoverStrandedWork().execute(as_of=now)

    assert result.queued == 0
    assert sent == []
