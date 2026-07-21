from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from engram.core.models import (
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    Organization,
    Project,
    Team,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory import candidate_work_reconciler
from engram.memory.candidate_work_reconciler import CandidateDecisionWorkInput
from engram.memory.conflict_links import conflict_candidate_target
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import StubBuilder

Scope = tuple[Organization, Project, object]

_HEX_A = 'a' * 64
_MANIFEST_OLD = 'ordered-manifest-old'
_MANIFEST_NEW = 'ordered-manifest-new'


def _candidate(
    scope: Scope,
    suffix: str,
    *,
    status: str = CandidateStatus.PROPOSED,
) -> MemoryCandidate:
    organization, project, session = scope

    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title=f'candidate {suffix}',
        body=f'body {suffix}',
        status=status,
        content_hash=f'candidate-hash-{suffix}',
        confidence=Decimal('0.900'),
        evidence=[{'observation_id': str(uuid.uuid4())}],
    )


def _decision_work(
    scope: Scope,
    *,
    disposition: str,
    execution_state: str,
    organization: Organization | None = None,
    project: Project | None = None,
    team: Team | None = None,
) -> WorkflowWork:
    default_org, default_project, session = scope
    resolution_reason = ''
    resolved_at = None
    if disposition != WorkflowWorkDisposition.REQUIRED:
        resolution_reason = WorkflowWorkResolutionReason.SUCCEEDED
        resolved_at = timezone.now()

    return WorkflowWork.objects.create(
        organization=organization or default_org,
        project=project or default_project,
        team=team if team is not None else session.team,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        subject_type=WorkflowSubjectType.AGENT_SESSION,
        subject_id=uuid.uuid4(),
        contract_version=1,
        occurrence_key='',
        input_fingerprint=_HEX_A,
        input_snapshot={'schema': 'candidate_decision_input/v1'},
        disposition=disposition,
        execution_state=execution_state,
        resolution_reason=resolution_reason,
        resolved_at=resolved_at,
    )


def _input(candidate: MemoryCandidate, *, manifest: str) -> CandidateDecisionWorkInput:
    return CandidateDecisionWorkInput(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
        evidence_manifest_hash=manifest,
        policy_version=1,
    )


def _inspect(scope: Scope, *, as_of: object) -> list[object]:
    organization, project, _session = scope

    return list(
        candidate_work_reconciler.inspect_candidate_work(
            organization_id=organization.id,
            project_id=project.id,
            as_of=as_of,
        )
    )


def _codes(findings: list[object]) -> list[str]:
    return sorted(finding.code for finding in findings)


def _one(findings: list[object], code: str) -> object:
    matches = [finding for finding in findings if finding.code == code]
    assert len(matches) == 1, f'expected exactly one {code!r}, got {_codes(findings)}'

    return matches[0]


@pytest.mark.django_db
def test_absent_builder_reports_builder_unavailable_per_candidate() -> None:
    scope = create_scope('candidate-no-builder')
    candidate = _candidate(scope, '1')
    candidate_work_reconciler.set_candidate_decision_work_builder(None)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_builder_unavailable')
    assert finding.entity_id == str(candidate.id)
    assert finding.work_id is None
    assert finding.auto_repair_eligible is False


@pytest.mark.django_db
def test_builder_missing_work_reports_missing() -> None:
    scope = create_scope('candidate-missing')
    candidate = _candidate(scope, '1')
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_NEW: None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_work_missing')
    assert finding.entity_id == str(candidate.id)


@pytest.mark.django_db
def test_builder_terminal_work_reports_inactive() -> None:
    scope = create_scope('candidate-inactive')
    candidate = _candidate(scope, '1')
    settled = _decision_work(
        scope,
        disposition=WorkflowWorkDisposition.COMPLETE,
        execution_state=WorkflowWorkExecutionState.SETTLED,
    )
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_NEW: settled},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_work_inactive')
    assert finding.work_id == settled.id


@pytest.mark.django_db
def test_builder_foreign_scope_work_reports_scope_mismatch() -> None:
    scope = create_scope('candidate-scope-owned')
    foreign = create_scope('candidate-scope-foreign')
    candidate = _candidate(scope, '1')
    foreign_org, foreign_project, foreign_session = foreign
    mismatched = _decision_work(
        scope,
        disposition=WorkflowWorkDisposition.REQUIRED,
        execution_state=WorkflowWorkExecutionState.READY,
        organization=foreign_org,
        project=foreign_project,
        team=foreign_session.team,
    )
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_NEW: mismatched},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_work_scope_mismatch')
    assert finding.work_id == mismatched.id


@pytest.mark.django_db
def test_active_same_scope_work_satisfies_candidate_and_ordinary_proposal_not_reclassified() -> None:
    scope = create_scope('candidate-canonical')
    satisfied = _candidate(scope, 'satisfied')
    ordinary = _candidate(scope, 'ordinary')
    active = _decision_work(
        scope,
        disposition=WorkflowWorkDisposition.REQUIRED,
        execution_state=WorkflowWorkExecutionState.READY,
    )
    builder = StubBuilder(
        inputs={
            satisfied.id: _input(satisfied, manifest=_MANIFEST_NEW),
            ordinary.id: _input(ordinary, manifest=_MANIFEST_OLD),
        },
        works_by_manifest={_MANIFEST_NEW: active, _MANIFEST_OLD: None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    assert _codes(findings) == ['candidate_decision_work_missing']
    missing = _one(findings, 'candidate_decision_work_missing')
    assert missing.entity_id == str(ordinary.id)
    assert all(finding.entity_id != str(satisfied.id) for finding in findings)


@pytest.mark.django_db
def test_changed_evidence_manifest_resolves_new_generation_without_reopening_terminal() -> None:
    scope = create_scope('candidate-new-generation')
    candidate = _candidate(scope, '1')
    old_terminal = _decision_work(
        scope,
        disposition=WorkflowWorkDisposition.REQUIRED,
        execution_state=WorkflowWorkExecutionState.TERMINAL_FAILURE,
    )
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_OLD: old_terminal, _MANIFEST_NEW: None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_work_missing')
    assert finding.entity_id == str(candidate.id)
    reloaded = WorkflowWork.objects.get(id=old_terminal.id)
    assert reloaded.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    assert reloaded.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_findings_never_include_candidate_content_or_evidence() -> None:
    scope = create_scope('candidate-content-free')
    candidate = _candidate(scope, 'secret')
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_NEW: None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'candidate_decision_work_missing')
    serialized = repr(finding)
    assert candidate.title not in serialized
    assert candidate.body not in serialized
    for field_name in ('title', 'body', 'evidence'):
        assert not hasattr(finding, field_name)


@pytest.mark.django_db
def test_inspector_never_mutates_candidates_or_works() -> None:
    scope = create_scope('candidate-read-only')
    candidate = _candidate(scope, '1')
    active = _decision_work(
        scope,
        disposition=WorkflowWorkDisposition.REQUIRED,
        execution_state=WorkflowWorkExecutionState.READY,
    )
    builder = StubBuilder(
        inputs={candidate.id: _input(candidate, manifest=_MANIFEST_NEW)},
        works_by_manifest={_MANIFEST_NEW: active},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)
    candidates_before = MemoryCandidate.objects.count()
    works_before = WorkflowWork.objects.count()

    with CaptureQueriesContext(connection) as queries:
        _inspect(scope, as_of=timezone.now())

    writes = [
        entry['sql']
        for entry in queries.captured_queries
        if entry['sql'].strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert writes == []
    assert MemoryCandidate.objects.count() == candidates_before
    assert WorkflowWork.objects.count() == works_before


@pytest.mark.django_db
def test_foreign_scope_negative_control() -> None:
    scope = create_scope('candidate-owned')
    foreign = create_scope('candidate-foreign')
    _candidate(scope, '1')

    assert _inspect(foreign, as_of=timezone.now()) == []


def _conflict_memory(scope: Scope, suffix: str) -> Memory:
    organization, project, session = scope

    return Memory.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title=f'conflict anchor {suffix}',
        body=f'conflict anchor body {suffix}',
        status=MemoryStatus.CONFLICT,
        visibility_scope=VisibilityScope.PROJECT,
        confidence=Decimal('0.900'),
        current_version=1,
    )


@pytest.mark.django_db
def test_memory_link_does_not_satisfy_candidate_conflict_exclusion() -> None:
    scope = create_scope('candidate-conflict-exception')
    organization, project, _session = scope
    conflicted = _candidate(scope, 'conflicted')
    ordinary = _candidate(scope, 'ordinary')
    anchor = _conflict_memory(scope, 'anchor')
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=anchor,
        link_type=LinkType.CONFLICTS_WITH,
        target=conflict_candidate_target(conflicted.id),
    )
    builder = StubBuilder(
        inputs={
            conflicted.id: _input(conflicted, manifest=_MANIFEST_NEW),
            ordinary.id: _input(ordinary, manifest=_MANIFEST_OLD),
        },
        works_by_manifest={_MANIFEST_NEW: None, _MANIFEST_OLD: None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)

    findings = _inspect(scope, as_of=timezone.now())

    assert _codes(findings) == ['candidate_decision_work_missing', 'candidate_decision_work_missing']
    assert {finding.entity_id for finding in findings} == {str(conflicted.id), str(ordinary.id)}


@pytest.mark.django_db
def test_memory_link_does_not_satisfy_candidate_without_builder() -> None:
    scope = create_scope('candidate-conflict-no-builder')
    organization, project, _session = scope
    conflicted = _candidate(scope, 'conflicted')
    anchor = _conflict_memory(scope, 'anchor')
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=anchor,
        link_type=LinkType.CONFLICTS_WITH,
        target=conflict_candidate_target(conflicted.id),
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(None)

    findings = _inspect(scope, as_of=timezone.now())
    assert _codes(findings) == ['candidate_decision_builder_unavailable']
    assert findings[0].entity_id == str(conflicted.id)


@pytest.mark.django_db
def test_non_proposed_candidates_are_ignored() -> None:
    scope = create_scope('candidate-non-proposed')
    _candidate(scope, 'promoted', status=CandidateStatus.PROMOTED)
    _candidate(scope, 'rejected', status=CandidateStatus.REJECTED)
    candidate_work_reconciler.set_candidate_decision_work_builder(None)

    assert _inspect(scope, as_of=timezone.now()) == []
