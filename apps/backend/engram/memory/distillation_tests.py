from __future__ import annotations

import json
import threading
import urllib.error
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    Observation,
    Organization,
    Project,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    Team,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.distillation import (
    DistillSession,
    DistillSessionInput,
    chunk_observations,
    parse_synthesized_candidates,
    run_session_distillation_with_tracking,
    session_distillation_prompt,
    session_distillation_system_prompt,
)
from engram.memory.services import MemoryWorkerError, PromoteMemoryCandidate
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.real_provider_tests import _opener_returning, make_real_policy
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    FakeProviderGateway,
    OpenAICompatibleGateway,
    _completion_body,
)


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


class _RecordingResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _RecordingResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def sequenced_opener(bodies: list[bytes]) -> Any:
    calls: list[Any] = []

    def opener(request: Any, timeout: float = 30) -> _RecordingResponse:
        calls.append(request)
        body = bodies[len(calls) - 1] if len(calls) <= len(bodies) else bodies[-1]

        return _RecordingResponse(body)

    opener.calls = calls  # type: ignore[attr-defined]

    return opener


def candidates_body(items: list[dict[str, Any]]) -> bytes:
    return json.dumps({'choices': [{'message': {'content': json.dumps(items)}}]}).encode()


def create_session_scope(*, suffix: str = '1') -> tuple[Organization, Team, Project, Agent, AgentSession]:
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
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'codex-local-{suffix}',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        status=SessionStatus.ENDED,
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
        branch='master',
        cwd='/workspace/engram',
        ended_at=timezone.now(),
    )

    return organization, team, project, agent, session


def create_observation(
    organization: Organization,
    project: Project,
    team: Team,
    agent: Agent,
    session: AgentSession,
    *,
    index: int,
    **overrides: Any,
) -> Observation:
    defaults: dict[str, Any] = {
        'observation_type': 'tool_use',
        'title': f'observation {index}',
        'body': f'body {index}',
        'facts': [f'fact-{index}'],
        'narrative': f'narrative {index}',
        'concepts': [f'concept-{index}'],
        'files_read': [f'apps/file_read_{index}.py'],
        'files_modified': [f'apps/file_modified_{index}.py'],
        'prompt_number': index,
        'content_hash': f'hash-obs-{session.external_session_id}-{index}',
        'source_metadata': {'event_type': 'post_tool_use'},
        'observed_at': timezone.now(),
    }
    defaults.update(overrides)

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        **defaults,
    )


def create_curation_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team OpenAI',
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
        ciphertext='encrypted-secret',
        hmac_digest='secret-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Curation policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=2,
    )


def create_generation_policy(organization: Organization, team: Team, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='Team Generation OpenAI',
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
        ciphertext='encrypted-generation-secret',
        hmac_digest='generation-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


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


def test_session_distillation_system_prompt_requests_json_array_and_is_runtime_neutral() -> None:
    prompt = session_distillation_system_prompt()

    assert 'JSON' in prompt
    assert 'confidence' in prompt
    assert 'supporting_observation_ids' in prompt
    for brand in ('Claude', 'Codex', 'claude-mem', 'OpenAI', 'GPT', 'Anthropic'):
        assert brand not in prompt


@pytest.mark.django_db
def test_session_distillation_prompt_includes_facts_narrative_and_concepts() -> None:
    organization, team, project, agent, session = create_session_scope()
    observation = create_observation(
        organization,
        project,
        team,
        agent,
        session,
        index=1,
        facts=['migration 0042 added'],
        narrative='ran pytest and it now exits 0',
        concepts=['database-migrations'],
        files_read=['apps/backend/engram/core/models.py'],
    )

    prompt = session_distillation_prompt([observation])

    assert 'migration 0042 added' in prompt
    assert 'ran pytest and it now exits 0' in prompt
    assert 'database-migrations' in prompt
    assert 'apps/backend/engram/core/models.py' in prompt
    assert str(observation.id) in prompt


def test_parse_synthesized_candidates_falls_back_on_invalid_json() -> None:
    candidates = parse_synthesized_candidates('not json at all')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.500')
    assert candidates[0].body == 'not json at all'


def test_parse_synthesized_candidates_reads_full_real_gateway_output() -> None:
    pretty_json = json.dumps(
        [
            {'title': 'migration', 'body': 'added 0042', 'confidence': 0.91, 'supporting_observation_ids': ['a']},
            {'title': 'flaky test', 'body': 'retry pytest', 'confidence': 0.42, 'supporting_observation_ids': ['b']},
        ],
        indent=2,
    )

    candidates = parse_synthesized_candidates(_completion_body(pretty_json, 'candidates'))

    assert len(candidates) == 2
    assert candidates[0].title == 'migration'
    assert candidates[0].confidence == Decimal('0.910')
    assert candidates[1].confidence == Decimal('0.420')


def test_parse_synthesized_candidates_clamps_confidence_to_unit_interval() -> None:
    raw = json.dumps(
        [
            {'title': 'high', 'body': 'b1', 'confidence': 1.5, 'supporting_observation_ids': []},
            {'title': 'low', 'body': 'b2', 'confidence': -3, 'supporting_observation_ids': []},
        ],
    )

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].confidence == Decimal('1.000')
    assert candidates[1].confidence == Decimal('0.000')


