from __future__ import annotations

import uuid
from datetime import datetime

from django_celery_outbox.models import CeleryOutboxDeadLetter

from engram.core.models import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
)
from engram.memory.aware_time import require_aware
from engram.memory.session_work_reconciler import SessionWorkFinding
from engram.memory.work_dispatch import ALLOWED_TASK_NAMES, parse_work_task_id

DEAD_LETTER_UNSATISFIED_WORK = 'dead_letter_unsatisfied_work'
DEAD_LETTER_UNSATISFIED_ATTEMPT = 'dead_letter_unsatisfied_attempt'
DEAD_LETTER_ALREADY_SATISFIED = 'dead_letter_already_satisfied'
DEAD_LETTER_PAYLOAD_INVALID = 'dead_letter_payload_invalid'

_ENTITY_TYPE = 'transport_dead_letter'
_PROPOSED_ACTION = 'report_only'

_ACTIVE_RUN_STATES = frozenset(
    {
        WorkflowRunStatus.QUEUED,
        WorkflowRunStatus.RUNNING,
    }
)


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_args(args: object) -> tuple[uuid.UUID | None, uuid.UUID | None, bool]:
    if not isinstance(args, list) or len(args) < 1 or len(args) > 2:
        return None, None, False

    work_uuid = _uuid_or_none(args[0])
    if work_uuid is None:
        return None, None, False

    if len(args) == 2:
        run_uuid = _uuid_or_none(args[1])
        if run_uuid is None:
            return work_uuid, None, False

        return work_uuid, run_uuid, True

    return work_uuid, None, True


def _finding(
    dead_letter: CeleryOutboxDeadLetter,
    work: WorkflowWork,
    code: str,
    *,
    run_id: uuid.UUID | None,
    as_of: datetime,
) -> SessionWorkFinding:
    return SessionWorkFinding(
        code=code,
        organization_id=work.organization_id,
        project_id=work.project_id,
        entity_type=_ENTITY_TYPE,
        entity_id=str(dead_letter.id),
        work_id=work.id,
        workflow_run_id=run_id,
        observed_at=min(dead_letter.dead_at, as_of),
        proposed_action=_PROPOSED_ACTION,
        auto_repair_eligible=False,
    )


def _classify(
    dead_letter: CeleryOutboxDeadLetter,
    work: WorkflowWork,
    run: WorkflowRun | None,
    *,
    args_valid: bool,
    as_of: datetime,
) -> SessionWorkFinding:
    if not args_valid:
        return _finding(dead_letter, work, DEAD_LETTER_PAYLOAD_INVALID, run_id=None, as_of=as_of)

    if run is not None and run.status in _ACTIVE_RUN_STATES:
        return _finding(dead_letter, work, DEAD_LETTER_UNSATISFIED_ATTEMPT, run_id=run.id, as_of=as_of)

    if work.disposition == WorkflowWorkDisposition.REQUIRED:
        return _finding(dead_letter, work, DEAD_LETTER_UNSATISFIED_WORK, run_id=None, as_of=as_of)

    run_id = run.id if run is not None else None

    return _finding(dead_letter, work, DEAD_LETTER_ALREADY_SATISFIED, run_id=run_id, as_of=as_of)


def inspect_transport_work(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> tuple[SessionWorkFinding, ...]:
    require_aware(as_of)

    dead_letters = (
        CeleryOutboxDeadLetter.objects.filter(task_name__in=ALLOWED_TASK_NAMES)
        .only('id', 'task_id', 'task_name', 'args', 'dead_at')
        .order_by('id')
    )
    parsed: list[tuple[CeleryOutboxDeadLetter, uuid.UUID | None, uuid.UUID | None, bool]] = []
    work_ids: set[uuid.UUID] = set()
    for dead_letter in dead_letters:
        task_work, task_run = parse_work_task_id(dead_letter.task_id)
        args_work, args_run, args_valid = _parse_args(dead_letter.args)
        work_uuid = task_work if task_work is not None else args_work
        run_uuid = task_run if task_run is not None else args_run
        parsed.append((dead_letter, work_uuid, run_uuid, args_valid))
        if work_uuid is not None:
            work_ids.add(work_uuid)

    works = {
        work.id: work
        for work in WorkflowWork.objects.filter(
            id__in=work_ids,
            organization_id=organization_id,
            project_id=project_id,
        )
    }
    run_ids = {
        run_uuid
        for _dead_letter, work_uuid, run_uuid, args_valid in parsed
        if args_valid and run_uuid is not None and work_uuid in works
    }
    runs = {
        run.id: run
        for run in WorkflowRun.objects.filter(
            id__in=run_ids,
            organization_id=organization_id,
            project_id=project_id,
        )
    }

    findings: list[SessionWorkFinding] = []
    for dead_letter, work_uuid, run_uuid, args_valid in parsed:
        work = works.get(work_uuid)
        if work is None:
            continue

        run = None
        if args_valid and run_uuid is not None:
            resolved = runs.get(run_uuid)
            if resolved is not None and resolved.work_id == work.id:
                run = resolved

        findings.append(_classify(dead_letter, work, run, args_valid=args_valid, as_of=as_of))

    return tuple(findings)
