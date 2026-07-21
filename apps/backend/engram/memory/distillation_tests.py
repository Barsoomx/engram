from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.db import transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    DistillationObservationCoverage,
    DistillationStage,
    DistillationWindow,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryTransition,
    MemoryVersionSource,
    Observation,
    Organization,
    Project,
    Runtime,
    SessionStatus,
    Team,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.candidate_parsing import parse_synthesized_candidates
from engram.memory.distillation import (
    DistillationStageError,
    _accepted_stage_rows,
    _attach_promoted_candidate_source,
    finalize_distillation,
    run_complete_distillation_attempt,
    session_candidate_content_hash,
)
from engram.memory.distillation_provenance import (
    CandidatePlan,
    CandidateSourcePlan,
    CoveragePlan,
    FinalizationPlan,
    candidate_source_anchors,
    canonical_source_manifest,
)
from engram.memory.distillation_provenance import (
    session_candidate_content_hash as provenance_session_candidate_content_hash,
)
from engram.memory.distillation_provider_stage import (
    PROVIDER_OUTPUT_MALFORMED,
    PROVIDER_OUTPUT_TRUNCATED,
    stage_key,
    stage_target_key,
)
from engram.memory.distillation_window import materialize_distillation_window, render_observation_block
from engram.memory.services import MemoryWorkerError
from engram.memory.tasks import distill_session_work_v1
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import claim_work, finish_work_claim
from engram.memory.work_failures import (
    INFRASTRUCTURE_TRANSIENT,
    PROVIDER_TRANSIENT,
    UNEXPECTED,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    ProviderCallInput,
    ProviderCallResult,
    _completion_body,
)

_OWNER_RE = re.compile(r'^[^:]+:[0-9]+:[0-9a-f-]{36}$')
_RETRYING_CLASSES = frozenset({UNEXPECTED, INFRASTRUCTURE_TRANSIENT, PROVIDER_TRANSIENT})


def test_session_candidate_content_hash_has_one_neutral_canonical_definition() -> None:
    assert session_candidate_content_hash is provenance_session_candidate_content_hash
    assert (
        session_candidate_content_hash(uuid.UUID('00000000-0000-0000-0000-000000000001'), 'title', 'body')
        == '8e8076db4c858aeeac52fe26cac7ede4d11646e1bbedc9dc26f3ab69ad474423'
    )


@pytest.mark.django_db
def test_production_late_source_adapter_is_idempotent_without_reopening_decision_work() -> None:
    from engram.memory import transitions
    from engram.memory.transitions_test_support import provenanced_candidate, transition_request

    candidate, source, (organization, project, session) = provenanced_candidate('distill-late-source')
    promoted = transitions.PromoteMemoryCandidate().execute(transition_request(candidate))
    observation = create_observation(
        organization,
        project,
        session.team,
        session.agent,
        session,
        index=2,
        title='late source',
        body='late source body',
    )
    anchors = candidate_source_anchors(
        observation,
        observation_id=str(observation.id),
        session_sequence=observation.session_sequence,
        observation_digest=observation_content_digest(observation),
    )
    late_source = MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        candidate=candidate,
        window=source.window,
        observation=observation,
        stage=source.stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )

    candidate.refresh_from_db()
    attached = _attach_promoted_candidate_source(candidate, late_source, source.window)
    replay = _attach_promoted_candidate_source(candidate, late_source, source.window)

    assert attached is not None and attached.duplicate is False
    assert replay is not None and replay.duplicate is True
    assert MemoryTransition.objects.filter(candidate_id=candidate.id).count() == 2
    assert MemoryVersionSource.objects.filter(memory_version_id=promoted.memory_version.id).count() == 2
    decision_work = WorkflowWork.objects.filter(
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_id=candidate.id,
    )
    assert decision_work.count() == 2
    assert not decision_work.filter(disposition=WorkflowWorkDisposition.REQUIRED).exists()


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


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
        'session_sequence': index,
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


def create_curation_policy(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    fallback_enabled: bool = False,
) -> ModelPolicy:
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
        fallback_enabled=fallback_enabled,
    )


def test_parse_synthesized_candidates_falls_back_on_invalid_json() -> None:
    candidates = parse_synthesized_candidates('not json at all')

    assert len(candidates) == 1
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True
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
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True


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
    assert candidates[0].confidence == Decimal('0.000')
    assert candidates[0].parse_fallback is True
    assert candidates[0].body == 'this is not json and not fenced either'


def test_parse_synthesized_candidates_reads_kind() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'kind': 'gotcha'}]})

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].kind == 'gotcha'


def test_parse_synthesized_candidates_clamps_unknown_kind_to_empty_string() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'kind': 'random'}]})

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].kind == ''


