import hashlib

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from engram.core.models import (
    AuditEvent,
    CurationDecision,
    CurationOutcome,
    CurationReasonCode,
    EvidenceTier,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryTransition,
    MemoryTransitionType,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)


HEX64 = 'a' * 64


@pytest.fixture
def scope() -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    return organization, team, project


@pytest.fixture
def candidate_and_work(scope: tuple[Organization, Team, Project]) -> tuple[MemoryCandidate, WorkflowWork]:
    organization, team, project = scope
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Candidate',
        body='Candidate body',
        content_hash=hashlib.sha256(b'candidate').hexdigest(),
    )
    work = WorkflowWork.objects.create(
        organization=organization,
        project=project,
        team=team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate.id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint=HEX64,
        input_snapshot={'candidate_id': str(candidate.id)},
    )
    return candidate, work


@pytest.fixture
def transition(candidate_and_work: tuple[MemoryCandidate, WorkflowWork]) -> MemoryTransition:
    candidate, _work = candidate_and_work
    organization, team, project = candidate.organization, candidate.team, candidate.project
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Existing memory',
        body='Existing memory body',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='b' * 64,
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        full_text=memory.body,
    )
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type=LinkType.FILE,
        target='apps/backend/engram/core/models.py',
    )
    audit_event = AuditEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        event_type='MemoryTransitionCommitted',
        actor_type='test',
        actor_id='curation-decision-tests',
    )
    return MemoryTransition.objects.create(
        organization=organization,
        project=project,
        team=team,
        transition_type=MemoryTransitionType.SUPERSEDE,
        idempotency_key='curation-decision-transition',
        request_fingerprint='c' * 64,
        memory=memory,
        from_version=version,
        to_version=version,
        result_memory=memory,
        result_version=version,
        exact_document=document,
        result_exact_document=document,
        semantic_link=link,
        audit_event=audit_event,
        provenance_hash='d' * 64,
    )


def decision_kwargs(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> dict[str, object]:
    candidate, work = candidate_and_work
    return {
        'organization': candidate.organization,
        'project': candidate.project,
        'team': candidate.team,
        'work': work,
        'candidate': candidate,
        'input_fingerprint': HEX64,
        'evidence_manifest_hash': HEX64,
        'comparison_manifest_hash': HEX64,
        'outcome': CurationOutcome.REJECT_CANDIDATE,
        'reason_code': CurationReasonCode.NOISE_EMPTY,
        'effective_visibility_scope': VisibilityScope.PROJECT,
        'evidence_tier': EvidenceTier.NONE,
        'payload_hash': HEX64,
    }


@pytest.mark.django_db
def test_curation_decision_contract_choices_and_defaults(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    assert CurationOutcome.values == [
        'publish_new',
        'merge_evidence',
        'revise_memory',
        'supersede_memory',
        'reject_candidate',
        'open_conflict',
    ]
    assert CurationReasonCode.values == [
        'noise_empty',
        'noise_title_echo',
        'noise_redaction_only',
        'noise_parse_wrapper',
        'noise_lifecycle_only',
        'unsupported_provenance',
        'unsafe_content_after_redaction',
        'non_durable_session_scope',
        'exact_identity',
        'exact_duplicate_no_new_evidence',
        'distinct_claim',
        'equivalent_claim',
        'same_subject_revision',
        'ordered_replacement',
        'redundant_claim',
        'unsupported_claim',
        'same_scope_contradiction',
    ]
    assert EvidenceTier.values == ['none', 'supported', 'corroborated']

    decision = CurationDecision.objects.create(**decision_kwargs(candidate_and_work))

    assert decision.work_id == candidate_and_work[1].id
    assert decision.candidate_id == candidate_and_work[0].id
    assert decision.contract_version == 1
    assert decision.outcome == CurationOutcome.REJECT_CANDIDATE
    assert decision.reason_code == CurationReasonCode.NOISE_EMPTY
    assert decision.evidence_tier == EvidenceTier.NONE
    assert decision.transition_id is None
    assert decision.conflict_id is None


@pytest.mark.django_db
def test_curation_decision_rejects_invalid_contract_scope_and_hash(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    invalid = CurationDecision(**decision_kwargs(candidate_and_work), contract_version=2)
    with pytest.raises(ValidationError):
        invalid.full_clean()

    invalid_kwargs = decision_kwargs(candidate_and_work)
    invalid_kwargs.update(effective_visibility_scope=VisibilityScope.TEAM, effective_team=None)
    invalid = CurationDecision(**invalid_kwargs)
    with pytest.raises(ValidationError):
        invalid.full_clean()

    invalid_kwargs = decision_kwargs(candidate_and_work)
    invalid_kwargs['input_fingerprint'] = 'A' * 64
    invalid = CurationDecision(**invalid_kwargs)
    with pytest.raises(ValidationError):
        invalid.full_clean()


@pytest.mark.django_db
def test_curation_decision_is_append_only_and_work_is_unique(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    decision = CurationDecision.objects.create(**decision_kwargs(candidate_and_work))
    decision.input_fingerprint = 'b' * 64
    with pytest.raises(ValidationError):
        decision.save()

    with pytest.raises(IntegrityError), transaction.atomic():
        CurationDecision.objects.create(**decision_kwargs(candidate_and_work))


@pytest.mark.django_db
def test_curation_decision_rejects_duplicate_transition(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    transition: MemoryTransition,
) -> None:
    first_kwargs = decision_kwargs(candidate_and_work)
    first_kwargs['transition'] = transition
    CurationDecision.objects.create(**first_kwargs)

    second_kwargs = decision_kwargs(candidate_and_work)
    second_kwargs['transition'] = transition
    second_kwargs['work'] = WorkflowWork.objects.create(
        organization=candidate_and_work[0].organization,
        project=candidate_and_work[0].project,
        team=candidate_and_work[0].team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate_and_work[0].id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint='e' * 64,
        input_snapshot={'candidate_id': str(candidate_and_work[0].id)},
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        CurationDecision.objects.create(**second_kwargs)
