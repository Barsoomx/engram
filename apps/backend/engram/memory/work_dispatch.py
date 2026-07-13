from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from django.db import transaction

from engram.celery_app import app
from engram.core.models import (
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkType,
)

RESIGNAL_WINDOW = timedelta(minutes=5)
_RESIGNAL_WINDOW = RESIGNAL_WINDOW

_TASK_NAME_BY_WORK = {
    WorkflowWorkType.OBSERVATION_PROCESSING: 'engram.memory.process_observation_work_v1',
    WorkflowWorkType.SESSION_DISTILLATION: 'engram.memory.distill_session_work_v1',
    WorkflowWorkType.DAILY_DIGEST: 'engram.memory.generate_daily_digest_work_v1',
    WorkflowWorkType.WEEKLY_DIGEST: 'engram.memory.generate_weekly_digest_work_v1',
}


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')

    return


def work_task_signature(
    work_id: uuid.UUID,
    workflow_run_id: uuid.UUID | None = None,
) -> tuple[list[str], str]:
    args = [str(work_id)]
    task_id = f'workflow-work:{work_id}'
    if workflow_run_id is not None:
        args.append(str(workflow_run_id))
        task_id = f'{task_id}:run:{workflow_run_id}'

    return args, task_id


def _signal_package(task_name: str, work_id: uuid.UUID, run_id: uuid.UUID) -> None:
    args, task_id = work_task_signature(work_id, run_id)
    app.send_task(
        task_name,
        args=args,
        kwargs={},
        task_id=task_id,
    )

    return


def _eligible_queued_run(work: WorkflowWork) -> WorkflowRun | None:
    return (
        WorkflowRun.objects.select_for_update()
        .filter(
            work_id=work.id,
            execution_contract_version=1,
            status=WorkflowRunStatus.QUEUED,
        )
        .order_by('created_at', 'id')
        .first()
    )


def queue_work_attempt(*, work_id: uuid.UUID, now: datetime, origin: str) -> WorkflowRun:
    _require_aware(now)

    with transaction.atomic():
        try:
            work = WorkflowWork.objects.select_for_update().get(id=work_id)
        except WorkflowWork.DoesNotExist as error:
            raise ValueError('workflow work is outside the declared scope') from error

        task_name = _TASK_NAME_BY_WORK[work.work_type]
        existing = _eligible_queued_run(work)

        if existing is not None:
            if existing.dispatched_at is not None and now - existing.dispatched_at < _RESIGNAL_WINDOW:
                return existing

            existing.dispatched_at = now
            existing.save(update_fields=['dispatched_at', 'updated_at'])
            _signal_package(task_name, work.id, existing.id)

            return existing

        run = WorkflowRun.objects.create(
            organization_id=work.organization_id,
            project_id=work.project_id,
            team_id=work.team_id,
            work=work,
            run_type=work.work_type,
            status=WorkflowRunStatus.QUEUED,
            execution_contract_version=1,
            origin=origin,
            dispatched_at=now,
            input_snapshot=work.input_snapshot,
        )
        _signal_package(task_name, work.id, run.id)

        return run
