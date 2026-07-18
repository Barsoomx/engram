from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
import structlog
from django.db import connection
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.context.services import authorized_retrieval_documents
from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    CurationOutcome,
    CurationReasonCode,
    EvidenceTier,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    MemoryLink,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
    MemoryVersion,
    MemoryVersionSource,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory import c53_orchestrator_test_support as orch
from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork
from engram.memory.curation import (
    CurateMemoryCandidate,
    CurateMemoryCandidateInput,
    curation_judge_prompt,
    curation_judge_system_prompt,
    embed_candidate,
    find_near_duplicate,
    is_low_signal,
    parse_curation_decision,
    parse_curation_reason,
    resolve_curator_llm_judge_enabled,
)
from engram.memory.curation_judge import CurationJudgeComparisonV1, CurationJudgeVerdictV1
from engram.memory.curation_test_support import (
    JudgeGatewayStub,
    create_curation_policy,
    patch_atomic_near_duplicate,
    patch_judge_gateway,
    seed_atomic_existing_and_duplicate,
    set_curator_settings,
)
from engram.memory.tasks import embed_memory_projection_work_v1
from engram.memory.transitions import MemoryTransitionError, PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    FakeProviderGateway,
    ProviderCallInput,
    ProviderCallResult,
)

_LONG_BODY = 'The retrieval pipeline ranks documents by cosine similarity over embeddings.'

_JudgeGatewayStub = JudgeGatewayStub


@dataclass
class _DocumentStub:
    embedding_vector: list[float] = field(default_factory=list)


def create_scope(*, suffix: str = '1') -> tuple[Organization, Team, Project]:
    slug_suffix = '' if suffix == '1' else f'-{suffix}'
    organization = Organization.objects.create(name=f'Engram {suffix}', slug=f'engram{slug_suffix}')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )

    return organization, team, project


def create_embedding_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team Embedding OpenAI',
        provider='openai',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-embedding-secret',
        hmac_digest='embedding-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Embedding policy',
        scope='project',
        task_type='embedding',
        provider='openai',
        model='text-embedding-3-small',
        secret=secret,
        version=1,
    )


def create_candidate(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    title: str,
    body: str,
    content_hash: str,
    confidence: str = '0.900',
    visibility_scope: str = VisibilityScope.PROJECT,
    evidence: list[dict[str, object]] | None = None,
    decision_work_contract_version: int = 1,
) -> MemoryCandidate:
    existing_curation_policy_ids = tuple(
        ModelPolicy.objects.filter(
            organization=organization,
            project=project,
            task_type='curation',
        ).values_list('id', flat=True)
    )
    candidate, _source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix=content_hash,
        title=title,
        body=body,
        visibility_scope=visibility_scope,
        confidence=Decimal(confidence),
    )
    ModelPolicy.objects.filter(
        organization=organization,
        project=project,
        task_type='curation',
    ).exclude(id__in=existing_curation_policy_ids).update(active=False)
    update_fields = ['decision_work_contract_version', 'updated_at']
    candidate.decision_work_contract_version = decision_work_contract_version
    if evidence is not None:
        candidate.evidence = evidence
        update_fields.insert(0, 'evidence')
    candidate.save(update_fields=update_fields)

    return candidate


def promote_candidate(candidate: MemoryCandidate) -> Memory:
    result = PromoteMemoryCandidate().execute(transition_request(candidate))

    return result.memory


def complete_memory_embedding(memory: Memory) -> RetrievalDocument:
    document = RetrievalDocument.objects.get(memory=memory)
    work = WorkflowWork.objects.get(
        work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        subject_type=WorkflowSubjectType.RETRIEVAL_DOCUMENT,
        subject_id=document.id,
        input_snapshot__exact_projection_hash=document.exact_projection_hash,
    )
    embed_memory_projection_work_v1(str(work.id))

    return RetrievalDocument.objects.get(id=document.id)


def seed_existing_and_duplicate(
    organization: Organization,
    team: Team,
    project: Project,
) -> tuple[Memory, MemoryCandidate]:
    existing = promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Retrieval ranking',
            body=_LONG_BODY,
            content_hash='hash-existing',
        ),
    )
    complete_memory_embedding(existing)
    duplicate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-duplicate',
    )

    return existing, duplicate


class _ExplodingJudgeGateway(FakeProviderGateway):
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise AssertionError('the LLM judge must not be consulted on this path')


class _ExplodingGateway(FakeProviderGateway):
    def embed(self, data: object) -> object:
        raise AssertionError('the embedding provider must not be consulted on this path')

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise AssertionError('the judge provider must not be consulted on this path')


class _CountingJudgeGatewayStub(FakeProviderGateway):
    def __init__(self, body: str) -> None:
        self._body = body
        self.calls = 0

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls += 1

        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='',
            generated_body=self._body,
        )


def build_scope(organization: Organization, team: Team, project: Project) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=organization.id,
        api_key_id=organization.id,
        project_ids=(project.id,),
        team_ids=(team.id,),
        capabilities=(),
        actor_type='system',
        actor_id='test',
        project_bound=False,
    )


def test_is_low_signal_flags_empty_and_title_echo_bodies() -> None:
    assert is_low_signal(MemoryCandidate(title='t', body='')) is True
    assert is_low_signal(MemoryCandidate(title='t', body='   ')) is True
    assert is_low_signal(MemoryCandidate(title=_LONG_BODY, body=_LONG_BODY)) is True


def test_is_low_signal_passes_substantive_and_short_legitimate_bodies() -> None:
    assert is_low_signal(MemoryCandidate(title='Retrieval ranking', body=_LONG_BODY)) is False
    assert is_low_signal(MemoryCandidate(title='Networking', body='Use port 8443 not 8080')) is False
    assert is_low_signal(MemoryCandidate(title='Install', body='Run npm ci not npm i')) is False
    assert is_low_signal(MemoryCandidate(title='Database host', body='DB_HOST=prod')) is False


def test_find_near_duplicate_returns_highest_above_threshold() -> None:
    near = _DocumentStub(embedding_vector=[1.0, 0.0, 0.0])
    partial = _DocumentStub(embedding_vector=[0.6, 0.8, 0.0])
    documents = (partial, near)

    match = find_near_duplicate([1.0, 0.0, 0.0], documents, Decimal('0.850'))

    assert match is not None
    document, score = match
    assert document is near
    assert score == pytest.approx(1.0)


def test_find_near_duplicate_returns_none_below_threshold() -> None:
    orthogonal = _DocumentStub(embedding_vector=[0.0, 1.0, 0.0])

    assert find_near_duplicate([1.0, 0.0, 0.0], (orthogonal,), Decimal('0.850')) is None


def test_find_near_duplicate_returns_none_for_empty_inputs() -> None:
    assert find_near_duplicate([], (_DocumentStub(embedding_vector=[1.0]),), Decimal('0.850')) is None
    assert find_near_duplicate([1.0], (), Decimal('0.850')) is None
    assert find_near_duplicate([1.0], (_DocumentStub(embedding_vector=[]),), Decimal('0.850')) is None


@pytest.mark.django_db
def test_embed_candidate_returns_none_without_embedding_policy() -> None:
    organization, team, project = create_scope()
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-no-policy',
    )

    assert embed_candidate(candidate) is None


@pytest.mark.django_db
def test_embed_candidate_returns_vector_with_embedding_policy() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-with-policy',
    )

    vector = embed_candidate(candidate)

    assert vector is not None
    assert len(vector) == EMBEDDING_DIMENSION