def test_parse_synthesized_candidates_clamps_digest_kind_to_empty_string() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9, 'kind': 'digest'}]})

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].kind == ''


def test_parse_synthesized_candidates_missing_kind_defaults_to_empty_string() -> None:
    raw = json.dumps({'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]})

    candidates = parse_synthesized_candidates(raw)

    assert candidates[0].kind == ''


@pytest.mark.django_db
def test_observation_block_under_cap_is_unchanged() -> None:
    organization, team, project, agent, session = create_session_scope()
    observation = create_observation(organization, project, team, agent, session, index=1)

    block = render_observation_block(observation, cap=1_000_000)

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
    full_block = render_observation_block(observation, cap=1_000_000)
    cap = full_block.index('Files read:') - 1

    truncated_block = render_observation_block(observation, cap)

    assert len(truncated_block) == cap
    assert truncated_block.endswith('chars]')
    assert f'[truncated {len(full_block) - cap} chars]' in truncated_block
    assert 'body content that must survive truncation' in truncated_block
    assert 'fact that must survive truncation' in truncated_block
    assert 'r' * 200 not in truncated_block
    assert 'm' * 200 not in truncated_block


def create_session_distillation_work(session: AgentSession, *, upper: int) -> WorkflowWork:
    data = CreateWorkflowWorkInput(
        organization_id=session.organization_id,
        project_id=session.project_id,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        subject_type=WorkflowSubjectType.AGENT_SESSION,
        subject_id=session.id,
        input_snapshot={
            'schema': 'session_distillation_input/v1',
            'session_id': str(session.id),
            'lower_sequence_exclusive': 0,
            'upper_sequence_inclusive': upper,
        },
    )
    with transaction.atomic():
        work, created = create_work(data)

    assert created is True

    return work


@pytest.mark.django_db
def test_distill_session_work_v1_consumes_useful_prefix_and_resolves_work(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    first = create_observation(organization, project, team, agent, session, index=1)
    second = create_observation(organization, project, team, agent, session, index=2)
    lifecycle = create_observation(
        organization,
        project,
        team,
        agent,
        session,
        index=3,
        observation_type='session_lifecycle',
        source_metadata={'event_type': 'session_end'},
    )
    work = create_session_distillation_work(session, upper=2)
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    runs = WorkflowRun.objects.filter(work=work, run_type=WorkflowRunType.SESSION_DISTILLATION)
    assert runs.count() == 1
    run = runs.get()
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.started_at is not None
    assert run.finished_at is not None
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason in (
        WorkflowWorkResolutionReason.SUCCEEDED,
        WorkflowWorkResolutionReason.NO_SIGNAL,
    )
    assert len(gateway.calls) == 1
    prompt = gateway.calls[0].prompt
    assert str(first.id) in prompt
    assert str(second.id) in prompt
    assert str(lifecycle.id) not in prompt


@pytest.mark.django_db
def test_distill_session_work_v1_uses_complete_attempt_runner(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='complete-runner')
    create_observation(organization, project, team, agent, session, index=1)
    work = create_session_distillation_work(session, upper=1)
    called: list[uuid.UUID] = []

    def complete_attempt(*, work: WorkflowWork, claim: Any, now: Any) -> str:
        called.append(work.id)
        finish_work_claim(claim=claim, now=now, completion='product_no_signal')

        return 'completed'

    tasks_module = __import__('engram.memory.tasks', fromlist=['run_complete_distillation_attempt'])
    m_monkeypatch.setattr(tasks_module, 'run_complete_distillation_attempt', complete_attempt)

    distill_session_work_v1(str(work.id))

    assert called == [work.id]
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL


@pytest.mark.django_db
def test_distill_session_work_v1_ignores_useful_row_written_after_the_frozen_upper(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    first = create_observation(organization, project, team, agent, session, index=1)
    second = create_observation(organization, project, team, agent, session, index=2)
    work = create_session_distillation_work(session, upper=2)
    late = create_observation(organization, project, team, agent, session, index=3)
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    assert len(gateway.calls) == 1
    prompt = gateway.calls[0].prompt
    assert str(first.id) in prompt
    assert str(second.id) in prompt
    assert str(late.id) not in prompt
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE


@pytest.mark.django_db
def test_distill_session_work_v1_consumes_keyless_source_metadata_row_within_window(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    keyless = create_observation(organization, project, team, agent, session, index=1, source_metadata={})
    normal = create_observation(organization, project, team, agent, session, index=2)
    lifecycle = create_observation(
        organization,
        project,
        team,
        agent,
        session,
        index=3,
        observation_type='session_lifecycle',
        source_metadata={'event_type': 'session_end'},
    )
    work = create_session_distillation_work(session, upper=2)
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    assert len(gateway.calls) == 1
    prompt = gateway.calls[0].prompt
    assert str(keyless.id) in prompt
    assert str(normal.id) in prompt
    assert str(lifecycle.id) not in prompt


@pytest.mark.django_db
def test_distill_session_work_v1_duplicate_automatic_delivery_creates_one_run_and_no_second_provider_call(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    work = create_session_distillation_work(session, upper=2)
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))
    distill_session_work_v1(str(work.id))

    assert WorkflowRun.objects.filter(work=work).count() == 1
    assert len(gateway.calls) == 1
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE


# CONVERTED from test_distill_session_work_v1_uses_supplied_queued_run_without_creating_another.
# Old idiom: a bare v0 QUEUED run was adopted through _load_workflow_run + _claim_workflow_run (a
# status CAS to RUNNING) with no fencing token/owner. Under the cutover the supplied attempt is a v1
# queued run produced by queue_work_attempt; the adapter leases it through claim_work (token 1,
# process-owner), executes, then fences + finishes it in one short transaction. A v0 run can no longer
# satisfy the v1 running/succeeded contract, so the old CAS path cannot lease it.
@pytest.mark.django_db
def test_distill_session_work_v1_leases_supplied_queued_v1_run_without_creating_another(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    work = create_session_distillation_work(session, upper=2)
    queued = queue_work_attempt(
        work_id=work.id,
        now=timezone.now(),
        origin='reconciliation',
    )
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id), workflow_run_id=str(queued.id))

    assert WorkflowRun.objects.filter(work=work, execution_contract_version=1).count() == 1
    assert len(gateway.calls) == 1
    queued.refresh_from_db()
    assert queued.status == WorkflowRunStatus.SUCCEEDED
    assert queued.fencing_token == 1
    assert _OWNER_RE.match(queued.lease_owner) is not None
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED


@pytest.mark.django_db
def test_distill_session_work_v1_bounded_attempt_continues_with_new_queued_attempt(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    for index in range(1, 4):
        create_observation(
            organization,
            project,
            team,
            agent,
            session,
            index=index,
            body='x' * 4500,
            narrative='y' * 4500,
        )
    work = create_session_distillation_work(session, upper=3)
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '2')
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    assert len(gateway.calls) == 2
    assert not AuditEvent.objects.filter(
        event_type='SessionDistillationTruncated',
        target_id=str(session.id),
    ).exists()
    window = DistillationWindow.objects.get(work=work)
    assert window.chunks.count() == 3
    assert DistillationStage.objects.filter(window=window, status='complete').count() == 2
    assert not DistillationObservationCoverage.objects.filter(window=window).exists()

    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.execution_state == WorkflowWorkExecutionState.READY
    assert work.lease_owner == ''
    assert work.lease_expires_at is None

    executed = WorkflowRun.objects.get(
        work=work,
        execution_contract_version=1,
        status=WorkflowRunStatus.SUCCEEDED,
    )
    assert executed.fencing_token == 1
    assert executed.finished_at is not None

    continuation = WorkflowRun.objects.get(
        work=work,
        execution_contract_version=1,
        status=WorkflowRunStatus.QUEUED,
    )
    assert continuation.id != executed.id
    assert continuation.dispatched_at is not None
    assert CeleryOutbox.objects.filter(
        task_name='engram.memory.distill_session_work_v1',
        task_id=f'workflow-work:{work.id}:run:{continuation.id}',
    ).exists()


class _RetryScheduledError(Exception):
    pass


# CONVERTED from test_distill_session_work_v1_requeued_initial_run_executes_on_automatic_redelivery.
# Old idiom (C1.3b requeue-QUEUED-claim + self.retry-on-retryable): a transient provider error
# requeued the RUNNING run back to QUEUED and scheduled self.retry so the next automatic redelivery
# re-executed the same run. The cutover removes self.retry for domain failures: the leased v1 run is
# FAILED with a typed retrying class, the work moves to retry_wait with a bounded next_retry_at, and
# the task RAISES for observability. The logical reconciler -- not automatic Celery redelivery --
# creates the later attempt after next_retry_at.
@pytest.mark.django_db
def test_distill_session_work_v1_transient_failure_records_retry_wait_without_self_retry(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope()
    create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    create_observation(organization, project, team, agent, session, index=2)
    work = create_session_distillation_work(session, upper=2)

    gateway = _NoSignalStageGateway(error=ConnectionError('transient distill failure'))
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )
    m_retry = mock.Mock(side_effect=_RetryScheduledError)

    with mock.patch.object(distill_session_work_v1, 'retry', m_retry):
        with pytest.raises(DistillationStageError):
            distill_session_work_v1(str(work.id))

    m_retry.assert_not_called()
    run = WorkflowRun.objects.get(work=work, execution_contract_version=1)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.failure_class in _RETRYING_CLASSES
    assert run.failure_code == 'dependency_unreachable'
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.RETRY_WAIT
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
    assert work.next_retry_at is not None
    assert work.failure_streak == 1
    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).count() == 0


class _InjectedFinalizationError(Exception):
    pass


class _InjectedAcceptedStageError(Exception):
    pass


class _NoSignalStageGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[ProviderCallInput] = []
        self.error = error

    def _record(self, data: ProviderCallInput) -> ProviderCallRecord:
        policy = data.policy
        return ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=getattr(data, 'trace_id', ''),
            redaction_state='redacted',
            metadata={'prompt_retained': False},
        )

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls.append(data)
        observation_ids = re.findall(r'^Observation: ([0-9a-f-]{36})$', data.prompt, flags=re.MULTILINE)
        assert observation_ids
        policy = data.policy
        record = self._record(data)
        if self.error is not None:
            raise self.error

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state='redacted',
            generated_title='',
            generated_body=json.dumps(
                {
                    'memories': [],
                    'no_signal_observation_ids': observation_ids,
                }
            ),
        )