@pytest.mark.django_db
def test_distill_session_auto_promotes_high_confidence_and_holds_low_confidence() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)

    result = DistillSession().execute(
        DistillSessionInput(
            session_id=session.id,
            request_id='distill-1',
            auto_approve_threshold=Decimal('0.800'),
        ),
    )

    assert len(result.auto_promoted) == 1
    assert len(result.queued_for_review) == 1
    memory = result.auto_promoted[0]
    held = result.queued_for_review[0]

    assert memory.status == MemoryStatus.APPROVED
    assert memory.confidence == Decimal('0.900')
    assert held.status == CandidateStatus.PROPOSED
    assert held.confidence == Decimal('0.400')

    candidates = MemoryCandidate.objects.filter(project=project)
    assert candidates.count() == 2
    assert len(set(candidates.values_list('content_hash', flat=True))) == 2

    assert RetrievalDocument.objects.filter(memory=memory).exists()

    audit = AuditEvent.objects.get(event_type='MemoryCandidateHeldForReview')
    assert audit.actor_type == 'system'
    assert audit.target_id == str(held.id)
    assert audit.metadata['confidence'] == '0.400'
    assert audit.metadata['threshold'] == '0.800'


@pytest.mark.django_db
def test_distill_session_promotes_clean_candidate_through_curator_with_embeddings() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(result.auto_promoted) == 1
    memory = result.auto_promoted[0]
    assert memory.stale is False
    document = RetrievalDocument.objects.get(memory=memory)
    assert len(document.embedding_vector) == EMBEDDING_DIMENSION
    assert MemoryLink.objects.filter(link_type=LinkType.SUPERSEDED_BY).count() == 0
    assert AuditEvent.objects.filter(event_type='MemoryAutoRejected').count() == 0


@pytest.mark.django_db
def test_distill_session_falls_back_to_generation_policy_when_no_curation_policy() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_generation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(result.auto_promoted) + len(result.queued_for_review) == 2
    provider_call = ProviderCallRecord.objects.get()
    assert provider_call.task_type == 'generation'


@pytest.mark.django_db
def test_distill_session_makes_provider_call_outside_write_transaction(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)

    def m_raise(self: PromoteMemoryCandidate, data: object) -> object:
        raise RuntimeError('write phase boom')

    m_monkeypatch.setattr(PromoteMemoryCandidate, 'execute', m_raise)

    with pytest.raises(RuntimeError, match='write phase boom'):
        DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert ProviderCallRecord.objects.filter(task_type='curation').count() == 1
    # Candidate creation is a cheap DB-only write committed under the session lock; it is no
    # longer rolled back by a downstream promotion failure now that the curator's provider calls
    # (embed/judge) run after that lock is released. No Memory is created since promotion failed.
    assert MemoryCandidate.objects.filter(project=project).count() == 2
    assert Memory.objects.count() == 0


@pytest.mark.django_db
def test_distill_session_empty_session_is_noop() -> None:
    organization, team, project, _agent, session = create_session_scope()
    create_curation_policy(organization, team, project)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert result.auto_promoted == ()
    assert result.queued_for_review == ()
    assert MemoryCandidate.objects.count() == 0
    assert Memory.objects.count() == 0
    assert ProviderCallRecord.objects.count() == 0
    assert AuditEvent.objects.filter(event_type='MemoryCandidateHeldForReview').count() == 0