@pytest.mark.django_db
def test_curator_supersession_commits_one_typed_lineage_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, _project, existing, duplicate = seed_atomic_existing_and_duplicate(
        'curator-typed-supersession'
    )
    set_curator_settings(organization)
    patch_atomic_near_duplicate(monkeypatch, existing, score=0.970)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    transition = MemoryTransition.objects.get(
        candidate=duplicate,
        transition_type=MemoryTransitionType.SUPERSEDE,
    )
    assert result.decision == 'superseded'
    assert result.memory is not None
    assert transition.memory_id == existing.id
    assert transition.result_memory_id == result.memory.id
    assert transition.semantic_link is not None
    assert transition.semantic_link.link_type == LinkType.SUPERSEDED_BY
    assert transition.semantic_link.target == str(result.memory.id)
    assert existing.stale is True
    assert RetrievalDocument.objects.get(memory=existing).stale is True
    assert duplicate.status == CandidateStatus.PROMOTED
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'


@pytest.mark.django_db
def test_curate_promotes_clean_candidate_and_indexes_document() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-clean',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.memory is not None
    assert candidate.status == CandidateStatus.PROMOTED
    assert Memory.objects.filter(stale=False).count() == 1
    assert RetrievalDocument.objects.filter(memory=result.memory).exists()


@pytest.mark.django_db
def test_curate_promotes_short_legitimate_one_liner() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Networking port',
        body='Use port 8443 not 8080',
        content_hash='hash-one-liner',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.memory is not None
    assert candidate.status == CandidateStatus.PROMOTED
    assert Memory.objects.filter(stale=False).count() == 1


@pytest.mark.django_db
def test_curate_does_not_supersede_memory_in_another_org() -> None:
    org_a, team_a, project_a = create_scope()
    create_embedding_policy(org_a, team_a, project_a)
    set_curator_settings(org_a)
    _org_b, team_b, project_b = create_scope(suffix='2')
    create_embedding_policy(_org_b, team_b, project_b)
    foreign = promote_candidate(
        create_candidate(
            _org_b,
            team_b,
            project_b,
            title='Retrieval ranking',
            body=_LONG_BODY,
            content_hash='hash-foreign',
        ),
    )
    candidate = create_candidate(
        org_a,
        team_a,
        project_a,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-candidate',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    foreign.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.superseded_memory is None
    assert foreign.stale is False
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_curate_supersedes_existing_near_duplicate_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, _project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-near-duplicate')
    set_curator_settings(organization)
    patch_atomic_near_duplicate(monkeypatch, existing, score=0.970)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.memory is not None
    assert result.memory.id != existing.id
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True
    assert duplicate.status == CandidateStatus.PROMOTED
    transition = MemoryTransition.objects.get(
        candidate=duplicate,
        transition_type=MemoryTransitionType.SUPERSEDE,
    )
    assert transition.memory_id == existing.id
    assert transition.result_memory_id == result.memory.id
    assert transition.semantic_link.link_type == LinkType.SUPERSEDED_BY
    assert transition.semantic_link.target == str(result.memory.id)
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'


@pytest.mark.django_db
def test_curate_rejects_low_signal_candidate_without_creating_memory() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='noise',
        body='noise',
        content_hash='hash-noise',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'rejected'
    assert result.memory is None
    assert candidate.status == CandidateStatus.REJECTED
    assert Memory.objects.count() == 0
    assert RetrievalDocument.objects.count() == 0
    audit = AuditEvent.objects.get(event_type='MemoryAutoRejected')
    assert audit.actor_type == 'system'
    assert audit.target_id == str(candidate.id)


@pytest.mark.django_db
def test_curate_reject_does_not_clear_durable_conflict_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, candidate = seed_atomic_existing_and_duplicate('curator-conflict-reject')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))

    opened = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=existing, resolved_transition__isnull=True)
    conflict_link_id = conflict.semantic_link_id

    monkeypatch.setattr('engram.memory.curation.is_low_signal', lambda _candidate: True)
    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    conflict.refresh_from_db()
    candidate.refresh_from_db()
    assert opened.decision == 'held_conflict'
    assert result.decision == 'held_conflict'
    assert candidate.status == CandidateStatus.PROPOSED
    assert not AuditEvent.objects.filter(
        event_type='MemoryAutoRejected',
        target_id=str(candidate.id),
    ).exists()
    assert conflict.resolved_transition_id is None
    assert conflict.resolution == ''
    assert MemoryLink.objects.filter(id=conflict_link_id).exists()


@pytest.mark.django_db
def test_curate_disabled_passes_through_to_plain_promotion() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization, enabled=False)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Networking port',
        body='Use port 8443 not 8080',
        content_hash='hash-disabled',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'passthrough'
    assert result.memory is not None
    assert candidate.status == CandidateStatus.PROMOTED
    assert Memory.objects.count() == 1


@pytest.mark.django_db
def test_curate_without_embedding_policy_promotes_clean() -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-existing',
    )
    duplicate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-duplicate',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    assert result.decision == 'promoted'
    assert result.memory is not None
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_curate_replays_already_promoted_candidate_as_duplicate() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-clean',
    )
    first = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    second = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    assert second.duplicate is True
    assert second.memory is not None
    assert second.memory.id == first.memory.id
    assert Memory.objects.count() == 1
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_embedding_work_materializes_existing_memory_for_dedup() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    existing = promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Retrieval ranking',
            body=_LONG_BODY,
            content_hash='hash-existing',
        ),
    )
    document = complete_memory_embedding(existing)

    assert len(document.embedding_vector) == EMBEDDING_DIMENSION


@pytest.mark.django_db
def test_resolve_curator_llm_judge_enabled_defaults_false_without_settings() -> None:
    organization, _team, _project = create_scope()

    assert resolve_curator_llm_judge_enabled(organization) is False


@pytest.mark.django_db
def test_resolve_curator_llm_judge_enabled_reads_stored_value() -> None:
    organization, _team, _project = create_scope()
    set_curator_settings(organization, llm_judge_enabled=True)

    assert resolve_curator_llm_judge_enabled(organization) is True


def test_parse_curation_decision_reads_known_decisions() -> None:
    assert parse_curation_decision('{"decision": "merge"}') == 'merge'
    assert parse_curation_decision('{"decision": "keep_both"}') == 'keep_both'
    assert parse_curation_decision('{"decision": "reject"}') == 'reject'
    assert parse_curation_decision('{"decision": "contradicts", "reason": "opposite claim"}') == 'contradicts'


def test_parse_curation_decision_defaults_keep_both_for_unknown_or_unparseable() -> None:
    assert parse_curation_decision('not json at all') == 'keep_both'
    assert parse_curation_decision('{"decision": "explode"}') == 'keep_both'
    assert parse_curation_decision('[]') == 'keep_both'
    assert parse_curation_decision('{}') == 'keep_both'


def test_parse_curation_decision_strips_json_fence() -> None:
    assert parse_curation_decision('```json\n{"decision": "reject"}\n```') == 'reject'


def test_parse_curation_decision_unfenced_still_parses() -> None:
    assert parse_curation_decision('{"decision": "reject"}') == 'reject'


def test_parse_curation_decision_invalid_still_defaults() -> None:
    assert parse_curation_decision('this is not json and not fenced either') == 'keep_both'


def test_curation_judge_system_prompt_requires_reason() -> None:
    prompt = curation_judge_system_prompt()

    assert '"reason"' in prompt
    assert '"decision"' in prompt


