from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from engram.core.models import (
    CandidateStatus,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    MemoryConflict,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
)
from engram.memory.aware_time import require_aware
from engram.memory.candidate_decision_work import (
    CandidateDecisionWorkBuilder,
    ensure_candidate_decision_work_locked,
)
from engram.memory.candidate_decision_work import (
    CandidateDecisionWorkInput as _CandidateDecisionWorkInput,
)
from engram.memory.session_work_reconciler import SessionWorkFinding
from engram.memory.work_dispatch import queue_work_attempt

CandidateDecisionWorkInput = _CandidateDecisionWorkInput

CANDIDATE_DECISION_BUILDER_UNAVAILABLE = 'candidate_decision_builder_unavailable'
CANDIDATE_DECISION_WORK_MISSING = 'candidate_decision_work_missing'
CANDIDATE_DECISION_WORK_INACTIVE = 'candidate_decision_work_inactive'
CANDIDATE_DECISION_WORK_SCOPE_MISMATCH = 'candidate_decision_work_scope_mismatch'

_ENTITY_TYPE = 'memory_candidate'
_PROPOSED_ACTION = 'report_only'

_INACTIVE_EXECUTION_STATES = frozenset(
    {
        WorkflowWorkExecutionState.SETTLED,
        WorkflowWorkExecutionState.TERMINAL_FAILURE,
    }
)


_BUILDER: CandidateDecisionWorkBuilder | None = None


@dataclass(frozen=True, slots=True)
class ReconcileCandidateDecisionWorkResult:
    scanned: int
    queued: int


class ReconcileCandidateDecisionWork:
    def execute(self, as_of: datetime | None = None) -> ReconcileCandidateDecisionWorkResult:
        if as_of is None:
            as_of = timezone.now()
        require_aware(as_of)

        candidates = _cp3_repair_candidates()
        queued = 0
        for candidate in candidates:
            if _repair_candidate(candidate_id=candidate.id, as_of=as_of):
                queued += 1

        return ReconcileCandidateDecisionWorkResult(scanned=len(candidates), queued=queued)


def set_candidate_decision_work_builder(builder: CandidateDecisionWorkBuilder | None) -> None:
    global _BUILDER
    _BUILDER = builder

    return


def get_candidate_decision_work_builder() -> CandidateDecisionWorkBuilder | None:
    return _BUILDER


def _finding(
    candidate: MemoryCandidate,
    code: str,
    *,
    work_id: uuid.UUID | None,
    as_of: datetime,
) -> SessionWorkFinding:
    return SessionWorkFinding(
        code=code,
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        entity_type=_ENTITY_TYPE,
        entity_id=str(candidate.id),
        work_id=work_id,
        workflow_run_id=None,
        observed_at=min(candidate.created_at, as_of),
        proposed_action=_PROPOSED_ACTION,
        auto_repair_eligible=False,
    )


def _classify(
    candidate: MemoryCandidate,
    builder: CandidateDecisionWorkBuilder,
    as_of: datetime,
) -> SessionWorkFinding | None:
    value = builder.expected_input(candidate_id=candidate.id)
    work = builder.exact_work(value=value)
    if work is None:
        return _finding(candidate, CANDIDATE_DECISION_WORK_MISSING, work_id=None, as_of=as_of)

    if (work.organization_id, work.project_id, work.team_id) != (
        value.organization_id,
        value.project_id,
        value.team_id,
    ):
        return _finding(candidate, CANDIDATE_DECISION_WORK_SCOPE_MISMATCH, work_id=work.id, as_of=as_of)

    if work.disposition != WorkflowWorkDisposition.REQUIRED or work.execution_state in _INACTIVE_EXECUTION_STATES:
        return _finding(candidate, CANDIDATE_DECISION_WORK_INACTIVE, work_id=work.id, as_of=as_of)

    return None


def _proposed_candidates(organization_id: uuid.UUID, project_id: uuid.UUID) -> list[MemoryCandidate]:
    unresolved_conflicts = MemoryConflict.objects.filter(
        candidate_id=OuterRef('pk'),
        resolved_transition__isnull=True,
    )
    candidates = (
        MemoryCandidate.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            status=CandidateStatus.PROPOSED,
        )
        .annotate(has_canonical_conflict=Exists(unresolved_conflicts))
        .only('id', 'organization_id', 'project_id', 'team_id', 'created_at')
        .order_by('created_at', 'id')
    )

    return list(candidates)


