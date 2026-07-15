from __future__ import annotations

import hashlib
from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from engram.core.models import (
    CurationDecision,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryTransition,
    MemoryVersion,
    WorkflowRun,
    WorkflowWork,
    VisibilityScope,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work
from engram.memory.deterministic_gates import (
    DETERMINISTIC_POLICY_VERSION,
    DeterministicGateDisposition,
    DeterministicGateResult,
    DeterministicTerminalOutcome,
    EffectiveCandidateScope,
    EvaluateDeterministicCandidateGates,
    SanitizedCandidateView,
)
from engram.core.redaction import REDACTED_VALUE, RedactionResult
from engram.memory.escalation import escalation_reason
from engram.memory.transitions_test_support import provenanced_candidate
from engram.model_policy.models import ProviderCallRecord


def _work_for(candidate: MemoryCandidate):
    work, _created = ensure_candidate_decision_work(candidate.id)
    return work


def _rewrite_candidate(candidate: MemoryCandidate, *, title: str | None = None, body: str | None = None) -> None:
    if title is not None:
        candidate.title = title
    if body is not None:
        candidate.body = body
    candidate.content_hash = hashlib.sha256(f'{candidate.title}\n{candidate.body}'.encode()).hexdigest()
    candidate.save(update_fields=['title', 'body', 'content_hash', 'updated_at'])


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

    assert result.disposition == DeterministicGateDisposition.CONTINUE
    assert result.sanitized_candidate is not None
    assert REDACTED_VALUE in result.sanitized_candidate.body


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

    before_versions = MemoryVersion.objects.count()
    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition == DeterministicGateDisposition.TERMINAL
    assert result.terminal_outcome == DeterministicTerminalOutcome.MERGE_EVIDENCE
    assert result.reason_code == 'exact_identity'
    assert result.target_memory_version_id == memory.versions.get(version=1).id
    assert result.requires_transition is True
    assert MemoryVersion.objects.count() == before_versions


@pytest.mark.django_db
def test_cross_scope_evidence_calls_no_provider_and_mutates_nothing() -> None:
    candidate, source, _scope = provenanced_candidate('cross-scope')
    work, _created = ensure_candidate_decision_work(candidate.id)
    type(source).objects.filter(id=source.id).update(team_id=None)
    before = {
        'candidate': MemoryCandidate.objects.count(),
        'memory': Memory.objects.count(),
        'version': MemoryVersion.objects.count(),
        'conflict': MemoryConflict.objects.count(),
        'transition': MemoryTransition.objects.count(),
        'decision': CurationDecision.objects.count(),
        'work': WorkflowWork.objects.count(),
        'run': WorkflowRun.objects.count(),
        'provider': ProviderCallRecord.objects.count(),
    }

    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.disposition == DeterministicGateDisposition.RETRY
    assert result.operational_reason == 'stale_decision'
    after = {
        'candidate': MemoryCandidate.objects.count(),
        'memory': Memory.objects.count(),
        'version': MemoryVersion.objects.count(),
        'conflict': MemoryConflict.objects.count(),
        'transition': MemoryTransition.objects.count(),
        'decision': CurationDecision.objects.count(),
        'work': WorkflowWork.objects.count(),
        'run': WorkflowRun.objects.count(),
        'provider': ProviderCallRecord.objects.count(),
    }
    assert before == after


@pytest.mark.django_db
def test_session_scope_rejects_as_non_durable() -> None:
    candidate, _source, _scope = provenanced_candidate('session-scope')
    candidate.visibility_scope = VisibilityScope.SESSION
    candidate.save(update_fields=['visibility_scope', 'updated_at'])

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.disposition == DeterministicGateDisposition.TERMINAL
    assert result.reason_code == 'non_durable_session_scope'


@pytest.mark.django_db
def test_project_scope_preserves_project_effective_scope() -> None:
    candidate, _source, _scope = provenanced_candidate('project-scope')

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.effective_scope == EffectiveCandidateScope(VisibilityScope.PROJECT, None)


@pytest.mark.django_db
def test_team_scope_preserves_team_effective_scope() -> None:
    candidate, _source, _scope = provenanced_candidate('team-scope')
    candidate.visibility_scope = VisibilityScope.TEAM
    candidate.save(update_fields=['visibility_scope', 'updated_at'])

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.effective_scope == EffectiveCandidateScope(VisibilityScope.TEAM, candidate.team_id)


@pytest.mark.django_db
def test_team_scope_without_durable_sources_rejects_unsupported_provenance() -> None:
    candidate, _source, _scope = provenanced_candidate('missing-team-source')
    candidate.visibility_scope = VisibilityScope.TEAM
    candidate.title = 'No source candidate'
    candidate.body = 'Durable claim without source'
    candidate.content_hash = hashlib.sha256(uuid4().bytes).hexdigest()
    candidate.decision_work_contract_version = 1
    candidate.save(update_fields=['visibility_scope', 'title', 'body', 'content_hash', 'decision_work_contract_version', 'updated_at'])
    candidate.sources.all().delete()

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.disposition == DeterministicGateDisposition.TERMINAL
    assert result.reason_code == 'unsupported_provenance'


@pytest.mark.django_db
def test_empty_candidate_is_rejected_as_noise_empty() -> None:
    candidate, _source, _scope = provenanced_candidate('empty')
    _rewrite_candidate(candidate, title=' \t ', body='\n')

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_empty'


@pytest.mark.django_db
def test_redaction_only_candidate_is_rejected() -> None:
    candidate, _source, _scope = provenanced_candidate('redaction-only')
    _rewrite_candidate(candidate, title=REDACTED_VALUE, body=REDACTED_VALUE)

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_redaction_only'


@pytest.mark.django_db
def test_title_echo_uses_unicode_normalization_and_whitespace_folding() -> None:
    candidate, _source, _scope = provenanced_candidate('title-echo')
    _rewrite_candidate(candidate, title='Ｆoo\n  bar', body='foo bar')

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_title_echo'


@pytest.mark.django_db
def test_parse_fallback_entry_cannot_be_supported_by_another_entry() -> None:
    candidate, source, _scope = provenanced_candidate('parse-wrapper')
    candidate.evidence = [
        {'parse_fallback': True},
        {'supporting_observation_ids': [str(source.observation_id)]},
    ]
    candidate.save(update_fields=['evidence', 'updated_at'])

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_parse_wrapper'


@pytest.mark.django_db
def test_lifecycle_only_evidence_is_rejected() -> None:
    candidate, source, _scope = provenanced_candidate('lifecycle')
    source.observation.observation_type = 'session_lifecycle'
    source.observation.save(update_fields=['observation_type', 'updated_at'])

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_lifecycle_only'


@pytest.mark.django_db
def test_lifecycle_source_metadata_event_is_rejected() -> None:
    candidate, source, _scope = provenanced_candidate('lifecycle-metadata')
    source.observation.source_metadata = {'event_type': 'session_end'}
    source.observation.save(update_fields=['source_metadata', 'updated_at'])

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'noise_lifecycle_only'


@pytest.mark.django_db
def test_short_old_low_confidence_candidate_continues() -> None:
    candidate, _source, _scope = provenanced_candidate('short-old')
    _rewrite_candidate(candidate, title='x', body='y')
    candidate.confidence = Decimal('0.001')
    candidate.save(update_fields=['confidence', 'updated_at'])
    MemoryCandidate.objects.filter(id=candidate.id).update(created_at=timezone.now() - timedelta(days=900))

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.disposition == DeterministicGateDisposition.CONTINUE


@pytest.mark.django_db
def test_exact_duplicate_without_new_evidence_requires_no_transition() -> None:
    candidate, source, _scope = provenanced_candidate('exact-duplicate')
    work = _work_for(candidate)
    memory = Memory.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        title=candidate.title,
        body=candidate.body,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        memory=memory,
        version=1,
        body=candidate.body,
        content_hash='a' * 64,
    )
    from engram.core.models import MemoryVersionSource

    MemoryVersionSource.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        memory_version=version,
        candidate_source=source,
        source_content_hash='b' * 64,
    )

    result = EvaluateDeterministicCandidateGates().execute(work.id)

    assert result.reason_code == 'exact_duplicate_no_new_evidence'
    assert result.target_memory_version_id == version.id
    assert result.requires_transition is False