def test_curation_judge_system_prompt_keep_both_requires_compatibility() -> None:
    prompt = curation_judge_system_prompt()

    keep_both_bullet = next(line for line in prompt.splitlines() if line.startswith('- "keep_both"'))
    assert 'compatible' in keep_both_bullet


def test_parse_curation_reason_reads_reason() -> None:
    assert parse_curation_reason('{"decision": "merge", "reason": "same fact"}') == 'same fact'
    assert parse_curation_reason('{"decision": "merge"}') == ''
    assert parse_curation_reason('not json') == ''
    assert parse_curation_reason('[]') == ''


def test_parse_curation_reason_strips_json_fence() -> None:
    assert parse_curation_reason('```json\n{"reason": "dup"}\n```') == 'dup'


def test_parse_curation_reason_unfenced_still_parses() -> None:
    assert parse_curation_reason('{"reason": "dup"}') == 'dup'


def test_parse_curation_reason_invalid_still_defaults() -> None:
    assert parse_curation_reason('this is not json and not fenced either') == ''


@pytest.mark.django_db
def test_judge_decision_logs_redacted_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(
        monkeypatch,
        _JudgeGatewayStub('{"decision": "keep_both", "reason": "token sk-abcdef0123456789 already stored"}'),
    )

    with structlog.testing.capture_logs() as captured_logs:
        CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    events = [entry for entry in captured_logs if entry['event'] == 'curation_judge_decision']
    assert len(events) == 1
    assert events[0]['decision'] == 'keep_both'
    assert 'sk-abcdef0123456789' not in events[0]['reason']
    assert 'already stored' in events[0]['reason']


def test_curation_judge_prompt_redacts_secrets() -> None:
    candidate = MemoryCandidate(title='Deploy', body='Authenticate with sk-abcdef0123456789')
    memory = Memory(title='Old deploy', body='Rotated token egk_abcdef0123456789 last week')

    prompt = curation_judge_prompt(candidate, memory)

    assert 'sk-abcdef0123456789' not in prompt
    assert 'egk_abcdef0123456789' not in prompt
    assert '[REDACTED]' in prompt


@pytest.mark.django_db
def test_curate_judge_keep_both_promotes_clean_without_supersede() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.memory is not None
    assert existing.stale is False
    assert duplicate.status == CandidateStatus.PROMOTED
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0
    assert Memory.objects.filter(stale=False).count() == 2


@pytest.mark.django_db
def test_curate_judge_merge_supersedes_near_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-judge-merge')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "merge"}'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True
    transition = MemoryTransition.objects.get(
        candidate=duplicate,
        transition_type=MemoryTransitionType.SUPERSEDE,
    )
    assert transition.memory_id == existing.id
    assert transition.result_memory_id == result.memory.id
    assert transition.semantic_link.link_type == LinkType.SUPERSEDED_BY
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'


@pytest.mark.django_db
def test_curate_judge_reject_rejects_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "reject"}'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'rejected'
    assert result.memory is None
    assert duplicate.status == CandidateStatus.REJECTED
    assert existing.stale is False
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0
    audit = AuditEvent.objects.get(event_type='MemoryAutoRejected', target_id=str(duplicate.id))
    assert audit.metadata['reason'] == 'near_dup_judge_reject'


@pytest.mark.django_db
def test_curate_judge_reject_applies_even_with_pre_existing_provider_call_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    judge_policy = create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=judge_policy,
        secret=judge_policy.secret,
        provider=judge_policy.provider,
        model=judge_policy.model,
        task_type=judge_policy.task_type,
        policy_version=judge_policy.version,
        request_id=f'curator:{duplicate.id}:judge',
        trace_id='trace-preexisting-judge',
        redaction_state='clean',
        token_usage={'input_tokens': 1, 'output_tokens': 0},
        cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
        metadata={'prompt_retained': False},
    )
    stub = _CountingJudgeGatewayStub('{"decision": "reject"}')
    patch_judge_gateway(monkeypatch, stub)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'rejected'
    assert duplicate.status == CandidateStatus.REJECTED
    assert stub.calls >= 1


@pytest.mark.django_db
def test_curate_judge_contradicts_opens_durable_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-conflict-open')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'held_conflict'
    assert result.memory is None
    assert duplicate.status == CandidateStatus.PROPOSED
    conflict = MemoryConflict.objects.get(
        candidate=duplicate,
        memory=existing,
        resolved_transition__isnull=True,
    )
    assert len(conflict.evidence_hash) == 64
    link = MemoryLink.objects.get(link_type=LinkType.CONFLICTS_WITH)
    assert link.memory_id == existing.id
    assert link.target == f'candidate:{duplicate.id}'
    assert link.id == conflict.semantic_link_id
    transition = conflict.opened_transition
    assert transition.transition_type == MemoryTransitionType.CONFLICT_OPEN
    assert transition.semantic_link_id == link.id
    audit = transition.audit_event
    assert audit.event_type == 'MemoryTransitionCommitted'
    assert audit.actor_type == 'system'
    assert audit.metadata['candidate_id'] == str(duplicate.id)
    assert audit.metadata['memory_id'] == str(existing.id)
    assert audit.metadata['reason'] == 'opposite claim'
    assert existing.status == MemoryStatus.APPROVED
    assert existing.stale is False
    assert existing.refuted is False
    documents = authorized_retrieval_documents(organization, project, build_scope(organization, team, project))
    assert existing.id not in {document.memory_id for document in documents}


@pytest.mark.django_db
def test_curate_judge_contradicts_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-conflict-replay')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    conflict_count = MemoryConflict.objects.filter(candidate=duplicate).count()
    link_count = MemoryLink.objects.filter(link_type=LinkType.CONFLICTS_WITH).count()
    transition_count = MemoryTransition.objects.filter(
        candidate=duplicate,
        transition_type=MemoryTransitionType.CONFLICT_OPEN,
    ).count()
    audit_count = AuditEvent.objects.filter(event_type='MemoryTransitionCommitted').count()
    second = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    third = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    duplicate.refresh_from_db()
    assert second.decision == 'held_conflict'
    assert third.decision == 'held_conflict'
    assert (
        MemoryConflict.objects.filter(candidate=duplicate, resolved_transition__isnull=True).count()
        == conflict_count
        == 1
    )
    assert MemoryLink.objects.filter(link_type=LinkType.CONFLICTS_WITH).count() == link_count == 1
    assert (
        MemoryTransition.objects.filter(
            candidate=duplicate,
            transition_type=MemoryTransitionType.CONFLICT_OPEN,
        ).count()
        == transition_count
        == 1
    )
    assert AuditEvent.objects.filter(event_type='MemoryTransitionCommitted').count() == audit_count


@pytest.mark.django_db
def test_curate_judge_contradicts_redacts_reason_before_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-conflict-redaction')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(
        monkeypatch,
        _JudgeGatewayStub(
            '{"decision": "contradicts", "reason": "opposite of token sk-abcdef0123456789 already stored"}',
        ),
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    conflict = MemoryConflict.objects.get(candidate=duplicate, resolved_transition__isnull=True)
    audit = conflict.opened_transition.audit_event
    assert 'sk-abcdef0123456789' not in audit.metadata['reason']
    assert '[REDACTED]' in audit.metadata['reason']


