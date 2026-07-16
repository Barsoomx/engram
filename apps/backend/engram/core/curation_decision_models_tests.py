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
    MemoryConflict,
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
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret

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
        candidate=candidate,
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


def assert_decision_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CurationDecision(**kwargs).full_clean()


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


def _other_team(organization: Organization) -> Team:
    return Team.objects.create(organization=organization, name='Other', slug='other')


@pytest.mark.django_db
@pytest.mark.parametrize('relation', ['candidate', 'work'])
def test_curation_decision_rejects_same_project_cross_team_candidate_and_work(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    relation: str,
) -> None:
    candidate, work = candidate_and_work
    other_team = _other_team(candidate.organization)
    kwargs = decision_kwargs(candidate_and_work)
    if relation == 'candidate':
        candidate.team = other_team
    else:
        work.team = other_team

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
def test_curation_decision_requires_candidate_decision_work_for_candidate(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    candidate, work = candidate_and_work
    kwargs = decision_kwargs(candidate_and_work)
    work.work_type = WorkflowWorkType.MEMORY_EMBEDDING

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
def test_curation_decision_requires_team_effective_scope_to_match_decision_team(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    candidate, _work = candidate_and_work
    other_team = _other_team(candidate.organization)
    kwargs = decision_kwargs(candidate_and_work)
    kwargs.update(effective_visibility_scope=VisibilityScope.TEAM, effective_team=other_team)

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
def test_project_effective_target_allows_same_project_provenance_team(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    candidate, _work = candidate_and_work
    other_team = _other_team(candidate.organization)
    memory = Memory.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=other_team,
        title='Project target',
        body='Project target body',
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='b' * 64,
    )
    kwargs = decision_kwargs(candidate_and_work)
    kwargs['target_memory_version'] = version

    CurationDecision(**kwargs).full_clean()


@pytest.mark.django_db
def test_team_effective_target_requires_team_visibility_and_matching_team(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
) -> None:
    candidate, _work = candidate_and_work
    memory = Memory.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        title='Project target',
        body='Project target body',
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='b' * 64,
    )
    kwargs = decision_kwargs(candidate_and_work)
    kwargs.update(
        effective_visibility_scope=VisibilityScope.TEAM,
        effective_team=candidate.team,
        target_memory_version=version,
    )

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize('relation', ['candidate', 'team'])
def test_curation_decision_rejects_cross_team_transition_provenance(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    transition: MemoryTransition,
    relation: str,
) -> None:
    candidate, _work = candidate_and_work
    other_team = _other_team(candidate.organization)
    kwargs = decision_kwargs(candidate_and_work)
    kwargs['transition'] = transition
    if relation == 'candidate':
        transition.candidate = MemoryCandidate.objects.create(
            organization=candidate.organization,
            project=candidate.project,
            team=other_team,
            title='Other candidate',
            body='Other body',
            content_hash='e' * 64,
        )
    else:
        transition.team = other_team

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize('relation', ['candidate', 'team'])
def test_curation_decision_rejects_cross_team_conflict_provenance(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    transition: MemoryTransition,
    relation: str,
) -> None:
    candidate, _work = candidate_and_work
    other_team = _other_team(candidate.organization)
    conflict_link = MemoryLink.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        memory=transition.memory,
        link_type=LinkType.FILE,
        target='apps/backend/engram/core/curation_decision_models_tests.py',
    )
    conflict = MemoryConflict.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        candidate=candidate,
        memory=transition.memory,
        memory_version=transition.to_version,
        semantic_link=conflict_link,
        opened_transition=transition,
        evidence_hash=HEX64,
    )
    if relation == 'candidate':
        conflict.candidate = MemoryCandidate.objects.create(
            organization=candidate.organization,
            project=candidate.project,
            team=other_team,
            title='Other conflict candidate',
            body='Other conflict body',
            content_hash='f' * 64,
        )
    else:
        conflict.team = other_team
    kwargs = decision_kwargs(candidate_and_work)
    kwargs['conflict'] = conflict

    assert_decision_rejected(kwargs)


@pytest.fixture
def provider_chain(candidate_and_work: tuple[MemoryCandidate, WorkflowWork]) -> tuple[ModelPolicy, ProviderCallRecord]:
    candidate, _work = candidate_and_work
    secret = ProviderSecret.objects.create(
        organization=candidate.organization,
        team=candidate.team,
        name='Curation secret',
        provider='openai',
        scope='team',
    )
    policy = ModelPolicy.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        secret=secret,
        name='Curation policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        version=1,
    )
    call = ProviderCallRecord.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        policy=policy,
        secret=secret,
        provider='openai',
        model='gpt-4.1-mini',
        task_type='curation',
        policy_version=1,
        request_id='curation-call',
        redaction_state='redacted',
    )
    return policy, call


@pytest.mark.django_db
@pytest.mark.parametrize('relation', ['provider_call_record', 'policy'])
def test_curation_decision_rejects_cross_team_provider_provenance(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    provider_chain: tuple[ModelPolicy, ProviderCallRecord],
    relation: str,
) -> None:
    candidate, _work = candidate_and_work
    other_team = _other_team(candidate.organization)
    policy, call = provider_chain
    kwargs = decision_kwargs(candidate_and_work)
    kwargs.update(provider_call_record=call, policy=policy, policy_version=1)
    if relation == 'provider_call_record':
        call.team = other_team
    else:
        policy.team = other_team

    assert_decision_rejected(kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize('policy_scope', ['organization', 'project'])
def test_curation_decision_allows_teamless_org_and_project_policy_provenance(
    candidate_and_work: tuple[MemoryCandidate, WorkflowWork],
    policy_scope: str,
) -> None:
    candidate, _work = candidate_and_work
    secret = ProviderSecret.objects.create(
        organization=candidate.organization,
        name=f'{policy_scope} secret',
        provider='openai',
        scope='organization',
    )
    policy = ModelPolicy.objects.create(
        organization=candidate.organization,
        project=candidate.project if policy_scope == 'project' else None,
        name=f'{policy_scope} policy',
        scope=policy_scope,
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )
    call = ProviderCallRecord.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        policy=policy,
        secret=secret,
        provider='openai',
        model='gpt-4.1-mini',
        task_type='curation',
        policy_version=1,
        request_id=f'curation-{policy_scope}-call',
        redaction_state='redacted',
    )
    kwargs = decision_kwargs(candidate_and_work)
    kwargs.update(provider_call_record=call, policy=policy, policy_version=1)

    CurationDecision(**kwargs).full_clean()

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
