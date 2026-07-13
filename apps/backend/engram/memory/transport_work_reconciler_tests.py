from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox, CeleryOutboxDeadLetter

from engram.core.models import (
    AgentSession,
    Organization,
    Project,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory import transport_work_reconciler, work_execution
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import ended_session_work

Scope = tuple[Organization, Project, AgentSession]

_DISTILL_TASK = 'engram.memory.distill_session_work_v1'
_SESSION_LEASE = timedelta(seconds=720)


def _queued_run(work: WorkflowWork, *, dispatched_at: object) -> WorkflowRun:
    return WorkflowRun.objects.create(
        organization_id=work.organization_id,
        project_id=work.project_id,
        team_id=work.team_id,
        work=work,
        run_type=work.work_type,
        status=WorkflowRunStatus.QUEUED,
        execution_contract_version=1,
        origin=WorkflowRunOrigin.RECONCILIATION,
        dispatched_at=dispatched_at,
        input_snapshot=work.input_snapshot,
    )


def _settle(work: WorkflowWork) -> None:
    now = timezone.now()
    result = work_execution.claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:settle:{uuid.uuid4()}',
        now=now,
        lease_for=_SESSION_LEASE,
    )
    work_execution.finish_work_claim(claim=result.claim, now=now, completion='product_succeeded')


def _dead_letter(
    *,
    task_name: str,
    work_id: object,
    run_id: object | None = None,
    args: list[str] | None = None,
) -> CeleryOutboxDeadLetter:
    task_id = f'workflow-work:{work_id}'
    resolved_args = [str(work_id)]
    if run_id is not None:
        task_id = f'{task_id}:run:{run_id}'
        resolved_args = [str(work_id), str(run_id)]
    if args is not None:
        resolved_args = args

    now = timezone.now()

    return CeleryOutboxDeadLetter.objects.create(
        task_id=task_id,
        task_name=task_name,
        args=resolved_args,
        kwargs={},
        created_at=now,
        dead_at=now,
        failure_reason='provider secret leaked into transport failure reason',
    )


def _inspect(scope: Scope, *, as_of: object) -> list[object]:
    organization, project, _session = scope

    return list(
        transport_work_reconciler.inspect_transport_work(
            organization_id=organization.id,
            project_id=project.id,
            as_of=as_of,
        )
    )


def _one(findings: list[object], code: str) -> object:
    matches = [finding for finding in findings if finding.code == code]
    assert len(matches) == 1, f'expected exactly one {code!r}, got {[f.code for f in findings]}'

    return matches[0]


@pytest.mark.django_db
def test_unsatisfied_work_resolves_stable_ids_without_copying_package_state() -> None:
    scope = create_scope('transport-unsatisfied-work')
    work = ended_session_work(scope, sequence=1)
    _dead_letter(task_name=_DISTILL_TASK, work_id=work.id)
    dead_before = CeleryOutboxDeadLetter.objects.count()
    outbox_before = CeleryOutbox.objects.count()
    runs_before = WorkflowRun.objects.count()

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'dead_letter_unsatisfied_work')
    assert finding.work_id == work.id
    assert finding.workflow_run_id is None
    assert CeleryOutboxDeadLetter.objects.count() == dead_before
    assert CeleryOutbox.objects.count() == outbox_before
    assert WorkflowRun.objects.count() == runs_before


@pytest.mark.django_db
def test_active_attempt_reports_unsatisfied_attempt() -> None:
    scope = create_scope('transport-unsatisfied-attempt')
    now = timezone.now()
    work = ended_session_work(scope, sequence=1)
    run = _queued_run(work, dispatched_at=now)
    _dead_letter(task_name=_DISTILL_TASK, work_id=work.id, run_id=run.id)

    findings = _inspect(scope, as_of=now)

    finding = _one(findings, 'dead_letter_unsatisfied_attempt')
    assert finding.work_id == work.id
    assert finding.workflow_run_id == run.id


@pytest.mark.django_db
def test_terminal_work_and_run_reports_already_satisfied_informational() -> None:
    scope = create_scope('transport-already-satisfied')
    work = ended_session_work(scope, sequence=1)
    _settle(work)
    succeeded = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.SUCCEEDED).first()
    _dead_letter(task_name=_DISTILL_TASK, work_id=work.id, run_id=succeeded.id)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'dead_letter_already_satisfied')
    assert finding.work_id == work.id
    assert finding.auto_repair_eligible is False


@pytest.mark.django_db
def test_non_allowlisted_task_name_is_omitted() -> None:
    scope = create_scope('transport-non-allowlisted')
    work = ended_session_work(scope, sequence=1)
    _dead_letter(task_name='engram.memory.some_legacy_task', work_id=work.id)

    assert _inspect(scope, as_of=timezone.now()) == []


@pytest.mark.django_db
def test_foreign_scope_row_is_unattributable_and_omitted() -> None:
    owned = create_scope('transport-owned')
    foreign = create_scope('transport-foreign')
    foreign_work = ended_session_work(foreign, sequence=1)
    _dead_letter(task_name=_DISTILL_TASK, work_id=foreign_work.id)

    assert _inspect(owned, as_of=timezone.now()) == []


@pytest.mark.django_db
def test_malformed_args_report_payload_invalid_without_echoing_args() -> None:
    scope = create_scope('transport-payload-invalid')
    work = ended_session_work(scope, sequence=1)
    now = timezone.now()
    dead_letter = CeleryOutboxDeadLetter.objects.create(
        task_id=f'workflow-work:{work.id}',
        task_name=_DISTILL_TASK,
        args=['not-a-valid-uuid'],
        kwargs={},
        created_at=now,
        dead_at=now,
        failure_reason='never parsed',
    )

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'dead_letter_payload_invalid')
    assert finding.work_id == work.id
    assert 'not-a-valid-uuid' not in repr(finding)
    assert not hasattr(finding, 'args')
    assert dead_letter.id is not None


@pytest.mark.django_db
def test_inspector_never_writes_package_or_domain_rows() -> None:
    scope = create_scope('transport-read-only')
    work = ended_session_work(scope, sequence=1)
    _dead_letter(task_name=_DISTILL_TASK, work_id=work.id)
    runs_before = WorkflowRun.objects.count()

    with CaptureQueriesContext(connection) as queries:
        findings = _inspect(scope, as_of=timezone.now())

    assert findings
    writes = [
        entry['sql']
        for entry in queries.captured_queries
        if entry['sql'].strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert writes == []
    assert WorkflowRun.objects.count() == runs_before


@pytest.mark.django_db
def test_dead_letter_query_never_selects_failure_reason_column() -> None:
    scope = create_scope('transport-no-failure-reason')
    work = ended_session_work(scope, sequence=1)
    _dead_letter(task_name=_DISTILL_TASK, work_id=work.id)

    with CaptureQueriesContext(connection) as queries:
        _inspect(scope, as_of=timezone.now())

    dead_letter_reads = [
        entry['sql'] for entry in queries.captured_queries if 'celery_outbox_dead_letter' in entry['sql']
    ]
    assert dead_letter_reads
    assert all('failure_reason' not in sql for sql in dead_letter_reads)