@pytest.mark.django_db
def test_curate_judge_contradicts_truncates_stored_reason_to_200_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-conflict-truncation')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    long_reason = 'x' * 250
    patch_judge_gateway(
        monkeypatch,
        _JudgeGatewayStub(json.dumps({'decision': 'contradicts', 'reason': long_reason})),
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    conflict = MemoryConflict.objects.get(candidate=duplicate, resolved_transition__isnull=True)
    audit = conflict.opened_transition.audit_event
    assert len(audit.metadata['reason']) <= 200


@pytest.mark.django_db
def test_curate_judge_unparseable_defaults_to_keep_both(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('totally not json'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.memory is not None
    assert existing.stale is False
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_curate_judge_without_policy_defaults_to_keep_both() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'promoted'
    assert result.memory is not None
    assert existing.stale is False
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_curate_gray_band_without_judge_promotes_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=False)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _ExplodingJudgeGateway())

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'promoted'
    assert existing.stale is False
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0


@pytest.mark.django_db
def test_curate_above_threshold_supersedes_without_consulting_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate('curator-above-threshold')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='0.850', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=0.970)
    patch_judge_gateway(monkeypatch, _ExplodingJudgeGateway())

    result = CurateMemoryCandidate().execute(
        CurateMemoryCandidateInput(candidate_id=duplicate.id, correlation_id='corr-full-flow'),
    )

    existing.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True
    transition = MemoryTransition.objects.get(
        candidate=duplicate,
        transition_type=MemoryTransitionType.SUPERSEDE,
    )
    assert transition.audit_event.correlation_id == 'corr-full-flow'
    assert transition.audit_event.metadata['transition_type'] == MemoryTransitionType.SUPERSEDE


@pytest.mark.django_db(transaction=True)
def test_embed_candidate_call_has_no_open_transaction_during_curate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-embed-no-txn',
    )
    observed_in_atomic: list[bool] = []
    real_gateway = FakeProviderGateway()

    class _RecordingGateway(FakeProviderGateway):
        def embed(self, data: object) -> object:
            observed_in_atomic.append(connection.in_atomic_block)

            return real_gateway.embed(data)

    monkeypatch.setattr('engram.memory.curation.get_provider_gateway', lambda *_, **__: _RecordingGateway())

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    assert observed_in_atomic == [False]


@pytest.mark.django_db(transaction=True)
def test_judge_decision_call_has_no_open_transaction_during_curate(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    observed_in_atomic: list[bool] = []

    class _RecordingJudgeGateway(FakeProviderGateway):
        def call(self, data: ProviderCallInput) -> ProviderCallResult:
            observed_in_atomic.append(connection.in_atomic_block)

            return ProviderCallResult(
                provider=data.policy.provider,
                model=data.policy.model,
                call_record_id=uuid.uuid4(),
                redaction_state='clean',
                generated_title='',
                generated_body='{"decision": "keep_both"}',
            )

    patch_judge_gateway(monkeypatch, _RecordingJudgeGateway())

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert observed_in_atomic == [False]
    assert existing.stale is False


@pytest.mark.django_db(transaction=True)
def test_curate_memory_candidate_concurrent_execution_creates_exactly_one_memory() -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires real row locking on postgres')
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-concurrent-curate',
    )
    candidate_id = candidate.id
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate_id)))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for started in threads:
        started.start()
    for finished in threads:
        finished.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert Memory.objects.count() == 1
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0
    assert AuditEvent.objects.filter(event_type='MemoryCuratorPromoted').count() == 1


@pytest.mark.django_db
def test_curate_escalates_sensitive_candidate_without_creating_memory_legacy_v0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-sensitive',
        decision_work_contract_version=0,
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())
    provider_call_count = ProviderCallRecord.objects.count()

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'held_escalation'
    assert result.memory is None
    assert candidate.status == CandidateStatus.PROPOSED
    assert Memory.objects.count() == 0
    assert ProviderCallRecord.objects.count() == provider_call_count
    audit = AuditEvent.objects.get(event_type='MemoryCandidateHeldForReview')
    assert audit.actor_type == 'system'
    assert audit.capability == 'memories:review'
    assert audit.target_type == 'memory_candidate'
    assert audit.target_id == str(candidate.id)
    assert audit.metadata['reason'] == 'escalation:security_sensitive'
    assert audit.metadata['candidate_id'] == str(candidate.id)


@pytest.mark.django_db
def test_curate_escalates_org_wide_candidate_without_creating_memory_legacy_v0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Org-wide rollout',
        body=_LONG_BODY,
        content_hash='hash-escalation-org-wide',
        visibility_scope=VisibilityScope.ORGANIZATION,
        decision_work_contract_version=0,
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'held_escalation'
    assert candidate.status == CandidateStatus.PROPOSED
    assert Memory.objects.count() == 0
    audit = AuditEvent.objects.get(event_type='MemoryCandidateHeldForReview')
    assert audit.metadata['reason'] == 'escalation:org_wide_scope'


@pytest.mark.django_db
def test_curate_benign_candidate_promotes_without_escalation() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Networking port',
        body='Use port 8443 not 8080',
        content_hash='hash-escalation-benign',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'promoted'
    assert candidate.status == CandidateStatus.PROMOTED
    assert Memory.objects.count() == 1


@pytest.mark.django_db
def test_curate_escalation_rerun_writes_single_audit_row_legacy_v0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-rerun',
        decision_work_contract_version=0,
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())

    first = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    second = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    third = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    assert first.decision == 'held_escalation'
    assert second.decision == 'held_escalation'
    assert third.decision == 'held_escalation'
    assert AuditEvent.objects.filter(event_type='MemoryCandidateHeldForReview').count() == 1


@pytest.mark.django_db
def test_curate_escalation_holds_even_when_curator_disabled_legacy_v0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization, enabled=False)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-disabled-curator',
        decision_work_contract_version=0,
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'held_escalation'
    assert candidate.status == CandidateStatus.PROPOSED
    assert Memory.objects.count() == 0


@pytest.mark.django_db
def test_plain_promotion_cannot_bypass_open_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, existing, duplicate = seed_atomic_existing_and_duplicate(
        'curator-conflict-promotion-guard'
    )
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    conflict = MemoryConflict.objects.get(candidate=duplicate, resolved_transition__isnull=True)

    with pytest.raises(MemoryTransitionError) as error:
        PromoteMemoryCandidate().execute(transition_request(duplicate))

    duplicate.refresh_from_db()
    conflict.refresh_from_db()
    assert error.value.code == 'unresolved_conflict'
    assert duplicate.status == CandidateStatus.PROPOSED
    assert duplicate.promoted_memory_id is None
    assert conflict.resolved_transition_id is None
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id).exists()


@pytest.mark.django_db
def test_curate_passthrough_route_audits_curator_promoted() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization, enabled=False)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Networking port',
        body='Use port 8443 not 8080',
        content_hash='hash-audit-passthrough',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.actor_type == 'system'
    assert audit.actor_id == 'curator'
    assert audit.target_type == 'memory_candidate'
    assert audit.target_id == str(candidate.id)
    assert audit.metadata['decision'] == 'passthrough'
    assert audit.metadata['memory_id'] == str(result.memory.id)
    assert audit.metadata['candidate_id'] == str(candidate.id)
    assert audit.metadata['threshold'] is None


@pytest.mark.django_db
def test_curate_no_duplicate_route_audits_curator_promoted() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-audit-no-dup',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['decision'] == 'no_duplicate'
    assert audit.metadata['memory_id'] == str(result.memory.id)
    assert audit.metadata['threshold'] == '0.850'
    assert 'judge' not in audit.metadata


@pytest.mark.django_db
def test_curate_embedding_unavailable_route_audits_curator_promoted() -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-audit-embed-unavailable',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['decision'] == 'embedding_unavailable'
    assert audit.metadata['memory_id'] == str(result.memory.id)
    assert audit.metadata['threshold'] is None


