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
    _distill_chunk_char_budget,
    _observation_block,
    _parse_reduced_candidates,
    chunk_observations,
    parse_synthesized_candidates,
    run_session_distillation_with_tracking,
    session_distillation_prompt,
    session_distillation_system_prompt,
    session_reduce_system_prompt,
)
from engram.memory.services import MemoryWorkerError, PromoteMemoryCandidate
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.real_provider_tests import _opener_returning, make_real_policy
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    AnthropicMessagesGateway,
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


def candidates_body(items: list[dict[str, Any]] | dict[str, Any]) -> bytes:
    return json.dumps({'choices': [{'message': {'content': json.dumps(items)}}]}).encode()


def anthropic_tool_body(memories: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {'content': [{'type': 'tool_use', 'name': 'emit_memories', 'input': {'memories': memories}}]},
    ).encode()


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


def create_policy_with_model(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    model: str,
    task_type: str = 'curation',
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'{task_type} policy secret for {model}',
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
        name=f'{task_type} policy for {model}',
        scope='project',
        task_type=task_type,
        provider='openai',
        model=model,
        secret=secret,
        version=1,
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


def test_session_distillation_system_prompt_requests_json_object_and_is_runtime_neutral() -> None:
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

    prompt = session_distillation_prompt([observation], cap=100_000)

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


def test_parse_synthesized_candidates_reads_memories_object() -> None:
    raw = json.dumps(
        {
            'memories': [
                {
                    'title': 'Retry queue drops messages on Redis restart',
                    'body': 'Consumer acks before processing in worker/queue.py.',
                    'confidence': 0.9,
                    'supporting_observation_ids': ['obs-1'],
                },
            ],
        },
    )

    candidates = parse_synthesized_candidates(raw)

    assert len(candidates) == 1
    assert candidates[0].title == 'Retry queue drops messages on Redis restart'
    assert candidates[0].confidence == Decimal('0.900')
    assert candidates[0].supporting_observation_ids == ('obs-1',)


def test_parse_synthesized_candidates_empty_memories_means_no_candidates() -> None:
    assert parse_synthesized_candidates('{"memories": []}') == ()
    assert parse_synthesized_candidates('[]') == ()


def test_parse_synthesized_candidates_object_without_memories_falls_back() -> None:
    candidates = parse_synthesized_candidates('{"other": 1}')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.500')


def test_parse_synthesized_candidates_strips_json_fence_instead_of_falling_back() -> None:
    fenced = '```json\n{"memories": [{"title": "T", "body": "B", "confidence": 0.9}]}\n```'

    candidates = parse_synthesized_candidates(fenced)

    assert len(candidates) == 1
    assert candidates[0].title == 'T'
    assert candidates[0].body == 'B'
    assert candidates[0].confidence == Decimal('0.900')
    assert not candidates[0].title.startswith('```')


def test_parse_synthesized_candidates_unfenced_json_still_parses() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]})

    candidates = parse_synthesized_candidates(raw)

    assert len(candidates) == 1
    assert candidates[0].title == 'T'


def test_parse_synthesized_candidates_genuinely_invalid_still_falls_back() -> None:
    candidates = parse_synthesized_candidates('this is not json and not fenced either')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.500')
    assert candidates[0].body == 'this is not json and not fenced either'


def test_session_distillation_system_prompt_declares_memories_object_contract() -> None:
    prompt = session_distillation_system_prompt()

    assert '"memories"' in prompt
    assert '{"memories": []}' in prompt
    assert '0.9' in prompt


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
def test_distill_session_zero_synthesized_candidates_returns_empty_result(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    m_monkeypatch.setattr(
        'engram.model_policy.services.generated_candidates_payload',
        lambda _prompt: json.dumps({'memories': []}),
    )

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert result.auto_promoted == ()
    assert result.queued_for_review == ()
    assert MemoryCandidate.objects.filter(project=project).count() == 0
    assert ProviderCallRecord.objects.filter(task_type='curation').count() == 1


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
    # Budget is also the per-observation truncation cap; must stay large enough to keep the
    # 'Observation: <uuid>' head line intact while still forcing one observation per chunk.
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '100')
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