@pytest.mark.django_db
def test_same_text_in_another_scope_continues() -> None:
    candidate, _source, _scope = provenanced_candidate('other-scope')
    memory = Memory.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
        title=candidate.title,
        body=candidate.body,
        visibility_scope=VisibilityScope.TEAM,
    )
    MemoryVersion.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        memory=memory,
        version=1,
        body=candidate.body,
        content_hash='a' * 64,
    )

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.disposition == DeterministicGateDisposition.CONTINUE


@pytest.mark.django_db
def test_multiple_exact_current_matches_retry_stale_decision() -> None:
    candidate, _source, _scope = provenanced_candidate('multiple-matches')
    for _index in range(2):
        memory = Memory.objects.create(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
            title=candidate.title,
            body=candidate.body,
            visibility_scope=VisibilityScope.PROJECT,
        )
        MemoryVersion.objects.create(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
            memory=memory,
            version=1,
            body=candidate.body,
            content_hash='a' * 64,
        )

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.disposition == DeterministicGateDisposition.RETRY
    assert result.operational_reason == 'stale_decision'


@pytest.mark.django_db
def test_residual_secret_precedes_title_echo_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate, _source, _scope = provenanced_candidate('residual-secret')
    _rewrite_candidate(candidate, title='sk-abcdefghijklmnop', body='sk-abcdefghijklmnop')
    monkeypatch.setattr(
        'engram.memory.deterministic_gates.redact_value',
        lambda value: RedactionResult(value=value, redacted=False),
    )

    result = EvaluateDeterministicCandidateGates().execute(_work_for(candidate).id)

    assert result.reason_code == 'unsafe_content_after_redaction'


def test_unknown_terminal_outcome_is_rejected_at_runtime() -> None:
    with pytest.raises(ValueError):
        DeterministicGateResult(
            disposition=DeterministicGateDisposition.TERMINAL,
            policy_version=DETERMINISTIC_POLICY_VERSION,
            sanitized_candidate=SanitizedCandidateView(
                title='title',
                body='body',
                kind='',
                evidence=(),
                content_hash='a' * 64,
                redaction_codes=(),
            ),
            effective_scope=EffectiveCandidateScope(VisibilityScope.PROJECT, None),
            terminal_outcome='unknown',
            reason_code='noise_empty',
        )