@pytest.mark.django_db
def test_distill_session_is_idempotent_on_rerun() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)

    first = DistillSession().execute(DistillSessionInput(session_id=session.id))
    second = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert MemoryCandidate.objects.filter(project=project).count() == 2
    assert Memory.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1
    assert AuditEvent.objects.filter(event_type='MemoryCandidateHeldForReview').count() == 1
    # Reruns without an explicit run_id/correlation_id/request_id each get a fresh per-chunk request_id
    # (uuid4 fallback), so they are no longer deduped at the provider-call level; idempotency is enforced
    # by content_hash on MemoryCandidate instead.
    assert ProviderCallRecord.objects.filter(task_type='curation').count() == 2
    assert len(second.auto_promoted) == 1
    assert len(second.queued_for_review) == 1
    assert second.auto_promoted[0].id == first.auto_promoted[0].id
    assert second.queued_for_review[0].id == first.queued_for_review[0].id


@pytest.mark.django_db
def test_run_session_distillation_with_tracking_records_succeeded_run() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)

    result = run_session_distillation_with_tracking(session_id=session.id, request_id='track-distill-1')

    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    provider_call = ProviderCallRecord.objects.get(task_type='curation')

    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.input_snapshot == {'session_id': str(session.id)}
    assert run.provider_call_ids == [str(provider_call.id)]
    assert run.result_memory_id == result.auto_promoted[0].id


@pytest.mark.django_db
def test_run_session_distillation_with_tracking_marks_failed_run_and_reraises() -> None:
    organization, _team, _project, agent, session = create_session_scope()
    create_observation(organization, session.project, session.team, agent, session, index=1)

    with pytest.raises(Exception, match='Model policy was not found'):
        run_session_distillation_with_tracking(session_id=session.id, request_id='track-distill-fail')

    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.finished_at is not None


