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

from engram.access.services import EffectiveScope
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput, authorized_retrieval_documents
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    Organization,
    OrganizationSettings,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
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
    supersede_memory_system,
)
from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    FakeProviderGateway,
    ProviderCallInput,
    ProviderCallResult,
)

_LONG_BODY = 'The retrieval pipeline ranks documents by cosine similarity over embeddings.'


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
) -> MemoryCandidate:
    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        source_observation=None,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=visibility_scope,
        evidence=evidence if evidence is not None else [{'kind': 'test'}],
        content_hash=content_hash,
        confidence=Decimal(confidence),
    )


def promote_candidate(candidate: MemoryCandidate) -> Memory:
    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    return result.memory


def set_curator_settings(
    organization: Organization,
    *,
    enabled: bool = True,
    threshold: str = '0.850',
    llm_judge_enabled: bool = False,
) -> None:
    OrganizationSettings.objects.update_or_create(
        organization=organization,
        defaults={
            'curator_enabled': enabled,
            'near_dup_threshold': Decimal(threshold),
            'curator_llm_judge_enabled': llm_judge_enabled,
        },
    )


def create_curation_policy(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    task_type: str = 'curation',
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'Team {task_type} Anthropic',
        provider='anthropic',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-curation-secret',
        hmac_digest='curation-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name=f'{task_type} policy',
        scope='project',
        task_type=task_type,
        provider='anthropic',
        model='claude-judge',
        secret=secret,
        version=1,
    )


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
    duplicate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-duplicate',
    )

    return existing, duplicate


class _JudgeGatewayStub(FakeProviderGateway):
    def __init__(self, body: str) -> None:
        self._body = body

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='',
            generated_body=self._body,
        )


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


def patch_judge_gateway(monkeypatch: pytest.MonkeyPatch, gateway: FakeProviderGateway) -> None:
    monkeypatch.setattr('engram.memory.curation.get_provider_gateway', lambda *_, **__: gateway)


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
def test_supersede_memory_system_marks_loser_and_links_winner() -> None:
    organization, team, project = create_scope()
    loser = promote_candidate(
        create_candidate(organization, team, project, title='Old fact', body=_LONG_BODY, content_hash='hash-loser'),
    )
    winner_candidate = create_candidate(
        organization,
        team,
        project,
        title='New fact',
        body=_LONG_BODY,
        content_hash='hash-winner',
    )
    winner = promote_candidate(winner_candidate)

    link = supersede_memory_system(
        loser,
        winner,
        winner_candidate,
        score=0.97,
        threshold=Decimal('0.850'),
        correlation_id='corr-1',
    )

    loser.refresh_from_db()
    assert loser.stale is True
    assert link is not None
    assert link.link_type == LinkType.SUPERSEDED_BY
    assert link.target == str(winner.id)
    audit = AuditEvent.objects.get(event_type='MemorySuperseded')
    assert audit.actor_type == 'system'
    assert audit.result == AuditResult.RECORDED
    assert audit.target_type == 'memory'
    assert audit.target_id == str(loser.id)
    assert audit.correlation_id == 'corr-1'
    assert audit.metadata['winner_memory_id'] == str(winner.id)
    assert audit.metadata['loser_memory_id'] == str(loser.id)
    assert audit.metadata['near_dup_score'] == '0.97'
    assert audit.metadata['threshold'] == '0.850'


@pytest.mark.django_db
def test_supersede_memory_system_marks_loser_retrieval_document_stale() -> None:
    organization, team, project = create_scope()
    loser = promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Old fact',
            body=_LONG_BODY,
            content_hash='hash-loser-doc',
        ),
    )
    winner_candidate = create_candidate(
        organization,
        team,
        project,
        title='New fact',
        body=_LONG_BODY,
        content_hash='hash-winner-doc',
    )
    winner = promote_candidate(winner_candidate)

    supersede_memory_system(loser, winner, winner_candidate, score=0.97)

    document = RetrievalDocument.objects.get(memory=loser)
    assert document.stale is True


