from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count

from engram.core.models import WorkflowWork
from engram.memory import (
    candidate_work_reconciler,
    projection_reconciler,
    session_work_reconciler,
    transport_work_reconciler,
)
from engram.memory.aware_time import require_aware
from engram.memory.session_work_reconciler import LEASE_EXPIRED, SessionWorkFinding

_SAMPLE_LIMIT = 20

INVARIANT_SESSION = 'P3'
INVARIANT_SESSION_LEASE = 'P4'
INVARIANT_CANDIDATE = 'P6'
INVARIANT_PROJECTION = 'P7'
INVARIANT_TRANSPORT = 'transport'


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    invariant_id: str
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
class ReconciliationReport:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    findings: tuple[ReconciliationFinding, ...]
    counts_by_code: tuple[tuple[str, int], ...]
    work_counts_by_type_state: tuple[tuple[str, str, int], ...]
    oldest_age_seconds_by_code: tuple[tuple[str, int], ...]


def _session_invariant(finding: SessionWorkFinding) -> str:
    if finding.code == LEASE_EXPIRED:
        return INVARIANT_SESSION_LEASE

    return INVARIANT_SESSION


def _wrap(finding: SessionWorkFinding, invariant_id: str) -> ReconciliationFinding:
    return ReconciliationFinding(
        invariant_id=invariant_id,
        code=finding.code,
        organization_id=finding.organization_id,
        project_id=finding.project_id,
        entity_type=finding.entity_type,
        entity_id=finding.entity_id,
        work_id=finding.work_id,
        workflow_run_id=finding.workflow_run_id,
        observed_at=finding.observed_at,
        proposed_action=finding.proposed_action,
        auto_repair_eligible=finding.auto_repair_eligible,
    )


def _sort_key(finding: ReconciliationFinding) -> tuple[str, str, str, str, str, str]:
    return (
        finding.invariant_id,
        finding.code,
        finding.entity_type,
        finding.entity_id,
        str(finding.work_id) if finding.work_id is not None else '',
        str(finding.workflow_run_id) if finding.workflow_run_id is not None else '',
    )


def _collect(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> list[ReconciliationFinding]:
    session_inspection = session_work_reconciler.inspect_session_work(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
    )
    candidate_findings = candidate_work_reconciler.inspect_candidate_work(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
    )
    projection_findings = projection_reconciler.inspect_projection(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
    )
    transport_findings = transport_work_reconciler.inspect_transport_work(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
    )

    findings: list[ReconciliationFinding] = []
    findings.extend(_wrap(finding, _session_invariant(finding)) for finding in session_inspection.findings)
    findings.extend(_wrap(finding, INVARIANT_CANDIDATE) for finding in candidate_findings)
    findings.extend(_wrap(finding, INVARIANT_PROJECTION) for finding in projection_findings)
    findings.extend(_wrap(finding, INVARIANT_TRANSPORT) for finding in transport_findings)

    return findings


def _work_counts_by_type_state(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> tuple[tuple[str, str, int], ...]:
    rows = (
        WorkflowWork.objects.filter(organization_id=organization_id, project_id=project_id)
        .values('work_type', 'execution_state')
        .annotate(total=Count('id'))
        .order_by('work_type', 'execution_state')
    )

    return tuple((row['work_type'], row['execution_state'], row['total']) for row in rows)


def _cap_samples(findings: list[ReconciliationFinding]) -> tuple[ReconciliationFinding, ...]:
    per_code: Counter[str] = Counter()
    capped: list[ReconciliationFinding] = []
    for finding in findings:
        if per_code[finding.code] >= _SAMPLE_LIMIT:
            continue

        per_code[finding.code] += 1
        capped.append(finding)

    return tuple(capped)


def _oldest_age_seconds_by_code(
    findings: list[ReconciliationFinding],
    as_of: datetime,
) -> tuple[tuple[str, int], ...]:
    oldest: dict[str, int] = {}
    for finding in findings:
        age = int((as_of - finding.observed_at).total_seconds())
        oldest[finding.code] = max(oldest.get(finding.code, age), age)

    return tuple(sorted(oldest.items()))


def build_reconciliation_report(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> ReconciliationReport:
    require_aware(as_of)

    findings = _collect(organization_id=organization_id, project_id=project_id, as_of=as_of)
    findings.sort(key=_sort_key)
    counts_by_code = tuple(sorted(Counter(finding.code for finding in findings).items()))
    oldest_age_seconds_by_code = _oldest_age_seconds_by_code(findings, as_of)
    capped = _cap_samples(findings)

    return ReconciliationReport(
        organization_id=organization_id,
        project_id=project_id,
        as_of=as_of,
        findings=capped,
        counts_by_code=counts_by_code,
        work_counts_by_type_state=_work_counts_by_type_state(organization_id, project_id),
        oldest_age_seconds_by_code=oldest_age_seconds_by_code,
    )