class _SignalReductionStageGateway(_NoSignalStageGateway):
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls.append(data)
        policy = data.policy
        record = self._record(data)
        if data.response_kind == 'distill_extract.v1':
            observation_ids = re.findall(r'^Observation: ([0-9a-f-]{36})$', data.prompt, flags=re.MULTILINE)
            assert observation_ids
            payload = {
                'memories': [
                    {
                        'title': f'Durable fact {observation_ids[0]}',
                        'body': 'A durable fact grounded in this extraction chunk.',
                        'confidence': 0.9,
                        'supporting_observation_ids': observation_ids,
                    }
                ],
                'no_signal_observation_ids': [],
            }
        else:
            prompt = json.loads(data.prompt)
            source_refs = [draft['index'] for draft in prompt['drafts']]
            payload = {
                'memories': [
                    {
                        'title': 'Consolidated durable fact',
                        'body': 'One reduced fact preserving every extraction leaf.',
                        'confidence': 0.95,
                        'source_refs': source_refs,
                    }
                ]
            }

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state='redacted',
            generated_title='',
            generated_body=json.dumps(payload),
        )


@pytest.mark.django_db
def test_partial_oversized_session_resumes_uncovered_chunks(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='oversized-resume')
    create_curation_policy(organization, team, project)
    observations = [
        create_observation(
            organization,
            project,
            team,
            agent,
            session,
            index=index,
            body='x' * 4500,
            narrative='y' * 4500,
        )
        for index in range(1, 102)
    ]
    work = create_session_distillation_work(session, upper=101)
    gateway = _NoSignalStageGateway()
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '2')
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )
    now = timezone.now()
    first_claim_result = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'test:{uuid.uuid4()}',
        now=now,
        lease_for=timedelta(minutes=12),
    )
    assert first_claim_result.claim is not None
    injected = False

    def fail_after_first_accepted_stage(point: str) -> None:
        nonlocal injected
        if point == 'stage_completed' and not injected:
            injected = True
            raise _InjectedAcceptedStageError

    with pytest.raises(_InjectedAcceptedStageError):
        run_complete_distillation_attempt(
            work=work,
            claim=first_claim_result.claim,
            now=now,
            fault_injector=fail_after_first_accepted_stage,
        )

    assert DistillationStage.objects.filter(window__work=work, status='complete').count() == 1
    now += timedelta(minutes=13)
    attempts_after_fault = 0
    while WorkflowWork.objects.get(id=work.id).disposition == WorkflowWorkDisposition.REQUIRED:
        queued_run = (
            WorkflowRun.objects.filter(
                work=work,
                execution_contract_version=1,
                status=WorkflowRunStatus.QUEUED,
            )
            .order_by('created_at', 'id')
            .first()
        )
        claim_result = claim_work(
            work_id=work.id,
            expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
            lease_owner=f'test:{uuid.uuid4()}',
            now=now,
            lease_for=timedelta(minutes=12),
            workflow_run_id=queued_run.id if queued_run is not None else None,
        )
        assert claim_result.claim is not None
        run_complete_distillation_attempt(work=work, claim=claim_result.claim, now=now)
        attempts_after_fault += 1
        assert attempts_after_fault < 60
        now += timedelta(seconds=1)

    window = DistillationWindow.objects.get(work=work)
    assert window.chunks.count() == 101
    accepted = DistillationStage.objects.filter(window=window, stage_kind='extract', status='complete')
    assert accepted.count() == 101
    assert accepted.values('chunk_id').distinct().count() == 101
    coverage = DistillationObservationCoverage.objects.filter(window=window)
    assert coverage.count() == 101
    assert coverage.values('observation_id').distinct().count() == 101
    assert set(coverage.values_list('observation_id', flat=True)) == {item.id for item in observations}
    assert set(coverage.values_list('outcome', flat=True)) == {'no_signal'}
    assert len(gateway.calls) == 101
    assert attempts_after_fault > 1
    assert WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.SUCCEEDED).count() > 1
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert work.resolution_reason == WorkflowWorkResolutionReason.NO_SIGNAL