@pytest.mark.django_db
def test_supersede_memory_system_is_idempotent_when_already_stale() -> None:
    organization, team, project = create_scope()
    loser = promote_candidate(
        create_candidate(organization, team, project, title='Old fact', body=_LONG_BODY, content_hash='hash-loser'),
    )
    winner_candidate = create_candidate(
        organization,
        team,
        project,
        title='New fact',
        body=_LONG_BODY,
        content_hash='hash-winner',
    )
    winner = promote_candidate(winner_candidate)
    supersede_memory_system(loser, winner, winner_candidate, score=0.97)

    second = supersede_memory_system(loser, winner, winner_candidate, score=0.97)

    assert second is None
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 1
    assert AuditEvent.objects.filter(event_type='MemorySuperseded').count() == 1


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
def test_curate_supersedes_existing_near_duplicate_memory() -> None:
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
    duplicate = create_candidate(
        organization,
        team,
        project,
        title='Retrieval ranking',
        body=_LONG_BODY,
        content_hash='hash-duplicate',
    )

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
    link = MemoryLink.objects.get(link_type=LinkType.SUPERSEDED_BY)
    assert link.memory_id == existing.id
    assert link.target == str(result.memory.id)
    assert AuditEvent.objects.filter(event_type='MemorySuperseded').count() == 1


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
def test_curate_reject_low_signal_clears_conflict_links() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='noise',
        body='noise',
        content_hash='hash-noise-conflict',
    )
    other_candidate = create_candidate(
        organization,
        team,
        project,
        title='other candidate',
        body=_LONG_BODY,
        content_hash='hash-other-candidate',
    )
    existing = promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Existing fact',
            body=_LONG_BODY,
            content_hash='hash-existing-conflict',
        ),
    )
    conflict_link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=existing,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
        label='contradiction claim',
    )
    survivor_link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=existing,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{other_candidate.id}',
        label='contradiction claim',
    )

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    assert result.decision == 'rejected'
    assert not MemoryLink.objects.filter(id=conflict_link.id).exists()
    assert MemoryLink.objects.filter(id=survivor_link.id).exists()


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
def test_curate_reindexes_existing_memory_embedding_for_dedup() -> None:
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
    document = RetrievalDocument.objects.get(memory=existing)
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=document.memory_version_id))

    assert len(RetrievalDocument.objects.get(memory=existing).embedding_vector) == EMBEDDING_DIMENSION


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
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "merge"}'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True
    link = MemoryLink.objects.get(link_type=LinkType.SUPERSEDED_BY)
    assert link.memory_id == existing.id
    assert AuditEvent.objects.filter(event_type='MemorySuperseded').count() == 1


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
def test_curate_judge_contradicts_holds_candidate_and_links(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    duplicate.refresh_from_db()
    assert result.decision == 'held_conflict'
    assert result.memory is None
    assert duplicate.status == CandidateStatus.PROPOSED
    conflict_entries = [entry for entry in duplicate.evidence if entry.get('type') == 'conflict']
    assert len(conflict_entries) == 1
    assert conflict_entries[0]['memory_id'] == str(existing.id)
    assert conflict_entries[0]['reason'] == 'opposite claim'
    link = MemoryLink.objects.get(link_type=LinkType.CONFLICTS_WITH)
    assert link.memory_id == existing.id
    assert link.target == f'candidate:{duplicate.id}'
    audit = AuditEvent.objects.get(event_type='MemoryConflictDetected')
    assert audit.actor_type == 'system'
    assert audit.capability == 'memories:review'
    assert audit.target_type == 'memory_candidate'
    assert audit.target_id == str(duplicate.id)
    assert audit.metadata['candidate_id'] == str(duplicate.id)
    assert audit.metadata['memory_id'] == str(existing.id)
    assert audit.metadata['reason'] == 'opposite claim'
    assert existing.status == MemoryStatus.APPROVED
    assert existing.stale is False
    assert existing.refuted is False
    documents = authorized_retrieval_documents(organization, project, build_scope(organization, team, project))
    assert any(document.memory_id == existing.id for document in documents)


@pytest.mark.django_db
def test_curate_judge_contradicts_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    second = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    third = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    duplicate.refresh_from_db()
    assert second.decision == 'held_conflict'
    assert third.decision == 'held_conflict'
    conflict_entries = [entry for entry in duplicate.evidence if entry.get('type') == 'conflict']
    assert len(conflict_entries) == 1
    assert MemoryLink.objects.filter(link_type=LinkType.CONFLICTS_WITH).count() == 1
    assert AuditEvent.objects.filter(event_type='MemoryConflictDetected').count() == 1


@pytest.mark.django_db
def test_curate_judge_contradicts_redacts_reason_before_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(
        monkeypatch,
        _JudgeGatewayStub(
            '{"decision": "contradicts", "reason": "opposite of token sk-abcdef0123456789 already stored"}',
        ),
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    duplicate.refresh_from_db()
    conflict_entry = next(entry for entry in duplicate.evidence if entry.get('type') == 'conflict')
    assert 'sk-abcdef0123456789' not in conflict_entry['reason']
    assert '[REDACTED]' in conflict_entry['reason']
    audit = AuditEvent.objects.get(event_type='MemoryConflictDetected')
    assert 'sk-abcdef0123456789' not in audit.metadata['reason']
    assert '[REDACTED]' in audit.metadata['reason']


@pytest.mark.django_db
def test_curate_judge_contradicts_truncates_stored_reason_to_200_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    long_reason = 'x' * 250
    patch_judge_gateway(
        monkeypatch,
        _JudgeGatewayStub(json.dumps({'decision': 'contradicts', 'reason': long_reason})),
    )

    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    duplicate.refresh_from_db()
    conflict_entry = next(entry for entry in duplicate.evidence if entry.get('type') == 'conflict')
    assert len(conflict_entry['reason']) <= 200
    audit = AuditEvent.objects.get(event_type='MemoryConflictDetected')
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
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='0.850', llm_judge_enabled=True)
    existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _ExplodingJudgeGateway())

    result = CurateMemoryCandidate().execute(
        CurateMemoryCandidateInput(candidate_id=duplicate.id, correlation_id='corr-full-flow'),
    )

    existing.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True
    audit = AuditEvent.objects.get(event_type='MemorySuperseded')
    assert audit.correlation_id == 'corr-full-flow'
    assert audit.metadata['threshold'] == '0.850'


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


