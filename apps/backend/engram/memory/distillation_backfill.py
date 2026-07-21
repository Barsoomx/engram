from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from django.db import transaction
from django.db.models import OuterRef, Subquery

from engram.core.models import (
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.aware_time import require_aware
from engram.memory.work_dispatch import queue_work_attempt

logger = structlog.get_logger(__name__)

DEFAULT_FAILURE_CODES = (
    'provider_output_malformed',
    'provider_output_truncated',
    'provider_account_unavailable',
)

_RESET_FIELDS = (
    'execution_state',
    'failure_streak',
    'next_retry_at',
    'blocked_configuration_fingerprint',
    'lease_owner',
    'lease_expires_at',
    'heartbeat_at',
    'updated_at',
)


@dataclass(frozen=True, slots=True)
class BackfillTarget:
    work_id: uuid.UUID
    session_id: uuid.UUID
    latest_run_id: uuid.UUID
    failure_code: str
    execution_state: str


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    dispatched: tuple[uuid.UUID, ...] = ()
    skipped: tuple[tuple[uuid.UUID, str], ...] = ()


def select_targets(
    *,
    failure_codes: tuple[str, ...],
    limit: int,
    organization_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> list[BackfillTarget]:
    latest = WorkflowRun.objects.filter(
        work_id=OuterRef('id'),
        execution_contract_version=1,
    ).order_by('-created_at', '-id')
    works = (
        WorkflowWork.objects.filter(
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
            contract_version=1,
            disposition=WorkflowWorkDisposition.REQUIRED,
        )
        .annotate(
            latest_run_id=Subquery(latest.values('id')[:1]),
            latest_status=Subquery(latest.values('status')[:1]),
            latest_code=Subquery(latest.values('failure_code')[:1]),
        )
        .filter(
            latest_status=WorkflowRunStatus.FAILED,
            latest_code__in=failure_codes,
        )
        .order_by('created_at', 'id')
    )
    if organization_id is not None:
        works = works.filter(organization_id=organization_id)
    if project_id is not None:
        works = works.filter(project_id=project_id)

    return [
        BackfillTarget(
            work_id=work.id,
            session_id=work.subject_id,
            latest_run_id=work.latest_run_id,
            failure_code=work.latest_code,
            execution_state=work.execution_state,
        )
        for work in works[:limit]
    ]


def reset_work_for_redrive(work: WorkflowWork) -> None:
    work.execution_state = WorkflowWorkExecutionState.READY
    work.failure_streak = 0
    work.next_retry_at = None
    work.blocked_configuration_fingerprint = ''
    work.lease_owner = ''
    work.lease_expires_at = None
    work.heartbeat_at = None
    work.save(update_fields=list(_RESET_FIELDS))

    return


def redrive_target(
    *,
    work_id: uuid.UUID,
    failure_codes: tuple[str, ...],
    now: datetime,
) -> uuid.UUID | None:
    require_aware(now, field='now')

    with transaction.atomic():
        try:
            work = WorkflowWork.objects.select_for_update().get(
                id=work_id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                contract_version=1,
                disposition=WorkflowWorkDisposition.REQUIRED,
            )
        except WorkflowWork.DoesNotExist:
            return None

        latest = (
            WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
            .order_by('-created_at', '-id')
            .first()
        )
        if latest is None or latest.status != WorkflowRunStatus.FAILED or latest.failure_code not in failure_codes:
            return None

        prior_state = work.execution_state
        reset_work_for_redrive(work)
        run = queue_work_attempt(
            work_id=work.id,
            now=now,
            origin=WorkflowRunOrigin.RECONCILIATION,
        )
        if run.dispatched_at != now:
            return None

        logger.info(
            'distill_backfill_redriven',
            work_id=str(work.id),
            session_id=str(work.subject_id),
            run_id=str(run.id),
            prior_state=prior_state,
        )

        return run.id
