from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
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
from engram.memory.work_dispatch import RESIGNAL_WINDOW, queue_work_attempt
from engram.memory.work_execution import execution_configuration_fingerprint

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
    distillation_sources = MemoryCandidateSource.objects.filter(
        candidate_id=OuterRef('pk'),
        source_kind=MemoryCandidateSourceKind.DISTILLATION,
        window__isnull=False,
        stage__isnull=False,
    )
    agent_sources = MemoryCandidateSource.objects.filter(
        candidate_id=OuterRef('pk'),
        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
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
        .annotate(
            has_distillation_source=Exists(distillation_sources),
            has_agent_source=Exists(agent_sources),
            has_canonical_conflict=Exists(unresolved_conflicts),
        )
        .filter(has_canonical_conflict=False)
        .filter(Q(has_distillation_source=True) | Q(has_agent_source=True))
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


_SUPPORTED_SOURCE_KINDS = (
    frozenset({MemoryCandidateSourceKind.DISTILLATION}),
    frozenset({MemoryCandidateSourceKind.AGENT_PROPOSAL}),
)


def _stale_queued(runs: list[WorkflowRun], as_of: datetime) -> bool:
    queued = sorted(
        (run for run in runs if run.status == WorkflowRunStatus.QUEUED),
        key=lambda run: (run.created_at, run.id),
    )
    if not queued:
        return False

    oldest = queued[0]

    return oldest.dispatched_at is None or as_of - oldest.dispatched_at > RESIGNAL_WINDOW


def _repair_action(work: WorkflowWork, runs: list[WorkflowRun], as_of: datetime) -> str:
    state = work.execution_state
    running = any(run.status == WorkflowRunStatus.RUNNING for run in runs)
    fresh_queued = any(run.status == WorkflowRunStatus.QUEUED for run in runs) and not _stale_queued(runs, as_of)

    if state == WorkflowWorkExecutionState.BLOCKED:
        if execution_configuration_fingerprint(work) == work.blocked_configuration_fingerprint:
            return 'skip'

        return 'clear_and_dispatch'
    if state == WorkflowWorkExecutionState.LEASED:
        if work.lease_expires_at is not None and work.lease_expires_at < as_of:
            return 'dispatch'

        return 'dispatch' if _stale_queued(runs, as_of) else 'skip'
    if state == WorkflowWorkExecutionState.RETRY_WAIT:
        if _stale_queued(runs, as_of):
            return 'dispatch'
        if work.next_retry_at is not None and work.next_retry_at <= as_of and not running and not fresh_queued:
            return 'dispatch'

        return 'skip'
    if state == WorkflowWorkExecutionState.READY:
        if running or fresh_queued:
            return 'skip'

        return 'dispatch'

    return 'skip'


def _clear_block(work: WorkflowWork) -> None:
    work.execution_state = WorkflowWorkExecutionState.READY
    work.blocked_configuration_fingerprint = ''
    work.failure_streak = 0
    work.save(
        update_fields=['execution_state', 'blocked_configuration_fingerprint', 'failure_streak', 'updated_at']
    )

    return


def _repair_candidate(*, candidate_id: uuid.UUID, as_of: datetime) -> bool:
    try:
        MemoryCandidate.objects.get(
            id=candidate_id,
            status=CandidateStatus.PROPOSED,
            decision_work_contract_version=1,
        )
    except MemoryCandidate.DoesNotExist:
        return False
    pre_source_ids = set(
        MemoryCandidateSource.objects.filter(candidate_id=candidate_id).values_list('id', flat=True)
    )

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
        all_sources = list(
            MemoryCandidateSource.objects.filter(candidate_id=locked.id)
            .select_related('window', 'observation', 'stage')
            .order_by('window_id', 'observation_id', 'id')
        )
        if {source.id for source in all_sources} != pre_source_ids:
            return False

        kinds = {source.source_kind for source in all_sources}
        if kinds not in _SUPPORTED_SOURCE_KINDS:
            return False

        work, _created = ensure_candidate_decision_work_locked(locked, sources=all_sources)
        if work.disposition != WorkflowWorkDisposition.REQUIRED:
            return False
        runs = list(
            WorkflowRun.objects.select_for_update()
            .filter(work_id=work.id, execution_contract_version=1)
            .order_by('created_at', 'id')
        )
        action = _repair_action(work, runs, as_of)
        if action == 'skip':
            return False
        if action == 'clear_and_dispatch':
            _clear_block(work)
        run = queue_work_attempt(work_id=work.id, now=as_of, origin=WorkflowRunOrigin.RECONCILIATION)

        return run.dispatched_at == as_of