@pytest.mark.django_db
def test_curate_escalates_sensitive_candidate_without_creating_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-sensitive',
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'held_escalation'
    assert result.memory is None
    assert candidate.status == CandidateStatus.PROPOSED
    assert Memory.objects.count() == 0
    assert ProviderCallRecord.objects.count() == 0
    audit = AuditEvent.objects.get(event_type='MemoryCandidateHeldForReview')
    assert audit.actor_type == 'system'
    assert audit.capability == 'memories:review'
    assert audit.target_type == 'memory_candidate'
    assert audit.target_id == str(candidate.id)
    assert audit.metadata['reason'] == 'escalation:security_sensitive'
    assert audit.metadata['candidate_id'] == str(candidate.id)


@pytest.mark.django_db
def test_curate_escalates_org_wide_candidate_without_creating_memory(monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_curate_escalation_rerun_writes_single_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-rerun',
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
def test_curate_escalation_holds_even_when_curator_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    set_curator_settings(organization, enabled=False)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='Deploy notes',
        body='Remember to rotate the client secret before the release goes out',
        content_hash='hash-escalation-disabled-curator',
    )
    patch_judge_gateway(monkeypatch, _ExplodingGateway())

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    candidate.refresh_from_db()
    assert result.decision == 'held_escalation'
    assert candidate.status == CandidateStatus.PROPOSED
    assert Memory.objects.count() == 0


@pytest.mark.django_db
def test_promote_memory_candidate_clears_conflict_links_at_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    _existing, duplicate = seed_existing_and_duplicate(organization, team, project)
    patch_judge_gateway(monkeypatch, _JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))
    assert MemoryLink.objects.filter(link_type=LinkType.CONFLICTS_WITH).count() == 1

    PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=duplicate.id))

    assert MemoryLink.objects.filter(link_type=LinkType.CONFLICTS_WITH).count() == 0


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
    promote_candidate(
        create_candidate(
            organization,
            team,
            project,
            title='Retrieval ranking',
            body=secret_body,
            content_hash='hash-existing-secret',
        ),
    )
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