@pytest.mark.django_db
def test_complete_distillation_reduces_every_leaf_before_signal_finalization(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='reduction-finalization')
    create_curation_policy(organization, team, project)
    observations = [
        create_observation(
            organization,
            project,
            team,
            agent,
            session,
            index=index,
            body='x' * 4500,
            narrative='y' * 4500,
        )
        for index in range(1, 4)
    ]
    work = create_session_distillation_work(session, upper=3)
    gateway = _SignalReductionStageGateway()
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    window = DistillationWindow.objects.get(work=work)
    assert DistillationStage.objects.filter(window=window, stage_kind='extract', status='complete').count() == 3
    assert DistillationStage.objects.filter(window=window, stage_kind='reduce', status='complete').count() == 1
    candidate = MemoryCandidate.objects.get(project=project)
    assert candidate.title == 'Consolidated durable fact'
    sources = MemoryCandidateSource.objects.filter(candidate=candidate, window=window)
    assert sources.count() == 3
    assert set(sources.values_list('observation_id', flat=True)) == {item.id for item in observations}
    coverage = DistillationObservationCoverage.objects.filter(window=window)
    assert coverage.count() == 3
    assert set(coverage.values_list('outcome', flat=True)) == {'signal'}
    decision_work = WorkflowWork.objects.get(
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_id=candidate.id,
    )
    assert decision_work.disposition == WorkflowWorkDisposition.REQUIRED
    assert (
        CeleryOutbox.objects.filter(
            task_name='engram.memory.process_candidate_decision_work_v1',
        ).count()
        == 1
    )
    assert len(gateway.calls) == 4
    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_candidate_and_decision_work_signal_commit_or_roll_back_together() -> None:
    organization, team, project, agent, session = create_session_scope(suffix='finalization')
    policy = create_curation_policy(organization, team, project)
    observation = create_observation(organization, project, team, agent, session, index=1)
    work = create_session_distillation_work(session, upper=1)
    window = materialize_distillation_window(work)
    chunk = window.chunks.get()
    manifest_entry = chunk.input_manifest['observations'][0]
    now = timezone.now()
    call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'distill-stage:{uuid.uuid4()}',
        redaction_state='redacted',
    )
    stage_snapshot = {
        'memories': [
            {
                'title': 'Atomic candidate',
                'body': 'Atomic candidate body',
                'confidence': '0.900',
                'supporting_observation_ids': [str(observation.id)],
                'kind': 'gotcha',
            }
        ],
        'no_signal_observation_ids': [],
    }
    target_key = stage_target_key(
        work_id=str(work.id),
        work_input_fingerprint=work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind='extract',
        level=0,
        ordinal=0,
        chunk_ordinal=0,
        input_hash=chunk.input_hash,
        prompt_contract='distill_extract.v1',
    )
    stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        chunk=chunk,
        stage_kind='extract',
        level=0,
        ordinal=0,
        target_key=target_key,
        stage_key=stage_key(
            target_key=target_key,
            policy_id=str(policy.id),
            policy_version=policy.version,
            policy_role='primary',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract='distill_extract.v1',
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='complete',
        attempt_count=1,
        accepted_provider_call=call,
        response_hash='3' * 64,
        response_size=32,
        output_snapshot=stage_snapshot,
        output_hash=hashlib.sha256(canonical_json_bytes(stage_snapshot)).hexdigest(),
        completed_at=now,
    )
    anchor_input = {
        'observation_id': str(observation.id),
        'session_sequence': observation.session_sequence,
        'observation_digest': manifest_entry['content_digest'],
        'files_read': observation.files_read,
        'files_modified': observation.files_modified,
        'source_metadata': observation.source_metadata,
    }
    anchors = candidate_source_anchors(anchor_input)
    source_plan = CandidateSourcePlan(
        observation_id=str(observation.id),
        session_sequence=observation.session_sequence,
        observation_digest=manifest_entry['content_digest'],
        lineage_stage_key=stage.stage_key,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )
    candidate_plan = CandidatePlan(
        final_draft_id='final-draft',
        title='Atomic candidate',
        body='Atomic candidate body',
        confidence=Decimal('0.900'),
        kind='gotcha',
        deciding_stage_key=stage.stage_key,
        sources=(source_plan,),
        content_hash=session_candidate_content_hash(session.id, 'Atomic candidate', 'Atomic candidate body'),
    )
    plan = FinalizationPlan(
        scope={
            'organization_id': organization.id,
            'project_id': project.id,
            'team_id': team.id,
            'session_id': session.id,
        },
        candidates=(candidate_plan,),
        coverage=(
            CoveragePlan(
                observation_id=str(observation.id),
                session_sequence=observation.session_sequence,
                observation_digest=manifest_entry['content_digest'],
                outcome='signal',
                deciding_stage_key=stage.stage_key,
            ),
        ),
        has_signal=True,
        intent='signal',
        window_input_hash=window.input_hash,
    )
    unrelated_call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'distill-stage:{uuid.uuid4()}',
        redaction_state='redacted',
    )
    unrelated_prompt_contract = 'distill_extract.unrelated-v1'
    unrelated_target_key = stage_target_key(
        work_id=str(work.id),
        work_input_fingerprint=work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind='extract',
        level=0,
        ordinal=0,
        chunk_ordinal=0,
        input_hash=chunk.input_hash,
        prompt_contract=unrelated_prompt_contract,
    )
    unrelated_stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        chunk=chunk,
        stage_kind='extract',
        level=0,
        ordinal=0,
        target_key=unrelated_target_key,
        stage_key=stage_key(
            target_key=unrelated_target_key,
            policy_id=str(policy.id),
            policy_version=policy.version,
            policy_role='fallback',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract=unrelated_prompt_contract,
        policy=policy,
        policy_version=policy.version,
        policy_role='fallback',
        status='complete',
        attempt_count=1,
        accepted_provider_call=unrelated_call,
        response_hash='4' * 64,
        response_size=32,
        output_snapshot=stage_snapshot,
        output_hash=hashlib.sha256(canonical_json_bytes(stage_snapshot)).hexdigest(),
        completed_at=now,
    )
    claim_result = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'test:{uuid.uuid4()}',
        now=now,
        lease_for=timedelta(minutes=10),
    )
    assert claim_result.claim is not None
    claim = claim_result.claim
    unrelated_coverage_plan = replace(
        plan,
        coverage=(replace(plan.coverage[0], deciding_stage_key=unrelated_stage.stage_key),),
    )
    with pytest.raises(MemoryWorkerError, match='signal coverage deciding stage'):
        finalize_distillation(window=window, claim=claim, plan=unrelated_coverage_plan, now=now)

    for fault_point in ('candidate', 'source', 'coverage', 'work', 'package', 'root'):

        def inject(point: str, *, expected: str = fault_point) -> None:
            if point == expected:
                raise _InjectedFinalizationError(point)

        with pytest.raises(_InjectedFinalizationError, match=fault_point):
            finalize_distillation(window=window, claim=claim, plan=plan, now=now, fault_injector=inject)

        assert MemoryCandidate.objects.filter(project=project).count() == 0
        assert MemoryCandidateSource.objects.filter(window=window).count() == 0
        assert DistillationObservationCoverage.objects.filter(window=window).count() == 0
        assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.CANDIDATE_DECISION).count() == 0
        assert WorkflowRun.objects.filter(run_type=WorkflowWorkType.CANDIDATE_DECISION).count() == 0
        assert CeleryOutbox.objects.filter(task_name='engram.memory.process_candidate_decision_work_v1').count() == 0
        work.refresh_from_db()
        assert work.disposition == WorkflowWorkDisposition.REQUIRED
        assert DistillationStage.objects.get(id=stage.id).status == 'complete'

    finalize_distillation(window=window, claim=claim, plan=plan, now=now)

    candidate = MemoryCandidate.objects.get(project=project)
    work.refresh_from_db()
    root_run = WorkflowRun.objects.get(id=claim.workflow_run_id)
    assert candidate.decision_work_contract_version == 1
    assert MemoryCandidateSource.objects.filter(candidate=candidate, window=window).count() == 1
    assert DistillationObservationCoverage.objects.filter(window=window, outcome='signal').count() == 1
    assert (
        WorkflowWork.objects.filter(
            work_type=WorkflowWorkType.CANDIDATE_DECISION,
            subject_id=candidate.id,
        ).count()
        == 1
    )
    assert WorkflowRun.objects.filter(run_type=WorkflowWorkType.CANDIDATE_DECISION).count() == 1
    assert CeleryOutbox.objects.filter(task_name='engram.memory.process_candidate_decision_work_v1').count() == 1
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert root_run.status == WorkflowRunStatus.SUCCEEDED


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _complete_reduce_stage(
    window: DistillationWindow,
    policy: ModelPolicy,
    *,
    level: int,
    ordinal: int,
    prompt_contract: str,
) -> DistillationStage:
    seed = f'{prompt_contract}:{level}:{ordinal}'
    call = ProviderCallRecord.objects.create(
        organization=window.organization,
        project=window.project,
        team=window.team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'{seed}:{window.id}',
        redaction_state='redacted',
    )
    return DistillationStage.objects.create(
        organization=window.organization,
        project=window.project,
        team=window.team,
        window=window,
        stage_kind='reduce',
        level=level,
        ordinal=ordinal,
        target_key=_hex(f'{seed}:target'),
        stage_key=_hex(f'{seed}:stage'),
        input_hash=_hex(f'{seed}:input'),
        input_manifest={'schema': 'distillation_reduce_manifest.v1', 'level': level, 'ordinal': ordinal, 'refs': []},
        prompt_contract=prompt_contract,
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='complete',
        attempt_count=1,
        accepted_provider_call=call,
        response_hash=_hex(f'{seed}:response'),
        response_size=1,
        output_snapshot={'memories': []},
        output_hash=_hex(f'{seed}:output'),
        completed_at=timezone.now(),
    )


