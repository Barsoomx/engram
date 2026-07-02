from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
import structlog
from django.db import connection

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


def test_parse_curation_decision_defaults_keep_both_for_unknown_or_unparseable() -> None:
    assert parse_curation_decision('not json at all') == 'keep_both'
    assert parse_curation_decision('{"decision": "explode"}') == 'keep_both'
    assert parse_curation_decision('[]') == 'keep_both'
    assert parse_curation_decision('{}') == 'keep_both'


def test_curation_judge_system_prompt_requires_reason() -> None:
    prompt = curation_judge_system_prompt()

    assert '"reason"' in prompt
    assert '"decision"' in prompt


def test_parse_curation_reason_reads_reason() -> None:
    assert parse_curation_reason('{"decision": "merge", "reason": "same fact"}') == 'same fact'
    assert parse_curation_reason('{"decision": "merge"}') == ''
    assert parse_curation_reason('not json') == ''
    assert parse_curation_reason('[]') == ''


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

    result = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=duplicate.id))

    existing.refresh_from_db()
    assert result.decision == 'superseded'
    assert result.superseded_memory is not None
    assert result.superseded_memory.id == existing.id
    assert existing.stale is True


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
