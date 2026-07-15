from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from engram.core.models import (
    Memory,
    MemoryCandidate,
    MemoryVersion,
    VisibilityScope,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work
from engram.memory.deterministic_gates import (
    DETERMINISTIC_POLICY_VERSION,
    DeterministicGateDisposition,
    DeterministicTerminalOutcome,
    EvaluateDeterministicCandidateGates,
)
from engram.memory.escalation import escalation_reason
from engram.memory.transitions_test_support import provenanced_candidate


@pytest.mark.django_db
def test_low_confidence_candidate_gets_decision_work_not_human_review() -> None:
    candidate, _source, _scope = provenanced_candidate('low-confidence')
    candidate.confidence = Decimal('0.010')
    candidate.save(update_fields=['confidence', 'updated_at'])
    work, _created = ensure_candidate_decision_work(candidate.id)

    assert escalation_reason(candidate) == ''
    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition == DeterministicGateDisposition.CONTINUE
    assert result.policy_version == DETERMINISTIC_POLICY_VERSION


@pytest.mark.django_db
def test_sensitive_candidate_redacts_or_rejects_without_human_escalation() -> None:
    candidate, _source, _scope = provenanced_candidate('sensitive')
    candidate.body = 'Use sk-abcdefghijklmnop before release'
    candidate.content_hash = hashlib.sha256(candidate.body.encode()).hexdigest()
    candidate.save(update_fields=['body', 'content_hash', 'updated_at'])
    work, _created = ensure_candidate_decision_work(candidate.id)

    assert escalation_reason(candidate) == ''
    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition in {
        DeterministicGateDisposition.CONTINUE,
        DeterministicGateDisposition.TERMINAL,
    }


@pytest.mark.django_db
def test_org_scope_is_deterministically_narrowed_before_comparison() -> None:
    candidate, _source, _scope = provenanced_candidate('org-scope')
    candidate.visibility_scope = VisibilityScope.ORGANIZATION
    candidate.save(update_fields=['visibility_scope', 'updated_at'])
    work, _created = ensure_candidate_decision_work(candidate.id)

    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.effective_scope is not None
    assert result.effective_scope.visibility_scope == VisibilityScope.TEAM
    assert result.effective_scope.team_id == candidate.team_id


@pytest.mark.django_db
def test_exact_identity_merges_provenance_without_new_version() -> None:
    candidate, _source, _scope = provenanced_candidate('exact-identity')
    work, _created = ensure_candidate_decision_work(candidate.id)
    memory = Memory.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=None,
        title=candidate.title,
        body=candidate.body,
        visibility_scope=candidate.visibility_scope,
        kind=candidate.kind,
    )
    MemoryVersion.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        memory=memory,
        version=1,
        body=candidate.body,
        content_hash='a' * 64,
    )

    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition == DeterministicGateDisposition.TERMINAL
    assert result.terminal_outcome == DeterministicTerminalOutcome.MERGE_EVIDENCE
    assert result.requires_transition is True


@pytest.mark.django_db
def test_cross_scope_evidence_calls_no_provider_and_mutates_nothing() -> None:
    candidate, source, _scope = provenanced_candidate('cross-scope')
    work, _created = ensure_candidate_decision_work(candidate.id)
    type(source).objects.filter(id=source.id).update(team_id=None)
    before = (
        MemoryCandidate.objects.count(),
        Memory.objects.count(),
        MemoryVersion.objects.count(),
    )

    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition == DeterministicGateDisposition.RETRY
    assert result.operational_reason == 'stale_decision'
    assert before == (
        MemoryCandidate.objects.count(),
        Memory.objects.count(),
        MemoryVersion.objects.count(),
    )