@pytest.mark.django_db
def test_reduce_accepted_set_excludes_non_v2_prompt_contract_rows() -> None:
    organization, team, project, agent, session = create_session_scope(suffix='accepted-isolation')
    policy = create_curation_policy(organization, team, project)
    create_observation(organization, project, team, agent, session, index=1)
    work = create_session_distillation_work(session, upper=1)
    window = materialize_distillation_window(work)
    legacy = _complete_reduce_stage(window, policy, level=1, ordinal=0, prompt_contract='distill_reduce.v1')
    current = _complete_reduce_stage(window, policy, level=1, ordinal=1, prompt_contract='distill_reduce.v2')

    filtered = _accepted_stage_rows(window, 'reduce', prompt_contract='distill_reduce.v2')
    assert [stage.id for stage in filtered] == [current.id]

    unfiltered = _accepted_stage_rows(window, 'reduce')
    assert {stage.id for stage in unfiltered} == {legacy.id, current.id}


class _TruncatingThenValidReductionGateway(_NoSignalStageGateway):
    def __init__(self) -> None:
        super().__init__()
        self.reduce_calls = 0

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        self.calls.append(data)
        policy = data.policy
        record = self._record(data)
        if data.response_kind == 'distill_extract.v1':
            observation_ids = re.findall(r'^Observation: ([0-9a-f-]{36})$', data.prompt, flags=re.MULTILINE)
            assert observation_ids
            payload: dict[str, object] = {
                'memories': [
                    {
                        'title': f'Durable fact {observation_ids[0]}',
                        'body': 'A durable fact grounded in this extraction chunk.',
                        'confidence': 0.9,
                        'supporting_observation_ids': observation_ids,
                    }
                ],
                'no_signal_observation_ids': [],
            }
            finish_reason = ''
        else:
            self.reduce_calls += 1
            prompt = json.loads(data.prompt)
            source_refs = [draft['index'] for draft in prompt['drafts']]
            payload = {
                'memories': [
                    {
                        'title': 'Consolidated durable fact',
                        'body': 'One reduced fact preserving every extraction leaf.',
                        'confidence': 0.95,
                        'source_refs': source_refs,
                    }
                ]
            }
            finish_reason = 'length' if self.reduce_calls == 1 else ''

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state='redacted',
            generated_title='',
            generated_body=json.dumps(payload),
            finish_reason=finish_reason,
        )