def _cp3_repair_candidates(
    organization_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> list[MemoryCandidate]:
    unresolved_conflicts = MemoryConflict.objects.filter(
        candidate_id=OuterRef('pk'),
        resolved_transition__isnull=True,
    )
    durable_sources = MemoryCandidateSource.objects.filter(
        candidate_id=OuterRef('pk'),
        source_kind=MemoryCandidateSourceKind.DISTILLATION,
        window__isnull=False,
        stage__isnull=False,
    )
    filters = {
        'status': CandidateStatus.PROPOSED,
        'decision_work_contract_version': 1,
    }
    if organization_id is not None:
        filters['organization_id'] = organization_id
    if project_id is not None:
        filters['project_id'] = project_id
    return list(
        MemoryCandidate.objects.filter(**filters)
        .filter(Exists(durable_sources))
        .annotate(has_canonical_conflict=Exists(unresolved_conflicts))
        .filter(has_canonical_conflict=False)
        .only('id', 'organization_id', 'project_id', 'team_id', 'created_at')
        .distinct()
        .order_by('created_at', 'id')
    )


def inspect_candidate_work(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> tuple[SessionWorkFinding, ...]:
    require_aware(as_of)

    builder = get_candidate_decision_work_builder()
    findings: list[SessionWorkFinding] = []
    for candidate in _proposed_candidates(organization_id, project_id):
        if candidate.has_canonical_conflict:
            continue

        if builder is None:
            findings.append(_finding(candidate, CANDIDATE_DECISION_BUILDER_UNAVAILABLE, work_id=None, as_of=as_of))
            continue

        finding = _classify(candidate, builder, as_of)
        if finding is not None:
            findings.append(finding)

    return tuple(findings)


@dataclass(frozen=True, slots=True)
class CandidateWorkReconciliation:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    queued: int
    applied: tuple[str, ...]


def reconcile_candidate_work(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> CandidateWorkReconciliation:
    require_aware(as_of)

    applied: list[str] = []
    for candidate in _cp3_repair_candidates(organization_id, project_id):
        if _repair_candidate(candidate_id=candidate.id, as_of=as_of):
            applied.append(str(candidate.id))

    return CandidateWorkReconciliation(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
        queued=len(applied),
        applied=tuple(applied),
    )


def reconcile_scheduled_candidate_work(*, as_of: datetime) -> int:
    return ReconcileCandidateDecisionWork().execute(as_of=as_of).queued


def _requeue_eligible(work: WorkflowWork, as_of: datetime) -> bool:
    if work.execution_state == WorkflowWorkExecutionState.READY:
        return True

    if (
        work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
        and work.next_retry_at is not None
        and work.next_retry_at <= as_of
    ):
        return True

    return (
        work.execution_state == WorkflowWorkExecutionState.LEASED
        and work.lease_expires_at is not None
        and work.lease_expires_at < as_of
    )


def _blocking_attempt_exists(work: WorkflowWork, as_of: datetime) -> bool:
    runs = WorkflowRun.objects.filter(
        work_id=work.id,
        status__in=(WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING),
    )
    if not runs.exists():
        return False

    lease_expired = (
        work.execution_state == WorkflowWorkExecutionState.LEASED
        and work.lease_expires_at is not None
        and work.lease_expires_at < as_of
    )
    if not lease_expired:
        return True

    return runs.filter(status=WorkflowRunStatus.QUEUED).exists()


def _repair_candidate(*, candidate_id: uuid.UUID, as_of: datetime) -> bool:
    with transaction.atomic():
        try:
            locked = MemoryCandidate.objects.select_for_update().get(
                id=candidate_id,
                status=CandidateStatus.PROPOSED,
                decision_work_contract_version=1,
            )
        except MemoryCandidate.DoesNotExist:
            return False
        if MemoryConflict.objects.filter(candidate_id=locked.id, resolved_transition__isnull=True).exists():
            return False
        sources = list(
            MemoryCandidateSource.objects.filter(
                candidate_id=locked.id,
                source_kind=MemoryCandidateSourceKind.DISTILLATION,
                window__isnull=False,
                stage__isnull=False,
            )
            .select_related('window', 'observation', 'stage')
            .order_by('window_id', 'observation_id', 'id')
        )
        if not sources:
            return False
        work, _created = ensure_candidate_decision_work_locked(locked, sources=sources)
        if work.disposition != WorkflowWorkDisposition.REQUIRED or not _requeue_eligible(work, as_of):
            return False
        if _blocking_attempt_exists(work, as_of):
            return False
        run = queue_work_attempt(work_id=work.id, now=as_of, origin=WorkflowRunOrigin.RECONCILIATION)
        return run.dispatched_at == as_of