@pytest.mark.django_db
def test_distill_session_rerun_with_real_gateway_does_not_replay_stale_provider_call(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    make_real_policy(
        organization,
        project,
        task_type='curation',
        base_url='https://provider.example/v1',
        raw_key='real-key',
    )
    create_observation(organization, project, team, agent, session, index=1)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')
    candidates_payload = json.dumps(
        [
            {
                'title': 'redis timeout',
                'body': 'bumped redis timeout to 30s',
                'confidence': 0.9,
                'supporting_observation_ids': [],
            },
        ],
    )
    completion = {'choices': [{'message': {'content': candidates_payload}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    m_monkeypatch.setattr('urllib.request.urlopen', opener)

    first = run_session_distillation_with_tracking(
        session_id=session.id,
        request_id='rerun-1',
        auto_approve_threshold=Decimal('0.500'),
    )
    second = run_session_distillation_with_tracking(
        session_id=session.id,
        request_id='rerun-2',
        auto_approve_threshold=Decimal('0.500'),
    )

    assert len(opener.requests) == 2
    assert second.provider_call_ids[0] != first.provider_call_ids[0]
    assert len(first.auto_promoted) == 1
    assert len(second.auto_promoted) == 1
    assert second.auto_promoted[0].id == first.auto_promoted[0].id
    assert Memory.objects.count() == 1
    memory = Memory.objects.get()
    assert not memory.title.startswith('Observation:')
    assert memory.body == 'bumped redis timeout to 30s'


@pytest.mark.django_db(transaction=True)
def test_distill_session_curate_embed_call_has_no_open_transaction(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_embedding_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    observed_in_atomic: list[bool] = []
    real_gateway = FakeProviderGateway()

    class _RecordingGateway(FakeProviderGateway):
        def embed(self, data: object) -> object:
            observed_in_atomic.append(connection.in_atomic_block)

            return real_gateway.embed(data)

    m_monkeypatch.setattr('engram.memory.curation.get_provider_gateway', lambda *_, **__: _RecordingGateway())

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert observed_in_atomic == [False]


@pytest.mark.django_db(transaction=True)
def test_distill_session_concurrent_execution_creates_exactly_one_memory() -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires real row locking on postgres')
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    session_id = session.id
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(DistillSession().execute(DistillSessionInput(session_id=session_id)))
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
    assert MemoryCandidate.objects.filter(project=project).count() == 2
    assert Memory.objects.count() == 1
    assert RetrievalDocument.objects.count() == 1


@pytest.mark.django_db
def test_chunk_observations_splits_oversized_blocks_into_separate_chunks() -> None:
    organization, team, project, agent, session = create_session_scope()
    observations = [create_observation(organization, project, team, agent, session, index=i) for i in range(1, 4)]

    chunks = chunk_observations(observations, budget=10)

    assert len(chunks) == 3
    assert [chunk[0].id for chunk in chunks] == [observation.id for observation in observations]


@pytest.mark.django_db
def test_chunk_observations_packs_small_blocks_into_one_chunk_under_budget() -> None:
    organization, team, project, agent, session = create_session_scope()
    observations = [create_observation(organization, project, team, agent, session, index=i) for i in range(1, 4)]

    chunks = chunk_observations(observations, budget=1_000_000)

    assert len(chunks) == 1
    assert [observation.id for observation in chunks[0]] == [observation.id for observation in observations]


@pytest.mark.django_db
def test_distill_session_batches_observations_into_multiple_provider_calls_with_distinct_request_ids(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    for index in range(1, 4):
        create_observation(organization, project, team, agent, session, index=index)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    m_monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')
    body = candidates_body(
        [{'title': 'candidate title', 'body': 'candidate body', 'confidence': 0.9, 'supporting_observation_ids': []}],
    )
    opener = sequenced_opener([body, body, body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id, correlation_id='batch-test-1'))

    assert len(opener.calls) == 3
    records = list(ProviderCallRecord.objects.filter(task_type='curation').order_by('created_at'))
    assert len(records) == 3
    request_ids = [record.request_id for record in records]
    assert len(set(request_ids)) == 3
    assert request_ids == [f'distill-session:{session.id}:batch-test-1:curation:chunk:{index}' for index in range(3)]
    assert result.provider_call_ids == tuple(str(record.id) for record in records)
    assert not any(
        candidate.title.startswith('Observation:') for candidate in MemoryCandidate.objects.filter(project=project)
    )


@pytest.mark.django_db
def test_distill_session_chunk_prompts_cover_every_observation_exactly_once_in_order(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    observations = [create_observation(organization, project, team, agent, session, index=i) for i in range(1, 5)]
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body = candidates_body([{'title': 't', 'body': 'b', 'confidence': 0.9, 'supporting_observation_ids': []}])
    opener = sequenced_opener([body] * len(observations))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    prompts = [json.loads(request.data)['messages'][-1]['content'] for request in opener.calls]
    covered_ids = []
    for prompt in prompts:
        matches = [str(observation.id) for observation in observations if str(observation.id) in prompt]
        assert len(matches) == 1
        covered_ids.append(matches[0])

    assert covered_ids == [str(observation.id) for observation in observations]


@pytest.mark.django_db
def test_distill_session_small_session_makes_exactly_one_provider_call(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    body = candidates_body(
        [
            {'title': 'promoted', 'body': 'body a', 'confidence': 0.9, 'supporting_observation_ids': []},
            {'title': 'held', 'body': 'body b', 'confidence': 0.4, 'supporting_observation_ids': []},
        ],
    )
    opener = sequenced_opener([body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id, correlation_id='small-test-1'))

    assert len(opener.calls) == 1
    record = ProviderCallRecord.objects.get(task_type='curation')
    assert record.request_id == f'distill-session:{session.id}:small-test-1:curation:chunk:0'
    assert len(result.auto_promoted) == 1
    assert len(result.queued_for_review) == 1


@pytest.mark.django_db
def test_distill_session_dedups_identical_candidate_across_chunks(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body = candidates_body(
        [{'title': 'dup title', 'body': 'dup body', 'confidence': 0.9, 'supporting_observation_ids': []}],
    )
    opener = sequenced_opener([body, body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == 2
    assert MemoryCandidate.objects.filter(project=project).count() == 1


@pytest.mark.django_db
def test_distill_session_candidate_evidence_matches_its_own_chunk_provider_call(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body_zero = candidates_body(
        [{'title': 'chunk0 candidate', 'body': 'body0', 'confidence': 0.9, 'supporting_observation_ids': []}],
    )
    body_one = candidates_body(
        [{'title': 'chunk1 candidate', 'body': 'body1', 'confidence': 0.9, 'supporting_observation_ids': []}],
    )
    opener = sequenced_opener([body_zero, body_one])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id, correlation_id='prov-test-1'))

    record0 = ProviderCallRecord.objects.get(request_id=f'distill-session:{session.id}:prov-test-1:curation:chunk:0')
    record1 = ProviderCallRecord.objects.get(request_id=f'distill-session:{session.id}:prov-test-1:curation:chunk:1')
    candidate0 = MemoryCandidate.objects.get(title='chunk0 candidate')
    candidate1 = MemoryCandidate.objects.get(title='chunk1 candidate')

    assert candidate0.evidence[0]['provider_call_id'] == str(record0.id)
    assert candidate1.evidence[0]['provider_call_id'] == str(record1.id)


@pytest.mark.django_db
def test_distill_session_aborts_on_chunk_failure_without_entering_transaction(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body = candidates_body([{'title': 't', 'body': 'b', 'confidence': 0.9, 'supporting_observation_ids': []}])
    calls: list[Any] = []

    def opener(request: Any, timeout: float = 30) -> _RecordingResponse:
        calls.append(request)
        if len(calls) == 2:
            raise urllib.error.URLError('boom')

        return _RecordingResponse(body)

    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    with pytest.raises(MemoryWorkerError):
        DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(calls) == 2
    assert MemoryCandidate.objects.filter(project=project).count() == 0


@pytest.mark.django_db
def test_run_session_distillation_with_tracking_marks_failed_run_on_chunk_provider_error(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')

    def opener(request: Any, timeout: float = 30) -> _RecordingResponse:
        raise urllib.error.URLError('boom')

    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    with pytest.raises(MemoryWorkerError):
        run_session_distillation_with_tracking(session_id=session.id, request_id='fail-track-1')

    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    assert run.status == WorkflowRunStatus.FAILED


@pytest.mark.django_db
def test_run_session_distillation_with_tracking_records_all_chunk_provider_call_ids(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body = candidates_body([{'title': 'x', 'body': 'y', 'confidence': 0.4, 'supporting_observation_ids': []}])
    opener = sequenced_opener([body, body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    run_session_distillation_with_tracking(session_id=session.id, request_id='track-multi-1')

    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    records = ProviderCallRecord.objects.filter(task_type='curation').order_by('created_at')

    assert run.provider_call_ids == [str(record.id) for record in records]
    assert len(run.provider_call_ids) == 2


@pytest.mark.django_db
def test_distill_session_truncates_to_max_chunks_and_audits_when_chunk_count_exceeds_cap(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    for index in range(1, 11):
        create_observation(organization, project, team, agent, session, index=index)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '10')
    body = candidates_body([{'title': 't', 'body': 'b', 'confidence': 0.9, 'supporting_observation_ids': []}])
    opener = sequenced_opener([body] * 8)
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    run_session_distillation_with_tracking(session_id=session.id, request_id='cap-test-1')

    assert len(opener.calls) == 8
    audit = AuditEvent.objects.get(event_type='SessionDistillationTruncated')
    assert audit.actor_type == 'system'
    assert audit.target_type == 'agent_session'
    assert audit.target_id == str(session.id)
    assert audit.capability == 'memories:review'
    assert audit.metadata == {
        'chunks_total': 10,
        'chunks_processed': 8,
        'observation_count': 10,
        'observations_distilled': 8,
    }
    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    assert run.escalation is True


@pytest.mark.django_db
def test_distill_session_at_or_under_max_chunks_does_not_truncate_or_escalate() -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)

    run_session_distillation_with_tracking(session_id=session.id, request_id='no-truncation-1')

    assert AuditEvent.objects.filter(event_type='SessionDistillationTruncated').count() == 0
    run = WorkflowRun.objects.get(run_type=WorkflowRunType.SESSION_DISTILLATION)
    assert run.escalation is False


@pytest.mark.django_db
def test_distill_session_two_direct_invocations_without_run_id_yield_distinct_chunk_request_ids(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    body = candidates_body([{'title': 't', 'body': 'b', 'confidence': 0.9, 'supporting_observation_ids': []}])
    opener = sequenced_opener([body, body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))
    DistillSession().execute(DistillSessionInput(session_id=session.id))

    records = list(ProviderCallRecord.objects.filter(task_type='curation').order_by('created_at'))
    assert len(records) == 2
    assert records[0].request_id != records[1].request_id