@pytest.mark.django_db
def test_reduce_truncation_bumps_generation_to_disjoint_level_band(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='truncation-remediation')
    create_curation_policy(organization, team, project)
    for index in range(1, 4):
        create_observation(
            organization, project, team, agent, session, index=index, body='x' * 4500, narrative='y' * 4500
        )
    work = create_session_distillation_work(session, upper=3)
    gateway = _TruncatingThenValidReductionGateway()
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setenv('ENGRAM_DISTILL_REDUCE_TARGET', '1')
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    now = timezone.now()
    first = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'test:{uuid.uuid4()}',
        now=now,
        lease_for=timedelta(minutes=12),
    )
    assert first.claim is not None
    with pytest.raises(DistillationStageError):
        run_complete_distillation_attempt(work=work, claim=first.claim, now=now)

    window = DistillationWindow.objects.get(work=work)
    marker = DistillationStage.objects.get(
        window=window,
        stage_kind='reduce',
        status='required',
        last_failure_class=PROVIDER_OUTPUT_TRUNCATED,
    )
    assert 1 <= marker.level <= 4

    now += timedelta(minutes=13)
    attempts = 0
    while WorkflowWork.objects.get(id=work.id).disposition == WorkflowWorkDisposition.REQUIRED:
        queued = (
            WorkflowRun.objects.filter(work=work, execution_contract_version=1, status=WorkflowRunStatus.QUEUED)
            .order_by('created_at', 'id')
            .first()
        )
        claim_result = claim_work(
            work_id=work.id,
            expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
            lease_owner=f'test:{uuid.uuid4()}',
            now=now,
            lease_for=timedelta(minutes=12),
            workflow_run_id=queued.id if queued is not None else None,
        )
        assert claim_result.claim is not None
        run_complete_distillation_attempt(work=work, claim=claim_result.claim, now=now)
        attempts += 1
        assert attempts < 20
        now += timedelta(seconds=1)

    generation_one = DistillationStage.objects.filter(
        window=window, stage_kind='reduce', prompt_contract='distill_reduce.v2', level__gte=17, level__lte=20
    )
    assert generation_one.filter(status='complete').exists()
    assert not DistillationStage.objects.filter(window=window, stage_kind='reduce', level__gte=48).exists()

    marker.refresh_from_db()
    assert marker.status == 'required'

    work.refresh_from_db()
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_large_session_distills_without_malformed_or_truncated_and_covers_every_observation(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='large-session')
    create_curation_policy(organization, team, project)
    observations = [
        create_observation(
            organization, project, team, agent, session, index=index, body='x' * 4500, narrative='y' * 4500
        )
        for index in range(1, 101)
    ]
    work = create_session_distillation_work(session, upper=100)
    gateway = _SignalReductionStageGateway()
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setenv('ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT', '40')
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    now = timezone.now()
    attempts = 0
    while WorkflowWork.objects.get(id=work.id).disposition == WorkflowWorkDisposition.REQUIRED:
        queued = (
            WorkflowRun.objects.filter(work=work, execution_contract_version=1, status=WorkflowRunStatus.QUEUED)
            .order_by('created_at', 'id')
            .first()
        )
        claim_result = claim_work(
            work_id=work.id,
            expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
            lease_owner=f'test:{uuid.uuid4()}',
            now=now,
            lease_for=timedelta(minutes=12),
            workflow_run_id=queued.id if queued is not None else None,
        )
        assert claim_result.claim is not None
        run_complete_distillation_attempt(work=work, claim=claim_result.claim, now=now)
        attempts += 1
        assert attempts < 60
        now += timedelta(seconds=1)

    window = DistillationWindow.objects.get(work=work)
    assert not DistillationStage.objects.filter(window=window, last_failure_class=PROVIDER_OUTPUT_MALFORMED).exists()
    assert not DistillationStage.objects.filter(window=window, last_failure_class=PROVIDER_OUTPUT_TRUNCATED).exists()
    coverage = DistillationObservationCoverage.objects.filter(window=window)
    assert set(coverage.values_list('observation_id', flat=True)) == {item.id for item in observations}
    assert set(coverage.values_list('outcome', flat=True)) == {'signal'}
    candidate_count = MemoryCandidate.objects.filter(project=project).count()
    assert 1 <= candidate_count <= 25
    work.refresh_from_db()
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_small_session_never_calls_reduce(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, agent, session = create_session_scope(suffix='small-session')
    create_curation_policy(organization, team, project)
    observations = [
        create_observation(
            organization, project, team, agent, session, index=index, body='x' * 4500, narrative='y' * 4500
        )
        for index in range(1, 4)
    ]
    work = create_session_distillation_work(session, upper=3)
    gateway = _SignalReductionStageGateway()
    m_monkeypatch.setenv('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET', '8000')
    m_monkeypatch.setattr(
        'engram.memory.distillation_provider_stage.get_provider_gateway',
        lambda *_args, **_kwargs: gateway,
    )

    distill_session_work_v1(str(work.id))

    window = DistillationWindow.objects.get(work=work)
    assert DistillationStage.objects.filter(window=window, stage_kind='extract', status='complete').count() == 3
    assert DistillationStage.objects.filter(window=window, stage_kind='reduce').count() == 0
    coverage = DistillationObservationCoverage.objects.filter(window=window)
    assert set(coverage.values_list('observation_id', flat=True)) == {item.id for item in observations}
    assert MemoryCandidate.objects.filter(project=project).count() == 3
    work.refresh_from_db()
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED
