from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
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
    embed_candidate,
    find_near_duplicate,
    is_low_signal,
    supersede_memory_system,
)
from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope

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
) -> MemoryCandidate:
    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        source_observation=None,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[{'kind': 'test'}],
        content_hash=content_hash,
        confidence=Decimal(confidence),
    )


def promote_candidate(candidate: MemoryCandidate) -> Memory:
    result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))

    return result.memory


def set_curator_settings(organization: Organization, *, enabled: bool = True, threshold: str = '0.850') -> None:
    OrganizationSettings.objects.update_or_create(
        organization=organization,
        defaults={'curator_enabled': enabled, 'near_dup_threshold': Decimal(threshold)},
    )


def test_is_low_signal_flags_short_empty_and_title_echo_bodies() -> None:
    assert is_low_signal(MemoryCandidate(title='t', body='')) is True
    assert is_low_signal(MemoryCandidate(title='t', body='   ')) is True
    assert is_low_signal(MemoryCandidate(title='t', body='too short')) is True
    assert is_low_signal(MemoryCandidate(title=_LONG_BODY, body=_LONG_BODY)) is True


def test_is_low_signal_passes_substantive_body() -> None:
    assert is_low_signal(MemoryCandidate(title='Retrieval ranking', body=_LONG_BODY)) is False


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
    assert len(vector) == 64


@pytest.mark.django_db
def test_supersede_memory_system_marks_loser_and_links_winner() -> None:
    organization, team, project = create_scope()
    loser = promote_candidate(
        create_candidate(organization, team, project, title='Old fact', body=_LONG_BODY, content_hash='hash-loser'),
    )
    winner = promote_candidate(
        create_candidate(organization, team, project, title='New fact', body=_LONG_BODY, content_hash='hash-winner'),
    )

    link = supersede_memory_system(loser, winner, request_id='req-1', correlation_id='corr-1', score=0.97)

    loser.refresh_from_db()
    assert loser.stale is True
    assert link is not None
    assert link.link_type == LinkType.SUPERSEDED_BY
    assert link.target == str(winner.id)
    audit = AuditEvent.objects.get(event_type='MemorySuperseded')
    assert audit.actor_type == 'system'
    assert audit.result == AuditResult.RECORDED
    assert audit.target_id == str(loser.id)
    assert audit.metadata['winner_memory_id'] == str(winner.id)
    assert audit.metadata['near_dup_score'] == '0.97'


@pytest.mark.django_db
def test_supersede_memory_system_is_idempotent_when_already_stale() -> None:
    organization, team, project = create_scope()
    loser = promote_candidate(
        create_candidate(organization, team, project, title='Old fact', body=_LONG_BODY, content_hash='hash-loser'),
    )
    winner = promote_candidate(
        create_candidate(organization, team, project, title='New fact', body=_LONG_BODY, content_hash='hash-winner'),
    )
    supersede_memory_system(loser, winner, score=0.97)

    second = supersede_memory_system(loser, winner, score=0.97)

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
def test_curate_disabled_passes_through_to_plain_promotion() -> None:
    organization, team, project = create_scope()
    create_embedding_policy(organization, team, project)
    set_curator_settings(organization, enabled=False)
    candidate = create_candidate(
        organization,
        team,
        project,
        title='noise',
        body='noise',
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

    assert len(RetrievalDocument.objects.get(memory=existing).embedding_vector) == 64