def test_session_reduce_system_prompt_requests_memories_object_and_is_runtime_neutral() -> None:
    prompt = session_reduce_system_prompt()

    assert 'JSON' in prompt
    assert '"memories"' in prompt
    assert 'source_ids' in prompt
    assert 'confidence' in prompt
    for brand in ('Claude', 'Codex', 'claude-mem', 'OpenAI', 'GPT', 'Anthropic'):
        assert brand not in prompt


def test_distill_chunk_char_budget_env_override_returns_verbatim(m_monkeypatch: pytest.MonkeyPatch) -> None:
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '777')
    policy = ModelPolicy(model='claude-3-opus', metadata={'context_window_tokens': 999999})

    assert _distill_chunk_char_budget(policy) == 777


def test_distill_chunk_char_budget_unknown_model_defaults_to_40000_capped_by_ceiling(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60')
    policy = ModelPolicy(model='mystery-model-9000', metadata={})

    assert _distill_chunk_char_budget(policy) == 40000


def test_distill_chunk_char_budget_known_model_is_min_of_context_chars_and_ceiling(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60')
    policy = ModelPolicy(model='claude-3-opus', metadata={})

    assert _distill_chunk_char_budget(policy) == 120000


def test_distill_chunk_char_budget_uses_uncapped_context_chars_between_floor_and_ceiling(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60')
    policy = ModelPolicy(model='mystery-model', metadata={'context_window_tokens': 12000})

    assert _distill_chunk_char_budget(policy) == 12000


def test_distill_chunk_char_budget_floor_clamps_small_context_window(m_monkeypatch: pytest.MonkeyPatch) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60')
    policy = ModelPolicy(model='mystery-model', metadata={'context_window_tokens': 8000})

    assert _distill_chunk_char_budget(policy) == 8000


def test_distill_chunk_char_budget_never_exceeds_ceiling_when_ceiling_below_floor(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '1')
    policy = ModelPolicy(model='mystery-model', metadata={'context_window_tokens': 8000})

    assert _distill_chunk_char_budget(policy) == 2000


@pytest.mark.django_db
def test_distill_session_known_model_yields_fewer_chunks_than_unknown_and_drops_truncation_audit(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60')
    organization, team, project, agent, session = create_session_scope()
    observations = [
        create_observation(organization, project, team, agent, session, index=i, body='x' * 1500) for i in range(1, 51)
    ]
    unknown_policy = ModelPolicy(model='mystery-model-9000', metadata={})
    known_policy = ModelPolicy(model='claude-3-opus', metadata={})
    unknown_chunks = chunk_observations(observations, _distill_chunk_char_budget(unknown_policy))
    known_chunks = chunk_observations(observations, _distill_chunk_char_budget(known_policy))
    assert len(known_chunks) < len(unknown_chunks)
    assert len(unknown_chunks) >= 2
    max_chunks = len(unknown_chunks) - 1
    m_monkeypatch.setenv('ENGRAM_DISTILL_MAX_CHUNKS', str(max_chunks))

    unknown_organization, unknown_team, unknown_project, unknown_agent, unknown_session = create_session_scope(
        suffix='unknown',
    )
    create_policy_with_model(unknown_organization, unknown_team, unknown_project, model='mystery-model-9000')
    for index in range(1, 51):
        create_observation(
            unknown_organization,
            unknown_project,
            unknown_team,
            unknown_agent,
            unknown_session,
            index=index,
            body='x' * 1500,
        )

    known_organization, known_team, known_project, known_agent, known_session = create_session_scope(suffix='known')
    create_policy_with_model(known_organization, known_team, known_project, model='claude-3-opus')
    for index in range(1, 51):
        create_observation(
            known_organization,
            known_project,
            known_team,
            known_agent,
            known_session,
            index=index,
            body='x' * 1500,
        )

    unknown_result = run_session_distillation_with_tracking(session_id=unknown_session.id, request_id='unknown-1')
    known_result = run_session_distillation_with_tracking(session_id=known_session.id, request_id='known-1')

    assert len(unknown_result.provider_call_ids) == max_chunks
    assert len(known_result.provider_call_ids) == len(known_chunks)
    assert len(known_result.provider_call_ids) < len(unknown_result.provider_call_ids)
    assert AuditEvent.objects.filter(
        event_type='SessionDistillationTruncated',
        target_id=str(unknown_session.id),
    ).exists()
    assert not AuditEvent.objects.filter(
        event_type='SessionDistillationTruncated',
        target_id=str(known_session.id),
    ).exists()


@pytest.mark.django_db
def test_observation_block_under_cap_is_unchanged() -> None:
    organization, team, project, agent, session = create_session_scope()
    observation = create_observation(organization, project, team, agent, session, index=1)

    block = _observation_block(observation, cap=1_000_000)

    assert block == '\n'.join(
        [
            f'Observation: {observation.id}',
            'Title: observation 1',
            'Body: body 1',
            "Facts: ['fact-1']",
            'Narrative: narrative 1',
            "Concepts: ['concept-1']",
            "Files read: ['apps/file_read_1.py']",
            "Files modified: ['apps/file_modified_1.py']",
        ],
    )


@pytest.mark.django_db
def test_observation_block_truncates_trailing_fields_and_keeps_head_content_and_marker() -> None:
    organization, team, project, agent, session = create_session_scope()
    observation = create_observation(
        organization,
        project,
        team,
        agent,
        session,
        index=1,
        body='body content that must survive truncation',
        facts=['fact that must survive truncation'],
        files_read=['apps/' + 'r' * 200 + '.py'],
        files_modified=['apps/' + 'm' * 200 + '.py'],
    )
    full_block = _observation_block(observation, cap=1_000_000)
    cap = full_block.index('Files read:') - 1

    truncated_block = _observation_block(observation, cap)

    assert len(truncated_block) == cap
    assert truncated_block.endswith('chars]')
    assert f'[truncated {len(full_block) - cap} chars]' in truncated_block
    assert 'body content that must survive truncation' in truncated_block
    assert 'fact that must survive truncation' in truncated_block
    assert 'r' * 200 not in truncated_block
    assert 'm' * 200 not in truncated_block


@pytest.mark.django_db
def test_chunk_observations_packing_length_matches_session_prompt_length_when_truncating() -> None:
    organization, team, project, agent, session = create_session_scope()
    observations = [
        create_observation(
            organization,
            project,
            team,
            agent,
            session,
            index=i,
            files_read=[f'apps/{"y" * 500}_{i}.py'],
        )
        for i in range(1, 4)
    ]
    cap = 200

    chunks = chunk_observations(observations, cap)

    for chunk in chunks:
        rendered = session_distillation_prompt(chunk, cap)
        expected_length = sum(len(_observation_block(observation, cap)) for observation in chunk)
        expected_length += 2 * (len(chunk) - 1)
        assert len(rendered) == expected_length


def test_parse_reduced_candidates_returns_none_on_invalid_json() -> None:
    assert _parse_reduced_candidates('not json') is None


def test_parse_reduced_candidates_returns_none_when_not_a_dict() -> None:
    assert _parse_reduced_candidates(json.dumps([{'title': 'x', 'source_ids': [0]}])) is None


def test_parse_reduced_candidates_returns_none_when_memories_key_missing_or_not_a_list() -> None:
    assert _parse_reduced_candidates(json.dumps({'title': 'x'})) is None
    assert _parse_reduced_candidates(json.dumps({'memories': 'nope'})) is None


def test_parse_reduced_candidates_returns_none_on_missing_or_wrong_typed_fields() -> None:
    assert _parse_reduced_candidates(json.dumps({'memories': [{'title': 'x', 'source_ids': [0]}]})) is None
    assert (
        _parse_reduced_candidates(
            json.dumps({'memories': [{'title': 'x', 'body': 'y', 'confidence': 0.5, 'source_ids': 'nope'}]}),
        )
        is None
    )
    assert (
        _parse_reduced_candidates(
            json.dumps({'memories': [{'title': 'x', 'body': 'y', 'confidence': 0.5, 'source_ids': [True]}]}),
        )
        is None
    )
    assert _parse_reduced_candidates(json.dumps({'memories': ['not a dict']})) is None


def test_parse_reduced_candidates_parses_valid_memories_object() -> None:
    raw = json.dumps(
        {'memories': [{'title': 'merged', 'body': 'merged body', 'confidence': 0.75, 'source_ids': [0, 2]}]},
    )

    parsed = _parse_reduced_candidates(raw)

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0].title == 'merged'
    assert parsed[0].confidence == Decimal('0.750')
    assert parsed[0].source_ids == (0, 2)


def test_parse_reduced_candidates_strips_json_fence() -> None:
    raw = json.dumps(
        {'memories': [{'title': 'merged', 'body': 'merged body', 'confidence': 0.75, 'source_ids': [0, 2]}]},
    )
    fenced = f'```json\n{raw}\n```'

    parsed = _parse_reduced_candidates(fenced)

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0].title == 'merged'


def test_parse_reduced_candidates_unfenced_json_still_parses() -> None:
    raw = json.dumps(
        {'memories': [{'title': 'merged', 'body': 'merged body', 'confidence': 0.75, 'source_ids': [0, 2]}]},
    )

    parsed = _parse_reduced_candidates(raw)

    assert parsed is not None
    assert len(parsed) == 1


def test_parse_reduced_candidates_truly_invalid_still_returns_none() -> None:
    assert _parse_reduced_candidates('this is not json and not fenced either') is None


def _reduce_scope(
    m_monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: str = '1',
    observation_count: int = 40,
) -> tuple[Organization, Team, Project, Agent, AgentSession, ModelPolicy]:
    organization, team, project, agent, session = create_session_scope()
    policy = create_policy_with_model(organization, team, project, model='mystery-reduce-model')
    for index in range(1, observation_count + 1):
        create_observation(organization, project, team, agent, session, index=index)
    m_monkeypatch.delenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', raising=False)
    m_monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', timeout)

    return organization, team, project, agent, session, policy


def _draft_chunk_body(index: int, *, title_len: int = 4, body_len: int = 4) -> bytes:
    return candidates_body(
        [
            {
                'title': f'draft{index}{"t" * title_len}',
                'body': f'draftbody{index}{"b" * body_len}',
                'confidence': 0.5,
                'supporting_observation_ids': [],
            },
        ],
    )


@pytest.mark.django_db
def test_distill_session_reduce_does_not_fire_for_single_chunk_even_over_target(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '0')
    body = candidates_body(
        [
            {'title': 'one', 'body': 'body one', 'confidence': 0.9, 'supporting_observation_ids': []},
            {'title': 'two', 'body': 'body two', 'confidence': 0.4, 'supporting_observation_ids': []},
        ],
    )
    opener = sequenced_opener([body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == 1
    assert AuditEvent.objects.filter(event_type='SessionDistillationReduceSkipped').count() == 0
    assert MemoryCandidate.objects.filter(project=project).count() == 2


@pytest.mark.django_db
def test_distill_session_reduce_does_not_fire_when_count_at_or_under_target(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', str(len(chunks)))
    bodies = [_draft_chunk_body(index) for index in range(len(chunks))]
    opener = sequenced_opener(bodies)
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == len(chunks)
    assert AuditEvent.objects.filter(event_type='SessionDistillationReduceSkipped').count() == 0


@pytest.mark.django_db
def test_distill_session_reduce_maps_source_ids_to_observation_union_ignoring_invalid_ids(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 3
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [
        candidates_body(
            [
                {
                    'title': f'draft{index}',
                    'body': f'draft body {index}',
                    'confidence': 0.5,
                    'supporting_observation_ids': [str(chunks[index][0].id)],
                },
            ],
        )
        for index in range(len(chunks))
    ]
    reduce_items = [
        {'title': 'merged one', 'body': 'merged body one', 'confidence': 0.8, 'source_ids': [0, 1]},
        {'title': 'merged two', 'body': 'merged body two', 'confidence': 0.6, 'source_ids': [2, 99999]},
    ]
    opener = sequenced_opener([*chunk_bodies, candidates_body({'memories': reduce_items})])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id, correlation_id='reduce-map-1'))

    assert len(opener.calls) == len(chunks) + 1
    reduce_record = ProviderCallRecord.objects.get(
        request_id=f'distill-session:{session.id}:reduce-map-1:curation:reduce',
    )
    chunk_records = ProviderCallRecord.objects.filter(task_type='curation').exclude(id=reduce_record.id)
    assert chunk_records.count() == len(chunks)
    assert str(reduce_record.id) in result.provider_call_ids

    candidate_one = MemoryCandidate.objects.get(title='merged one')
    candidate_two = MemoryCandidate.objects.get(title='merged two')
    assert candidate_one.evidence[0]['supporting_observation_ids'] == [
        str(chunks[0][0].id),
        str(chunks[1][0].id),
    ]
    assert candidate_two.evidence[0]['supporting_observation_ids'] == [str(chunks[2][0].id)]
    assert candidate_one.evidence[0]['reduced'] is True
    assert candidate_two.evidence[0]['reduced'] is True
    assert MemoryCandidate.objects.filter(project=project).count() == 2


@pytest.mark.django_db
def test_distill_session_reduce_skips_over_budget_and_keeps_union(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [
        candidates_body(
            [
                {
                    'title': f'oversized draft {index}',
                    'body': ('z' * (budget // 2)) + str(index),
                    'confidence': 0.5,
                    'supporting_observation_ids': [],
                },
            ],
        )
        for index in range(len(chunks))
    ]
    opener = sequenced_opener(chunk_bodies)
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == len(chunks)
    audit = AuditEvent.objects.get(event_type='SessionDistillationReduceSkipped')
    assert audit.metadata['reason'] == 'over_budget'
    assert MemoryCandidate.objects.filter(project=project).count() == len(chunks)


@pytest.mark.django_db
def test_distill_session_reduce_falls_back_to_union_on_provider_error_and_audits(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [_draft_chunk_body(index) for index in range(len(chunks))]
    calls: list[Any] = []

    def opener(request: Any, timeout: float = 30) -> _RecordingResponse:
        calls.append(request)
        if len(calls) > len(chunk_bodies):
            raise urllib.error.URLError('reduce boom')

        return _RecordingResponse(chunk_bodies[len(calls) - 1])

    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(calls) == len(chunk_bodies) + 1
    audit = AuditEvent.objects.get(event_type='SessionDistillationReduceSkipped')
    assert audit.metadata['reason'] == 'provider_error'
    assert MemoryCandidate.objects.filter(project=project).count() == len(chunks)
    assert len(result.provider_call_ids) == len(chunks)


@pytest.mark.django_db
def test_distill_session_reduce_falls_back_to_union_on_strict_parse_failure_and_audits(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [_draft_chunk_body(index) for index in range(len(chunks))]
    garbage_reduce_body = candidates_body({'memories': [{'title': 'no source ids here', 'confidence': 0.5}]})
    opener = sequenced_opener([*chunk_bodies, garbage_reduce_body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == len(chunk_bodies) + 1
    audit = AuditEvent.objects.get(event_type='SessionDistillationReduceSkipped')
    assert audit.metadata['reason'] == 'parse_failed'
    assert MemoryCandidate.objects.filter(project=project).count() == len(chunks)
    assert len(result.provider_call_ids) == len(chunks)


@pytest.mark.django_db
def test_distill_session_reduce_final_count_matches_llm_provided_target(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch, observation_count=60)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 4
    target = len(chunks) - 1
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', str(target))
    chunk_bodies = [_draft_chunk_body(index) for index in range(len(chunks))]
    reduce_items = [
        {'title': f'reduced {index}', 'body': f'reduced body {index}', 'confidence': 0.7, 'source_ids': [index]}
        for index in range(target)
    ]
    reduce_items[-1]['source_ids'] = list(range(target - 1, len(chunks)))
    opener = sequenced_opener([*chunk_bodies, candidates_body({'memories': reduce_items})])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert MemoryCandidate.objects.filter(project=project).count() == target
    assert target <= len(chunks) - 1


@pytest.mark.django_db
def test_distill_session_reduce_empty_memories_keeps_union_and_audits_empty(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [_draft_chunk_body(index) for index in range(len(chunks))]
    empty_reduce_body = candidates_body({'memories': []})
    opener = sequenced_opener([*chunk_bodies, empty_reduce_body])
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    result = DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert len(opener.calls) == len(chunks) + 1
    audit = AuditEvent.objects.get(event_type='SessionDistillationReduceSkipped')
    assert audit.metadata['reason'] == 'empty'
    assert MemoryCandidate.objects.filter(project=project).count() == len(chunks)
    assert len(result.provider_call_ids) == len(chunks)


@pytest.mark.django_db
def test_distill_session_reduce_succeeds_via_anthropic_tool_use_and_maps_source_ids(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    chunk_bodies = [
        anthropic_tool_body(
            [
                {
                    'title': f'draft{index}',
                    'body': f'draft body {index}',
                    'confidence': 0.5,
                    'supporting_observation_ids': [str(chunks[index][0].id)],
                },
            ],
        )
        for index in range(len(chunks))
    ]
    reduce_body = anthropic_tool_body(
        [
            {
                'title': 'merged via anthropic',
                'body': 'merged body via anthropic',
                'confidence': 0.8,
                'source_ids': list(range(len(chunks))),
            },
        ],
    )
    opener = sequenced_opener([*chunk_bodies, reduce_body])
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)
    m_monkeypatch.setattr('engram.memory.distillation.get_provider_gateway', lambda *_args, **_kwargs: gateway)

    DistillSession().execute(DistillSessionInput(session_id=session.id, correlation_id='anthropic-reduce-1'))

    assert len(opener.calls) == len(chunks) + 1
    assert AuditEvent.objects.filter(event_type='SessionDistillationReduceSkipped').count() == 0
    candidate = MemoryCandidate.objects.get(project=project)
    assert candidate.title == 'merged via anthropic'
    assert candidate.evidence[0]['reduced'] is True
    assert sorted(candidate.evidence[0]['supporting_observation_ids']) == sorted(
        str(chunks[index][0].id) for index in range(len(chunks))
    )


@pytest.mark.django_db
def test_distill_session_reduce_parses_successfully_under_fake_provider(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session, policy = _reduce_scope(m_monkeypatch)
    observations = list(Observation.objects.filter(project=project).order_by('prompt_number'))
    budget = _distill_chunk_char_budget(policy)
    chunks = chunk_observations(observations, budget)
    assert len(chunks) >= 2
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')

    DistillSession().execute(DistillSessionInput(session_id=session.id))

    assert AuditEvent.objects.filter(event_type='SessionDistillationReduceSkipped').count() == 0
    candidates = MemoryCandidate.objects.filter(project=project)
    assert candidates.count() == 2
    assert all(candidate.evidence[0].get('reduced') is True for candidate in candidates)
