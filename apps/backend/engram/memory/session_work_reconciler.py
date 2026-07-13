from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Exists, OuterRef

from engram.core.models import (
    AgentSession,
    SessionStatus,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.aware_time import require_aware
from engram.memory.observation_work import useful_observation_upper
from engram.memory.work_dispatch import RESIGNAL_WINDOW, queue_work_attempt
from engram.memory.work_execution import execution_configuration_fingerprint, fingerprint_matches

SESSION_CURRENT_WORK_MISSING = 'session_current_work_missing'
SESSION_CURRENT_WORK_INCOMPLETE = 'session_current_work_incomplete'
WORK_NEVER_CLAIMED = 'work_never_claimed'
ATTEMPT_SIGNAL_STALE = 'attempt_signal_stale'
LEASE_EXPIRED = 'lease_expired'
LOGICAL_RETRY_DUE = 'logical_retry_due'
CONFIGURATION_BLOCKED = 'configuration_blocked'
CONFIGURATION_CHANGED = 'configuration_changed'
TERMINAL_INPUT_FAILURE = 'terminal_input_failure'

_ENTITY_TYPE = 'agent_session'

_PROPOSED_ACTION = {
    SESSION_CURRENT_WORK_MISSING: 'create_or_reuse_exact_work',
    SESSION_CURRENT_WORK_INCOMPLETE: 'execute_latest_work',
    WORK_NEVER_CLAIMED: 'queue_reconciliation_attempt',
    ATTEMPT_SIGNAL_STALE: 'resignal_queued_attempt',
    LEASE_EXPIRED: 'reclaim_via_claim_work',
    LOGICAL_RETRY_DUE: 'queue_reconciliation_attempt',
    CONFIGURATION_BLOCKED: 'report_only',
    CONFIGURATION_CHANGED: 'clear_block_and_queue',
    TERMINAL_INPUT_FAILURE: 'report_only',
}

_AUTO_REPAIR_CODES = frozenset(
    {
        WORK_NEVER_CLAIMED,
        ATTEMPT_SIGNAL_STALE,
        LEASE_EXPIRED,
        LOGICAL_RETRY_DUE,
        CONFIGURATION_CHANGED,
    }
)


@dataclass(frozen=True, slots=True)
class SessionWorkFinding:
    code: str
    organization_id: uuid.UUID
    project_id: uuid.UUID
    entity_type: str
    entity_id: str
    work_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    observed_at: datetime
    proposed_action: str
    auto_repair_eligible: bool


@dataclass(frozen=True, slots=True)
class SessionWorkInspection:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    findings: tuple[SessionWorkFinding, ...]


@dataclass(frozen=True, slots=True)
class SessionWorkReconciliation:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    queued: int
    applied: tuple[str, ...]


def _snapshot_upper(work: WorkflowWork) -> int | None:
    snapshot = work.input_snapshot
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get('lower_sequence_exclusive') != 0:
        return None

    return snapshot.get('upper_sequence_inclusive')


def _current_generation_work(session: AgentSession, upper: int, *, lock: bool) -> WorkflowWork | None:
    works = WorkflowWork.objects.filter(
        organization_id=session.organization_id,
        project_id=session.project_id,
        team_id=session.team_id,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        subject_type=WorkflowSubjectType.AGENT_SESSION,
        subject_id=session.id,
        contract_version=1,
    )
    if lock:
        works = works.select_for_update()

    for work in works.order_by('created_at', 'id'):
        if _snapshot_upper(work) == upper and fingerprint_matches(work):
            return work

    return None


def _v1_runs(work: WorkflowWork, *, lock: bool) -> list[WorkflowRun]:
    runs = WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
    if lock:
        runs = runs.select_for_update()

    return list(runs.order_by('created_at', 'id'))


def _classify_blocked(work: WorkflowWork, evidence: uuid.UUID | None) -> tuple[str, uuid.UUID | None]:
    if execution_configuration_fingerprint(work) == work.blocked_configuration_fingerprint:
        return CONFIGURATION_BLOCKED, evidence

    return CONFIGURATION_CHANGED, evidence


def _classify_queued(runs: list[WorkflowRun], as_of: datetime) -> tuple[str, uuid.UUID | None] | None:
    queued = [run for run in runs if run.status == WorkflowRunStatus.QUEUED]
    if not queued:
        return None

    oldest = queued[0]
    if oldest.dispatched_at is None or as_of - oldest.dispatched_at > RESIGNAL_WINDOW:
        return ATTEMPT_SIGNAL_STALE, oldest.id

    return SESSION_CURRENT_WORK_INCOMPLETE, None


def _classify_ready(work: WorkflowWork, runs: list[WorkflowRun], as_of: datetime) -> tuple[str, uuid.UUID | None]:
    queued = _classify_queued(runs, as_of)
    if queued is not None:
        return queued

    if as_of - work.created_at >= RESIGNAL_WINDOW:
        return WORK_NEVER_CLAIMED, None

    return SESSION_CURRENT_WORK_INCOMPLETE, None


def _classify(work: WorkflowWork, runs: list[WorkflowRun], as_of: datetime) -> tuple[str, uuid.UUID | None]:
    state = work.execution_state
    latest = runs[-1] if runs else None
    latest_id = latest.id if latest is not None else None

    if state == WorkflowWorkExecutionState.TERMINAL_FAILURE:
        return TERMINAL_INPUT_FAILURE, latest_id

    if state == WorkflowWorkExecutionState.BLOCKED:
        return _classify_blocked(work, latest_id)

    if state == WorkflowWorkExecutionState.RETRY_WAIT:
        queued = _classify_queued(runs, as_of)
        if queued is not None:
            return queued

        if work.next_retry_at is not None and work.next_retry_at <= as_of:
            return LOGICAL_RETRY_DUE, None

        return SESSION_CURRENT_WORK_INCOMPLETE, None

    if state == WorkflowWorkExecutionState.LEASED:
        queued = _classify_queued(runs, as_of)
        if queued is not None:
            return queued

        if work.lease_expires_at is not None and work.lease_expires_at < as_of:
            running = next((run for run in runs if run.status == WorkflowRunStatus.RUNNING), None)

            return LEASE_EXPIRED, (running.id if running is not None else None)

        return SESSION_CURRENT_WORK_INCOMPLETE, None

    return _classify_ready(work, runs, as_of)


_CONFIGURATION_ONSET_CODES = frozenset(
    {
        CONFIGURATION_BLOCKED,
        CONFIGURATION_CHANGED,
        TERMINAL_INPUT_FAILURE,
    }
)


def _run_by_id(runs: list[WorkflowRun], run_id: uuid.UUID | None) -> WorkflowRun | None:
    if run_id is None:
        return None

    return next((run for run in runs if run.id == run_id), None)


def _condition_onset(
    code: str,
    work: WorkflowWork,
    runs: list[WorkflowRun],
    run_id: uuid.UUID | None,
) -> datetime:
    if code == ATTEMPT_SIGNAL_STALE:
        run = _run_by_id(runs, run_id)
        if run is not None:
            return run.dispatched_at or run.created_at

        return work.created_at

    if code == LEASE_EXPIRED:
        return work.lease_expires_at or work.created_at

    if code == LOGICAL_RETRY_DUE:
        return work.next_retry_at or work.created_at

    if code in _CONFIGURATION_ONSET_CODES:
        latest = runs[-1] if runs else None
        if latest is not None and latest.finished_at is not None:
            return latest.finished_at

        return work.updated_at

    return work.created_at


def _build_finding(
    session: AgentSession,
    work: WorkflowWork | None,
    runs: list[WorkflowRun],
    as_of: datetime,
) -> SessionWorkFinding | None:
    if work is None:
        return _finding(
            session,
            SESSION_CURRENT_WORK_MISSING,
            work_id=None,
            run_id=None,
            observed_at=min(session.created_at, as_of),
        )

    if work.disposition != WorkflowWorkDisposition.REQUIRED:
        return None

    code, run_id = _classify(work, runs, as_of)
    observed_at = min(_condition_onset(code, work, runs, run_id), as_of)

    return _finding(session, code, work_id=work.id, run_id=run_id, observed_at=observed_at)


def _finding(
    session: AgentSession,
    code: str,
    *,
    work_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    observed_at: datetime,
) -> SessionWorkFinding:
    return SessionWorkFinding(
        code=code,
        organization_id=session.organization_id,
        project_id=session.project_id,
        entity_type=_ENTITY_TYPE,
        entity_id=str(session.id),
        work_id=work_id,
        workflow_run_id=run_id,
        observed_at=observed_at,
        proposed_action=_PROPOSED_ACTION[code],
        auto_repair_eligible=code in _AUTO_REPAIR_CODES,
    )


def _has_unresolved_required_work() -> Exists:
    return Exists(
        WorkflowWork.objects.filter(
            subject_type=WorkflowSubjectType.AGENT_SESSION,
            subject_id=OuterRef('id'),
            work_type=WorkflowWorkType.SESSION_DISTILLATION,
            contract_version=1,
            disposition=WorkflowWorkDisposition.REQUIRED,
        )
    )


def _scoped_ended_sessions(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    unresolved_only: bool = False,
) -> list[AgentSession]:
    sessions = AgentSession.objects.filter(
        organization_id=organization_id,
        project_id=project_id,
        status=SessionStatus.ENDED,
        end_work_contract_version=1,
    )
    if unresolved_only:
        sessions = sessions.filter(_has_unresolved_required_work())

    return list(sessions.order_by('created_at', 'id'))


def _sort_key(finding: SessionWorkFinding) -> tuple[str, str, str, str]:
    return (
        finding.code,
        finding.entity_id,
        str(finding.work_id) if finding.work_id is not None else '',
        str(finding.workflow_run_id) if finding.workflow_run_id is not None else '',
    )


def inspect_session_work(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> SessionWorkInspection:
    require_aware(as_of)

    findings: list[SessionWorkFinding] = []
    for session in _scoped_ended_sessions(organization_id, project_id):
        upper = useful_observation_upper(session)
        work = _current_generation_work(session, upper, lock=False)
        runs = _v1_runs(work, lock=False) if work is not None else []
        finding = _build_finding(session, work, runs, as_of)
        if finding is not None:
            findings.append(finding)

    findings.sort(key=_sort_key)

    return SessionWorkInspection(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
        findings=tuple(findings),
    )


def _clear_block(work: WorkflowWork) -> None:
    work.execution_state = WorkflowWorkExecutionState.READY
    work.blocked_configuration_fingerprint = ''
    work.failure_streak = 0
    work.save(
        update_fields=[
            'execution_state',
            'blocked_configuration_fingerprint',
            'failure_streak',
            'updated_at',
        ]
    )

    return


def _reconcile_one_session(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    as_of: datetime,
) -> str | None:
    with transaction.atomic():
        try:
            session = AgentSession.objects.select_for_update(of=('self',)).get(
                id=session_id,
                organization_id=organization_id,
                project_id=project_id,
            )
        except AgentSession.DoesNotExist:
            return None

        if session.status != SessionStatus.ENDED or session.end_work_contract_version != 1:
            return None

        upper = useful_observation_upper(session)
        work = _current_generation_work(session, upper, lock=True)
        if work is None or work.disposition != WorkflowWorkDisposition.REQUIRED:
            return None

        runs = _v1_runs(work, lock=True)
        code, _run_id = _classify(work, runs, as_of)
        if code not in _AUTO_REPAIR_CODES:
            return None

        if code == CONFIGURATION_CHANGED:
            _clear_block(work)

        run = queue_work_attempt(work_id=work.id, now=as_of, origin=WorkflowRunOrigin.RECONCILIATION)
        if run.dispatched_at != as_of:
            return None

        return code


def reconcile_session_work(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> SessionWorkReconciliation:
    require_aware(as_of)

    applied: list[str] = []
    session_ids = [session.id for session in _scoped_ended_sessions(organization_id, project_id, unresolved_only=True)]
    for session_id in session_ids:
        code = _reconcile_one_session(organization_id, project_id, session_id, as_of)
        if code is not None:
            applied.append(code)

    return SessionWorkReconciliation(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
        queued=len(applied),
        applied=tuple(applied),
    )


def reconcile_scheduled_session_work(*, as_of: datetime) -> int:
    require_aware(as_of)

    scopes = (
        AgentSession.objects.filter(
            status=SessionStatus.ENDED,
            end_work_contract_version=1,
        )
        .values_list('organization_id', 'project_id')
        .distinct()
        .order_by('organization_id', 'project_id')
    )

    total = 0
    for organization_id, project_id in scopes:
        result = reconcile_session_work(
            organization_id=organization_id,
            project_id=project_id,
            as_of=as_of,
        )
        total += result.queued

    return total
