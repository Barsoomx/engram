from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Value
from django.db.models.functions import Cast, Concat

from engram.core.models import (
    CandidateStatus,
    LinkType,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryLink,
    WorkflowRunOrigin,
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
from engram.memory.conflict_links import CONFLICT_CANDIDATE_TARGET_PREFIX
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
    conflict_links = MemoryLink.objects.filter(
        organization_id=organization_id,
        project_id=project_id,
        memory__organization_id=organization_id,
        memory__project_id=project_id,
        link_type=LinkType.CONFLICTS_WITH,
        target=Concat(
            Value(CONFLICT_CANDIDATE_TARGET_PREFIX),
            Cast(OuterRef('id'), output_field=CharField()),
        ),
    )
    candidates = (
        MemoryCandidate.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            status=CandidateStatus.PROPOSED,
        )
        .annotate(has_canonical_conflict=Exists(conflict_links))
        .only('id', 'organization_id', 'project_id', 'team_id', 'created_at')
        .order_by('created_at', 'id')
    )

    return list(candidates)


def _cp3_repair_candidates(organization_id: uuid.UUID, project_id: uuid.UUID) -> list[MemoryCandidate]:
    conflict_links = MemoryLink.objects.filter(
        organization_id=organization_id,
        project_id=project_id,
        memory__organization_id=organization_id,
        memory__project_id=project_id,
        link_type=LinkType.CONFLICTS_WITH,
        target=Concat(
            Value(CONFLICT_CANDIDATE_TARGET_PREFIX),
            Cast(OuterRef('id'), output_field=CharField()),
        ),
    )
    return list(
        MemoryCandidate.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            status=CandidateStatus.PROPOSED,
            decision_work_contract_version=1,
            sources__isnull=False,
        )
        .annotate(has_canonical_conflict=Exists(conflict_links))
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
        if candidate.has_canonical_conflict:
            continue
        with transaction.atomic():
            try:
                locked = MemoryCandidate.objects.select_for_update().get(
                    id=candidate.id,
                    organization_id=organization_id,
                    project_id=project_id,
                    status=CandidateStatus.PROPOSED,
                    decision_work_contract_version=1,
                )
            except MemoryCandidate.DoesNotExist:
                continue
            if not MemoryCandidateSource.objects.filter(candidate_id=locked.id).exists():
                continue
            work, _created = ensure_candidate_decision_work_locked(locked)
            if (
                work.disposition != WorkflowWorkDisposition.REQUIRED
                or work.execution_state != WorkflowWorkExecutionState.READY
            ):
                continue
            run = queue_work_attempt(work_id=work.id, now=as_of, origin=WorkflowRunOrigin.RECONCILIATION)
            if run.dispatched_at != as_of:
                continue
            applied.append(str(candidate.id))

    return CandidateWorkReconciliation(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
        queued=len(applied),
        applied=tuple(applied),
    )


def reconcile_scheduled_candidate_work(*, as_of: datetime) -> int:
    require_aware(as_of)
    scopes = (
        MemoryCandidateSource.objects.filter(
            candidate__status=CandidateStatus.PROPOSED,
            candidate__decision_work_contract_version=1,
        )
        .values_list('organization_id', 'project_id')
        .distinct()
        .order_by('organization_id', 'project_id')
    )

    total = 0
    for organization_id, project_id in scopes:
        result = reconcile_candidate_work(
            organization_id=organization_id,
            project_id=project_id,
            as_of=as_of,
        )
        total += result.queued

    return total