@pytest.mark.django_db
def test_curate_judge_keep_both_route_audits_curator_promoted() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['decision'] == 'judge_keep_both'
    assert audit.metadata['memory_id'] == str(result.memory.id)
    assert audit.metadata['threshold'] == '1.050'
    assert 'judge' in audit.metadata


@pytest.mark.django_db
def test_curate_replay_does_not_double_audit_curator_promoted() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-audit-replay',
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    assert AuditEvent.objects.filter(event_type='MemoryCuratorPromoted').count() == 1


@pytest.mark.django_db
def test_curate_judge_outcome_audit_includes_judge_context(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    judge_policy = create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "keep_both"}'))

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    judge = audit.metadata['judge']
    assert judge['policy_id'] == str(judge_policy.id)
    assert judge['policy_version'] == judge_policy.version
    assert judge['provider'] == judge_policy.provider
    assert judge['model'] == judge_policy.model
    assert judge['provider_call_record_id']
    assert judge['candidate']['title'] == duplicate.title
    assert judge['candidate']['body_sha256'] == hashlib.sha256(_LONG_BODY.encode()).hexdigest()
    assert judge['candidate']['body_length'] == len(_LONG_BODY)
    assert judge['existing_memory']['memory_id'] == str(existing.id)
    assert judge['existing_memory']['title'] == existing.title
    assert judge['existing_memory']['body_sha256'] == hashlib.sha256(_LONG_BODY.encode()).hexdigest()
    assert judge['existing_memory']['body_length'] == len(_LONG_BODY)


@pytest.mark.django_db
def test_curate_redacts_secret_from_all_audit_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    secret_body = f'{_LONG_BODY} Token sk-abcdef0123456789 is embedded here.'
    existing = promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Retrieval ranking',
            body=secret_body,
            content_hash='hash-existing-secret',
        ),
    )
    complete_memory_embedding(existing)
    duplicate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=secret_body,
        content_hash='hash-duplicate-secret',
    )
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "reject"}'))

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    audit = AuditEvent.objects.get(event_type='MemoryAutoRejected', target_id=str(duplicate.id))
    assert audit.metadata['judge']['candidate']['body_sha256'] == hashlib.sha256(secret_body.encode()).hexdigest()
    for audit_event in AuditEvent.objects.all():
        assert 'sk-abcdef0123456789' not in json.dumps(audit_event.metadata)


@pytest.mark.django_db
def test_curate_audit_collects_evidence_source_ids() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-evidence-ids',
        evidence=[{'observation_id': 'obs-1', 'supporting_observation_ids': ['obs-2', 'obs-3']}],
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert set(audit.metadata['evidence_source_ids']) == {'obs-1', 'obs-2', 'obs-3'}


@pytest.mark.django_db
def test_curate_audit_dedupes_evidence_source_ids() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-evidence-dedup',
        evidence=[
            {'observation_id': 'obs-dup'},
            {'observation_id': 'obs-dup'},
            {'supporting_observation_ids': ['obs-dup', 'obs-unique']},
        ],
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['evidence_source_ids'] == ['obs-dup', 'obs-unique']


@pytest.mark.django_db
def test_curate_audit_caps_evidence_source_ids_at_fifty_in_order() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    ids = [f'obs-{index}' for index in range(60)]
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-evidence-cap',
        evidence=[{'supporting_observation_ids': ids}],
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['evidence_source_ids'] == ids[:50]


@pytest.mark.django_db
def test_curate_audit_excludes_conflict_entries_from_evidence_source_ids() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-evidence-conflict',
        evidence=[
            {'observation_id': 'obs-real'},
            {'type': 'conflict', 'memory_id': 'mem-should-be-excluded'},
        ],
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    audit = AuditEvent.objects.get(event_type='MemoryCuratorPromoted')
    assert audit.metadata['evidence_source_ids'] == ['obs-real']


# ---------------------------------------------------------------------------
# C5.3 - Work-Driven Automatic Decision Orchestration (DecideMemoryCandidate)
# RED tests for the new orchestrator. These drive the production task entry
# point `process_candidate_decision_work_v1` with the rollout gate enabled and
# stub the frozen C5.1/C5.2 seams (embedding, shortlist, evidence, judge) so the
# orchestration/persistence/atomicity contract is exercised in isolation.
# ---------------------------------------------------------------------------

_EMBEDDING = (0.1,) + (0.0,) * 1535


@pytest.mark.django_db
def test_publish_new_outcome_commits_promotion_decision_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('publish')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='publish')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.outcome == CurationOutcome.PUBLISH_NEW
    assert decision.reason_code == CurationReasonCode.DISTINCT_CLAIM
    assert decision.target_memory_version_id is None
    assert decision.transition_id is not None
    assert decision.conflict_id is None
    assert decision.evidence_tier == EvidenceTier.SUPPORTED
    assert decision.input_fingerprint == work.input_fingerprint
    assert decision.evidence_manifest_hash == work.input_snapshot['evidence_manifest_hash']
    assert decision.comparison_manifest_hash == shortlist.manifest_hash
    assert decision.provider_call_record_id == call.id
    assert decision.policy_id == policy.id
    assert decision.policy_version == policy.version
    assert decision.effective_visibility_scope == VisibilityScope.PROJECT
    assert decision.effective_team_id is None
    assert decision.payload_hash
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id is not None
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_merge_evidence_outcome_commits_merge_decision_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('merge')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='merge', title='Cache eviction policy', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='merge', title='Cache eviction approach', body='The hot cache tier evicts oldest entries first.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('merge_evidence', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.outcome == CurationOutcome.MERGE_EVIDENCE
    assert decision.reason_code == CurationReasonCode.EQUIVALENT_CLAIM
    assert decision.target_memory_version_id == target_version.id
    assert decision.transition_id is not None
    assert decision.comparison_manifest_hash == shortlist.manifest_hash
    candidate.refresh_from_db()
    memory.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert MemoryVersion.objects.filter(memory=memory).count() == 2
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_revise_memory_outcome_commits_revision_decision_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('revise')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='revise', title='Deployment rollback path', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='revise', title='Deployment rollback update', body='Rollback now drains connections first.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('revise_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.outcome == CurationOutcome.REVISE_MEMORY
    assert decision.reason_code == CurationReasonCode.SAME_SUBJECT_REVISION
    assert decision.target_memory_version_id == target_version.id
    assert decision.transition_id is not None
    candidate.refresh_from_db()
    memory.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id == memory.id
    assert MemoryVersion.objects.filter(memory=memory).count() == 2
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_supersede_memory_outcome_commits_supersession_decision_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('supersede')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='supersede', title='Broker prefetch value', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='supersede', title='Broker prefetch replacement', body='Prefetch is now set to one per worker.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('supersede_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.outcome == CurationOutcome.SUPERSEDE_MEMORY
    assert decision.reason_code == CurationReasonCode.ORDERED_REPLACEMENT
    assert decision.target_memory_version_id == target_version.id
    assert decision.transition_id is not None
    candidate.refresh_from_db()
    memory.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id is not None
    assert candidate.promoted_memory_id != memory.id
    assert memory.stale is True
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_open_conflict_outcome_commits_conflict_decision_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('conflict')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='conflict', title='Primary region choice', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='conflict', title='Primary region choice', body='The primary region is now the eastern zone.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('open_conflict', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.outcome == CurationOutcome.OPEN_CONFLICT
    assert decision.reason_code == CurationReasonCode.SAME_SCOPE_CONTRADICTION
    assert decision.conflict_id is not None
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=memory)
    assert conflict.resolved_transition_id is None
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_publish_new_reredacts_stale_secret_before_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = 'sk-livetoken0123456789abcdef'
    scope = orch.orchestrator_scope('reredact')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(
        scope,
        suffix='reredact',
        title='Gateway credential rotation',
        body=f'The staging gateway key is {secret} and must be rotated each release.',
    )
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    candidate.refresh_from_db()
    assert candidate.promoted_memory_id is not None
    memory = Memory.objects.get(id=candidate.promoted_memory_id)
    version = MemoryVersion.objects.get(memory=memory, version=memory.current_version)
    document = RetrievalDocument.objects.get(memory_version=version)
    assert secret not in memory.title
    assert secret not in memory.body
    assert secret not in json.dumps(memory.metadata)
    assert secret not in version.body
    assert secret not in document.full_text
    assert secret not in json.dumps(document.metadata)


