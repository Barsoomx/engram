from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_celery_outbox.models import CeleryOutboxDeadLetter

from engram.core.models import (
    CandidateStatus,
    MemoryCandidate,
    Organization,
    Project,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory import candidate_work_reconciler, work_execution, work_reconciliation
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import ended_session_work

Scope = tuple[Organization, Project, object]

_DISTILL_TASK = 'engram.memory.distill_session_work_v1'
_SESSION_LEASE = timedelta(seconds=720)

_FINDING_FIELDS = {
    'invariant_id',
    'code',
    'organization_id',
    'project_id',
    'entity_type',
    'entity_id',
    'work_id',
    'workflow_run_id',
    'observed_at',
    'proposed_action',
    'auto_repair_eligible',
}
_REPORT_FIELDS = {
    'organization_id',
    'project_id',
    'as_of',
    'findings',
    'counts_by_code',
    'work_counts_by_type_state',
    'oldest_age_seconds_by_code',
}
_FORBIDDEN_SUBSTRINGS = ('candidate body', 'candidate title')


def _candidate(scope: Scope, suffix: str) -> MemoryCandidate:
    organization, project, session = scope

    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title=f'candidate title {suffix}',
        body=f'candidate body {suffix}',
        status=CandidateStatus.PROPOSED,
        content_hash=f'candidate-hash-{suffix}',
        confidence=Decimal('0.900'),
    )


def _report(scope: Scope, *, as_of: object) -> object:
    organization, project, _session = scope
    candidate_work_reconciler.set_candidate_decision_work_builder(None)

    return work_reconciliation.build_reconciliation_report(
        organization_id=organization.id,
        project_id=project.id,
        as_of=as_of,
    )


@pytest.mark.django_db
def test_report_and_finding_dataclasses_expose_exact_fields() -> None:
    scope = create_scope('report-shape')
    _candidate(scope, '1')

    report = _report(scope, as_of=timezone.now())

    assert {field.name for field in fields(work_reconciliation.ReconciliationReport)} == _REPORT_FIELDS
    assert {field.name for field in fields(work_reconciliation.ReconciliationFinding)} == _FINDING_FIELDS
    assert report.findings
    finding = report.findings[0]
    assert isinstance(finding.invariant_id, str)
    assert finding.invariant_id.startswith('P')


@pytest.mark.django_db
def test_candidate_findings_carry_p6_invariant_id() -> None:
    scope = create_scope('report-p6')
    candidate = _candidate(scope, '1')

    report = _report(scope, as_of=timezone.now())

    candidate_findings = [f for f in report.findings if f.entity_id == str(candidate.id)]
    assert candidate_findings
    assert {f.invariant_id for f in candidate_findings} == {'P6'}


@pytest.mark.django_db
def test_two_runs_at_same_snapshot_are_equal_and_ordered() -> None:
    scope = create_scope('report-deterministic')
    for index in range(5):
        _candidate(scope, f'{index:02d}')
    as_of = timezone.now()

    first = _report(scope, as_of=as_of)
    second = _report(scope, as_of=as_of)

    assert first.findings == second.findings
    assert first.counts_by_code == second.counts_by_code
    assert first.oldest_age_seconds_by_code == second.oldest_age_seconds_by_code
    sort_keys = [
        (
            f.invariant_id,
            f.code,
            f.entity_type,
            f.entity_id,
            str(f.work_id or ''),
            str(f.workflow_run_id or ''),
        )
        for f in first.findings
    ]
    assert sort_keys == sorted(sort_keys)


@pytest.mark.django_db
def test_samples_capped_at_twenty_per_code_while_counts_stay_exact() -> None:
    scope = create_scope('report-capped')
    for index in range(25):
        _candidate(scope, f'{index:02d}')

    report = _report(scope, as_of=timezone.now())

    counts = dict(report.counts_by_code)
    assert counts['candidate_decision_builder_unavailable'] == 25
    sampled = [f for f in report.findings if f.code == 'candidate_decision_builder_unavailable']
    assert len(sampled) == 20


@pytest.mark.django_db
def test_report_is_content_free() -> None:
    scope = create_scope('report-content-free')
    _candidate(scope, 'secret')

    report = _report(scope, as_of=timezone.now())

    serialized = repr(report)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialized
    for finding in report.findings:
        for field_name in ('title', 'body', 'evidence', 'failure_reason'):
            assert not hasattr(finding, field_name)


@pytest.mark.django_db
def test_report_is_read_only() -> None:
    scope = create_scope('report-read-only')
    _candidate(scope, '1')
    as_of = timezone.now()

    with CaptureQueriesContext(connection) as queries:
        report = _report(scope, as_of=as_of)

    assert report.findings
    writes = [
        entry['sql']
        for entry in queries.captured_queries
        if entry['sql'].strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert writes == []


@pytest.mark.django_db
def test_foreign_scope_negative_control() -> None:
    scope = create_scope('report-owned')
    foreign = create_scope('report-foreign')
    _candidate(scope, '1')

    report = _report(foreign, as_of=timezone.now())

    assert report.findings == ()


def _dead_letter(work: WorkflowWork) -> CeleryOutboxDeadLetter:
    now = timezone.now()

    return CeleryOutboxDeadLetter.objects.create(
        task_id=f'workflow-work:{work.id}',
        task_name=_DISTILL_TASK,
        args=[str(work.id)],
        kwargs={},
        created_at=now,
        dead_at=now,
        failure_reason='provider secret leaked into transport failure reason',
    )


def _claim(work: WorkflowWork, now: object) -> object:
    return work_execution.claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:report:{uuid.uuid4()}',
        now=now,
        lease_for=_SESSION_LEASE,
    )


@pytest.mark.django_db
def test_oldest_age_reflects_condition_onset_not_as_of() -> None:
    scope = create_scope('report-onset-age')
    work = ended_session_work(scope, sequence=1)
    now = timezone.now()
    WorkflowWork.objects.filter(id=work.id).update(created_at=now - timedelta(days=3))

    report = _report(scope, as_of=now)

    ages = dict(report.oldest_age_seconds_by_code)
    assert ages['work_never_claimed'] == pytest.approx(259200, abs=120)
    finding = next(f for f in report.findings if f.code == 'work_never_claimed')
    assert finding.observed_at <= now


@pytest.mark.django_db
def test_transport_findings_carry_neutral_transport_marker() -> None:
    scope = create_scope('report-transport-marker')
    work = ended_session_work(scope, sequence=1)
    _dead_letter(work)

    report = _report(scope, as_of=timezone.now())

    transport = [f for f in report.findings if f.code == 'dead_letter_unsatisfied_work']
    assert transport
    assert {f.invariant_id for f in transport} == {'transport'}


@pytest.mark.django_db
def test_lease_expired_session_finding_carries_p4_invariant_id() -> None:
    scope = create_scope('report-lease-p4')
    work = ended_session_work(scope, sequence=1)
    now = timezone.now()
    _claim(work, now)

    report = _report(scope, as_of=now + _SESSION_LEASE + timedelta(seconds=60))

    lease_findings = [f for f in report.findings if f.code == 'lease_expired']
    assert lease_findings
    assert {f.invariant_id for f in lease_findings} == {'P4'}