@pytest.mark.django_db
def test_publish_new_persists_gate_narrowed_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('narrow')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(
        scope,
        suffix='narrow',
        title='Org-wide retry policy',
        body=_LONG_BODY,
        visibility_scope=VisibilityScope.ORGANIZATION,
    )
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    candidate.refresh_from_db()
    memory = Memory.objects.get(id=candidate.promoted_memory_id)
    decision = orch.curation_decisions_for(candidate)[0]
    assert memory.visibility_scope != VisibilityScope.ORGANIZATION
    assert memory.visibility_scope == decision.effective_visibility_scope
    assert memory.team_id == decision.effective_team_id


@pytest.mark.django_db
def test_exact_duplicate_no_new_evidence_settles_without_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('exactdup')
    memory = orch.target_memory(scope, suffix='exactdup', title='Cache eviction policy', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='exactdup', title='Cache eviction policy', body=_LONG_BODY
    )
    source = MemoryCandidateSource.objects.get(candidate=candidate)
    MemoryVersionSource.objects.create(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=target_version.memory.team_id,
        memory_version=target_version,
        candidate_source=source,
        source_content_hash=source.anchors_hash,
    )
    orch.install_deterministic_only(monkeypatch)
    before_versions = MemoryVersion.objects.filter(memory=memory).count()

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert MemoryVersion.objects.filter(memory=memory).count() == before_versions
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.MERGE_EVIDENCE
    assert decisions[0].reason_code == CurationReasonCode.EXACT_DUPLICATE_NO_NEW_EVIDENCE
    assert decisions[0].transition_id is None
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id == memory.id
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_reject_candidate_and_superseded_generation_complete_as_product_no_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = orch.orchestrator_scope('nosignal')
    reject_candidate, reject_work, reject_run = orch.subject_candidate(
        scope, suffix='reject', visibility_scope=VisibilityScope.SESSION
    )
    orch.install_deterministic_only(monkeypatch)

    _reject_result, reject_error = orch.run_decision(reject_work, reject_run)

    assert reject_error is None
    reject_decisions = orch.curation_decisions_for(reject_candidate)
    assert len(reject_decisions) == 1
    assert reject_decisions[0].outcome == CurationOutcome.REJECT_CANDIDATE
    assert reject_decisions[0].reason_code == CurationReasonCode.NON_DURABLE_SESSION_SCOPE
    assert reject_decisions[0].transition_id is None
    reject_candidate.refresh_from_db()
    reject_work.refresh_from_db()
    assert reject_candidate.status == CandidateStatus.REJECTED
    assert reject_work.disposition == WorkflowWorkDisposition.COMPLETE
    assert reject_work.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL

    gen_candidate, gen_work, gen_run = orch.subject_candidate(scope, suffix='generation')
    orch.mutate_candidate_generation(gen_candidate, new_title='Rewritten claim after fresher evidence arrived')
    orch.install_deterministic_only(monkeypatch)

    _gen_result, gen_error = orch.run_decision(gen_work, gen_run)

    assert gen_error is None
    assert orch.curation_decisions_for(gen_candidate) == []
    gen_candidate.refresh_from_db()
    gen_work.refresh_from_db()
    assert gen_candidate.status == CandidateStatus.PROPOSED
    assert gen_work.disposition == WorkflowWorkDisposition.COMPLETE
    assert gen_work.resolution_reason == WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED


@pytest.mark.django_db
def test_deterministic_terminal_bypasses_shortlist_and_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('bypass')
    candidate, work, run = orch.subject_candidate(scope, suffix='bypass', visibility_scope=VisibilityScope.SESSION)
    orch.install_deterministic_only(monkeypatch)

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.REJECT_CANDIDATE
    assert decisions[0].reason_code == CurationReasonCode.NON_DURABLE_SESSION_SCOPE
    assert (
        ProviderCallRecord.objects.filter(
            organization=scope.organization,
            request_id__startswith='curation-decision',
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_crash_after_embedding_preserves_proposed_candidate_and_retryable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = orch.orchestrator_scope('f20')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='f20')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def crash() -> None:
        raise RuntimeError('crash after embedding before judgment')

    orch.install_fault(monkeypatch, 'after_embedding', crash)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_crash_after_judge_response_creates_no_semantic_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('f22')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='f22')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def crash() -> None:
        raise RuntimeError('process died after judge response')

    orch.install_fault(monkeypatch, 'after_judge', crash)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_target_version_advance_fences_stale_judgment(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('f23')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='f23', title='Queue backpressure policy', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='f23', title='Queue backpressure revision', body='Backpressure now trips at 80 percent depth.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('revise_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def advance() -> None:
        orch.advance_target_memory(memory, title='Queue backpressure policy v2', body='Thresholds were revised again.')

    orch.install_fault(monkeypatch, 'before_transition', advance)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('outcome', 'service', 'candidate_tier'),
    [
        ('publish_new', 'PromoteMemoryCandidate', 'supported'),
        ('merge_evidence', 'MergeMemoryCandidate', 'supported'),
        ('supersede_memory', 'SupersedeMemoryWithCandidate', 'corroborated'),
    ],
)
def test_cp4_fault_at_each_transition_boundary_rolls_back_work_completion(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    service: str,
    candidate_tier: str,
) -> None:
    scope = orch.orchestrator_scope(f'cp4-{outcome}')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    target_version = None
    if outcome == 'publish_new':
        shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
        evidence = orch.stub_evidence(candidate_tier=candidate_tier)
    else:
        memory = orch.target_memory(scope, suffix=outcome, title=f'{outcome} target', body=_LONG_BODY)
        target_version = orch.current_version(memory)
        shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
        evidence = orch.stub_evidence(candidate_tier=candidate_tier, target=target_version, candidate_newer=True)
    candidate, work, run = orch.subject_candidate(scope, suffix=outcome)
    verdict = orch.stub_verdict(outcome, target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )
    orch.patch_cp4_service(monkeypatch, service)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_crash_after_commit_replays_one_decision_and_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('f24')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='f24')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _first_result, first_error = orch.run_decision(work, run)

    assert first_error is None
    committed = orch.curation_decisions_for(candidate)
    assert len(committed) == 1
    first = committed[0]

    orch.run_decision(work, run)

    replayed = orch.curation_decisions_for(candidate)
    assert len(replayed) == 1
    assert replayed[0].id == first.id
    assert replayed[0].transition_id == first.transition_id
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_concurrent_candidate_decisions_on_one_target_relist_the_loser(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('concurrent')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='concurrent', title='Retry ceiling policy', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    loser_candidate, loser_work, loser_run = orch.subject_candidate(
        scope, suffix='loser', title='Retry ceiling revision A', body='The retry ceiling should drop to three.'
    )
    winner_candidate, winner_work, winner_run = orch.subject_candidate(
        scope, suffix='winner', title='Retry ceiling revision B', body='The retry ceiling should drop to four.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('revise_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    triggered: list[bool] = []

    def winner_runs() -> None:
        if triggered:
            return
        triggered.append(True)
        orch.run_decision(winner_work, winner_run)

    orch.install_fault(monkeypatch, 'before_transition', winner_runs)

    _result, error = orch.run_decision(loser_work, loser_run)

    assert error is not None
    assert orch.curation_decisions_for(loser_candidate) == []
    loser_candidate.refresh_from_db()
    loser_work.refresh_from_db()
    assert loser_candidate.status == CandidateStatus.PROPOSED
    assert loser_work.execution_state != WorkflowWorkExecutionState.SETTLED
    winner_decisions = orch.curation_decisions_for(winner_candidate)
    assert len(winner_decisions) == 1
    assert winner_decisions[0].outcome == CurationOutcome.REVISE_MEMORY
    winner_candidate.refresh_from_db()
    assert winner_candidate.status == CandidateStatus.PROMOTED


@pytest.mark.django_db
def test_expired_worker_fence_cannot_apply_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('expired')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='expired')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def expire() -> None:
        orch.steal_work_lease(work)

    orch.install_fault(monkeypatch, 'before_transition', expire)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None


@pytest.mark.django_db
def test_reconciler_restores_one_missing_decision_work_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('reconcile')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, _source, _session = provenanced_candidate_in_scope(
        scope.organization,
        scope.project,
        scope.team,
        suffix='reconcile',
        title='Idempotent replay contract',
        body=_LONG_BODY,
    )
    missing = WorkflowWork.objects.filter(subject_id=candidate.id, work_type=WorkflowWorkType.CANDIDATE_DECISION)
    assert missing.count() == 0

    result = ReconcileCandidateDecisionWork().execute(as_of=timezone.now())

    assert result.queued == 1
    works = WorkflowWork.objects.filter(subject_id=candidate.id, work_type=WorkflowWorkType.CANDIDATE_DECISION)
    assert works.count() == 1
    work = works.get()
    run = WorkflowRun.objects.get(work=work, status=WorkflowRunStatus.QUEUED)
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert len(orch.curation_decisions_for(candidate)) == 1


@pytest.mark.django_db
def test_genuine_conflict_predicate_opens_canonical_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('genuine')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='genuine', title='Default timeout value', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='genuine', title='Default timeout value', body='The default request timeout is ninety seconds.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('open_conflict', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=memory)
    assert conflict.memory_version_id == target_version.id
    assert conflict.resolved_transition_id is None
    assert conflict.opened_transition_id is not None
    assert conflict.semantic_link_id is not None
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.OPEN_CONFLICT
    assert decisions[0].conflict_id == conflict.id


@pytest.mark.django_db
def test_temporal_precedence_supersedes_instead_of_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('precedence')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='precedence', title='Feature flag default', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='precedence', title='Feature flag default on', body='The feature flag now defaults to enabled.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('supersede_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert MemoryConflict.objects.filter(candidate=candidate).count() == 0
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.SUPERSEDE_MEMORY


@pytest.mark.django_db
def test_different_applicability_is_not_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('applicability')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='applicability', title='Staging cache size', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='applicability', title='Production cache size', body='The production cache holds two gigabytes.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = CurationJudgeVerdictV1(
        schema_version=1,
        outcome='publish_new',
        relation='compatible_distinct',
        target_memory_version_id=None,
        candidate_evidence_refs=('cref-1',),
        comparisons=(CurationJudgeComparisonV1(target_version.id, 'compatible_distinct', ('tref-1',)),),
        applicability='different',
        temporal_order='not_applicable',
        reason_code='distinct_claim',
        reason='different applicability so publish separately',
    )
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert MemoryConflict.objects.filter(candidate=candidate).count() == 0
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.PUBLISH_NEW
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED


@pytest.mark.django_db
def test_existing_open_conflict_pair_is_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('duplicate')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='duplicate', title='Log retention window', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='duplicate', title='Log retention window', body='Logs are retained for thirty days now.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('open_conflict', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _first_result, first_error = orch.run_decision(work, run)

    assert first_error is None
    assert MemoryConflict.objects.filter(candidate=candidate, memory=memory).count() == 1
    assert len(orch.curation_decisions_for(candidate)) == 1

    orch.run_decision(work, run)

    assert MemoryConflict.objects.filter(candidate=candidate, memory=memory).count() == 1
    assert len(orch.curation_decisions_for(candidate)) == 1


@pytest.mark.django_db
def test_empty_shortlist_publish_is_stale_when_new_memory_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('revalidate')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='revalidate')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='supported')
    verdict = orch.stub_verdict('publish_new')
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def appear() -> None:
        orch.target_memory(scope, suffix='revalidate-intruder', title='Concurrent current memory', body=_LONG_BODY)

    orch.install_fault(monkeypatch, 'before_transition', appear)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert isinstance(error, MemoryTransitionError)
    assert error.code == 'stale_decision'
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None


@pytest.mark.django_db
def test_existing_open_conflict_pair_settles_idempotently_across_generations(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('idempair')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='idempair', title='Primary region choice', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='idempair', title='Primary region choice', body='The primary region is now the eastern zone.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('open_conflict', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _first, first_error = orch.run_decision(work, run)

    assert first_error is None
    open_pairs = MemoryConflict.objects.filter(candidate=candidate, memory=memory, resolved_transition__isnull=True)
    assert open_pairs.count() == 1

    orch.mutate_candidate_generation(candidate, new_title='Primary region choice with revised wording')
    generation, run2 = orch.next_generation_work(candidate)
    memory.refresh_from_db()
    target_version2 = orch.current_version(memory)
    shortlist2 = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence2 = orch.stub_evidence(candidate_tier='supported', target=target_version2, target_tier='supported')
    verdict2 = orch.stub_verdict('open_conflict', target=target_version2)
    judge2 = orch.stub_judge_result(verdict2, call, policy, shortlist2)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist2, evidence=evidence2, judge_result=judge2
    )

    _second, second_error = orch.run_decision(generation, run2)

    assert second_error is None
    assert open_pairs.count() == 1
    generation.refresh_from_db()
    assert generation.disposition == WorkflowWorkDisposition.COMPLETE
    assert generation.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 2
    assert {decision.outcome for decision in decisions} == {CurationOutcome.OPEN_CONFLICT}


# ---------------------------------------------------------------------------
# C5.3 combined review round - regression tests for the audited defects.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_settlement_request_uses_frozen_decision_work_idempotency_key() -> None:
    scope = orch.orchestrator_scope('m10')
    candidate, work, _run = orch.subject_candidate(scope, suffix='m10')

    request = orch.decide_memory_candidate()()._request(work, candidate)

    assert request.idempotency_key == f'decision-work:{work.id}:settle:v1'


@pytest.mark.django_db
def test_open_conflict_decision_references_opened_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('m9')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='m9', title='Replica placement rule', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='m9', title='Replica placement rule', body='Replicas now span three availability zones.'
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')
    verdict = orch.stub_verdict('open_conflict', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=memory)
    decision = orch.curation_decisions_for(candidate)[0]
    assert decision.conflict_id == conflict.id
    assert decision.transition_id == conflict.opened_transition_id
    assert decision.transition_id is not None


@pytest.mark.django_db
def test_stale_shortlist_target_transition_fences_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('b2')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(scope, suffix='b2', title='Index build strategy', body=_LONG_BODY)
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope, suffix='b2', title='Index build revision', body='Indexes now build concurrently online.'
    )
    entry = orch.shortlist_entry(memory)
    orch.advance_target_memory(
        memory, title='Index build strategy v2', body='The strategy changed again before judgment.'
    )
    shortlist = orch.stub_shortlist(entry)
    evidence = orch.stub_evidence(candidate_tier='corroborated', target=target_version, candidate_newer=True)
    verdict = orch.stub_verdict('revise_memory', target=target_version)
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert isinstance(error, MemoryTransitionError)
    assert error.code == 'stale_decision'
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED


def _reject_verdict() -> CurationJudgeVerdictV1:
    return CurationJudgeVerdictV1(
        schema_version=1,
        outcome='reject_candidate',
        relation='unsupported',
        target_memory_version_id=None,
        candidate_evidence_refs=(),
        comparisons=(),
        applicability='different',
        temporal_order='not_applicable',
        reason_code='unsupported_claim',
        reason='no durable support for this claim',
    )


@pytest.mark.django_db
def test_model_reject_unsupported_records_rejection_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('b3ok')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='b3ok')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='none')
    judge = orch.stub_judge_result(_reject_verdict(), call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    _result, error = orch.run_decision(work, run)

    assert error is None
    decisions = orch.curation_decisions_for(candidate)
    assert len(decisions) == 1
    assert decisions[0].outcome == CurationOutcome.REJECT_CANDIDATE
    assert decisions[0].reason_code == CurationReasonCode.UNSUPPORTED_CLAIM
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.REJECTED
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL


@pytest.mark.django_db
def test_model_reject_of_superseded_generation_settles_without_rejecting(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = orch.orchestrator_scope('b3gen')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    candidate, work, run = orch.subject_candidate(scope, suffix='b3gen')
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    evidence = orch.stub_evidence(candidate_tier='none')
    judge = orch.stub_judge_result(_reject_verdict(), call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch, embedding=_EMBEDDING, shortlist=shortlist, evidence=evidence, judge_result=judge
    )

    def mutate() -> None:
        orch.mutate_candidate_generation(candidate, new_title='Fresher evidence arrived after the older rejection')

    orch.install_fault(monkeypatch, 'before_transition', mutate)

    _result, error = orch.run_decision(work, run)

    assert error is None
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED


@pytest.mark.django_db
def test_model_redundant_rejection_retries_when_shortlist_target_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = orch.orchestrator_scope('reject-stale-target')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(
        scope,
        suffix='reject-stale-target',
        title='Retry backoff policy',
        body=_LONG_BODY,
    )
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope,
        suffix='reject-stale-target',
        title='Retry backoff policy duplicate',
        body='The same retry backoff policy is repeated here.',
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(
        candidate_tier='supported',
        target=target_version,
        target_tier='supported',
    )
    verdict = CurationJudgeVerdictV1(
        schema_version=1,
        outcome='reject_candidate',
        relation='redundant',
        target_memory_version_id=target_version.id,
        candidate_evidence_refs=('cref-1',),
        comparisons=(
            CurationJudgeComparisonV1(
                memory_version_id=target_version.id,
                relation='redundant',
                target_evidence_refs=('tref-1',),
            ),
        ),
        applicability='same',
        temporal_order='not_applicable',
        reason_code='redundant_claim',
        reason='the frozen target already contains this claim',
    )
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch,
        embedding=_EMBEDDING,
        shortlist=shortlist,
        evidence=evidence,
        judge_result=judge,
    )

    def advance() -> None:
        orch.advance_target_memory(
            memory,
            title='Retry backoff policy v2',
            body='The target changed after the redundant verdict.',
        )

    orch.install_fault(monkeypatch, 'before_transition', advance)

    _result, error = orch.run_decision(work, run)

    candidate.refresh_from_db()
    observed = (
        getattr(error, 'code', None),
        len(orch.curation_decisions_for(candidate)),
        candidate.status,
    )
    assert observed == (
        'stale_decision',
        0,
        CandidateStatus.PROPOSED,
    )


@pytest.mark.django_db
def test_deterministic_exact_identity_does_not_rebase_onto_advanced_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = orch.orchestrator_scope('deterministic-stale-target')
    memory = orch.target_memory(
        scope,
        suffix='deterministic-stale-target',
        title='Cache eviction policy',
        body=_LONG_BODY,
    )
    candidate, work, run = orch.subject_candidate(
        scope,
        suffix='deterministic-stale-target',
        title='Cache eviction policy',
        body=_LONG_BODY,
    )
    module = orch.curation_module()
    original_gate = module.EvaluateDeterministicCandidateGates
    orch.install_deterministic_only(monkeypatch)

    class AdvancingDeterministicGate:
        def execute(self, work_id: uuid.UUID) -> object:
            result = original_gate().execute(work_id)
            orch.advance_target_memory(
                memory,
                title='Cache eviction policy v2',
                body='A concurrent revision changed eviction to frequency-based admission.',
            )

            return result

    monkeypatch.setattr(
        module,
        'EvaluateDeterministicCandidateGates',
        AdvancingDeterministicGate,
    )

    _result, error = orch.run_decision(work, run)

    assert isinstance(error, MemoryTransitionError)
    assert error.code == 'stale_decision'
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    memory.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED
    assert memory.title == 'Cache eviction policy v2'
    assert memory.body == 'A concurrent revision changed eviction to frequency-based admission.'
    assert memory.current_version == 2


@pytest.mark.django_db
def test_redundant_rejection_revalidates_target_before_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = orch.orchestrator_scope('redundant-stale-target')
    policy = orch.curation_policy(scope)
    call = orch.provider_call_record(scope, policy)
    memory = orch.target_memory(
        scope,
        suffix='redundant-stale-target',
        title='Retry backoff policy',
        body=_LONG_BODY,
    )
    target_version = orch.current_version(memory)
    candidate, work, run = orch.subject_candidate(
        scope,
        suffix='redundant-stale-target',
        title='Retry backoff policy restatement',
        body='The retry policy uses the same bounded exponential backoff.',
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(memory))
    evidence = orch.stub_evidence(
        candidate_tier='supported',
        target=target_version,
        target_tier='supported',
    )
    verdict = CurationJudgeVerdictV1(
        schema_version=1,
        outcome='reject_candidate',
        relation='redundant',
        target_memory_version_id=target_version.id,
        candidate_evidence_refs=('cref-1',),
        comparisons=(
            CurationJudgeComparisonV1(
                target_version.id,
                'redundant',
                ('tref-1',),
            ),
        ),
        applicability='same',
        temporal_order='not_applicable',
        reason_code='redundant_claim',
        reason='the candidate is redundant with the selected current target',
    )
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    orch.install_judged_decision(
        monkeypatch,
        embedding=_EMBEDDING,
        shortlist=shortlist,
        evidence=evidence,
        judge_result=judge,
    )

    def advance() -> None:
        orch.advance_target_memory(
            memory,
            title='Retry backoff policy v2',
            body='The current policy now uses fixed-delay retries without backoff.',
        )

    orch.install_fault(monkeypatch, 'before_transition', advance)

    _result, error = orch.run_decision(work, run)

    assert isinstance(error, MemoryTransitionError)
    assert error.code == 'stale_decision'
    assert orch.curation_decisions_for(candidate) == []
    candidate.refresh_from_db()
    work.refresh_from_db()
    memory.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert work.execution_state != WorkflowWorkExecutionState.SETTLED
    assert memory.current_version == 2
