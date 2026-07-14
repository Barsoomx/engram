from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    DistillationStage,
    DistillationWindow,
    MemoryCandidate,
    MemoryCandidateSource,
    Observation,
    Organization,
    Project,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory import candidate_work_reconciler
from engram.memory.candidate_decision_work import (
    build_candidate_decision_input,
    ensure_candidate_decision_work_locked,
    get_candidate_decision_work_builder,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    resolve_work_succeeded,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret


@pytest.fixture(autouse=True)
def f_reset_candidate_builder() -> None:
    yield
    candidate_work_reconciler.set_candidate_decision_work_builder(None)


def _scope(suffix: str) -> tuple[Organization, Team, Project, Agent, AgentSession]:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id=f'agent-{suffix}')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime='codex',
    )
    return organization, team, project, agent, session


def _candidate(scope: tuple[Organization, Team, Project, Agent, AgentSession], suffix: str) -> MemoryCandidate:
    organization, team, project, _agent, _session = scope
    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=f'Title {suffix}',
        body=f'Body {suffix}',
        status='proposed',
        content_hash=hashlib.sha256(f'content-{suffix}'.encode()).hexdigest(),
        confidence=Decimal('0.900'),
        evidence=[],
    )


def _mark_cp3_candidate(
    scope: tuple[Organization, Team, Project, Agent, AgentSession],
    candidate: MemoryCandidate,
) -> None:
    organization, team, project, agent, session = scope
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='tool_use',
        title='CP3 source',
        content_hash=hashlib.sha256(b'cp3-source').hexdigest(),
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )
    with transaction.atomic():
        work, _created = create_work(
            CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=session.id,
                input_snapshot={
                    'schema': 'session_distillation_input/v1',
                    'session_id': str(session.id),
                    'lower_sequence_exclusive': 0,
                    'upper_sequence_inclusive': 1,
                },
            )
        )
    window = DistillationWindow.objects.create(
        organization=organization,
        project=project,
        team=team,
        work=work,
        session=session,
        contract_version=1,
        lower_sequence_exclusive=0,
        upper_sequence_inclusive=1,
        observation_count=1,
        input_hash='1' * 64,
        chunk_char_budget=8000,
        reduction_target=1,
        chunk_contract_version=1,
    )
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name='CP3 test secret',
        provider='openai',
        scope='team',
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        secret=secret,
        name='CP3 test policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='cp3-test-model',
    )
    call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=team,
        policy=policy,
        secret=secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'cp3-source:{candidate.id}',
        redaction_state='redacted',
    )
    snapshot = {
        'memories': [
            {
                'title': candidate.title,
                'body': candidate.body,
                'confidence': str(candidate.confidence),
                'source_ids': ['leaf-source'],
            }
        ]
    }
    stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=team,
        window=window,
        stage_kind='reduce',
        level=1,
        ordinal=0,
        target_key='2' * 64,
        stage_key='3' * 64,
        input_hash='4' * 64,
        input_manifest={
            'schema': 'distillation_reduce_manifest.v1',
            'level': 1,
            'ordinal': 0,
            'refs': [],
        },
        prompt_contract='distill_reduce.v1',
        policy=policy,
        policy_version=policy.version,
        policy_role='primary',
        status='complete',
        attempt_count=1,
        accepted_provider_call=call,
        response_hash='5' * 64,
        response_size=1,
        output_snapshot=snapshot,
        output_hash=hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest(),
        completed_at=timezone.now(),
    )
    MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=team,
        candidate=candidate,
        window=window,
        observation=observation,
        stage=stage,
        anchors={'schema': 'candidate_source_anchors.v1'},
        anchors_hash='6' * 64,
    )
    MemoryCandidate.objects.filter(id=candidate.id).update(decision_work_contract_version=1)
    candidate.refresh_from_db()


@pytest.mark.django_db
def test_build_candidate_input_hashes_ordered_source_manifest() -> None:
    scope = _scope('manifest')
    candidate = _candidate(scope, '1')
    sources = [
        {
            'window_input_hash': 'b' * 64,
            'session_sequence': 2,
            'observation_id': str(uuid.uuid4()),
            'observation_digest': 'd' * 64,
            'stage_key': 'f' * 64,
            'anchors_hash': 'e' * 64,
        },
        {
            'window_input_hash': 'a' * 64,
            'session_sequence': 1,
            'observation_id': str(uuid.uuid4()),
            'observation_digest': 'c' * 64,
            'stage_key': 'a' * 64,
            'anchors_hash': 'b' * 64,
        },
    ]

    value = build_candidate_decision_input(candidate, sources=sources)
    ordered = sorted(
        sources,
        key=lambda item: tuple(
            item[field]
            for field in (
                'window_input_hash',
                'session_sequence',
                'observation_id',
                'observation_digest',
                'stage_key',
                'anchors_hash',
            )
        ),
    )
    expected_manifest_hash = hashlib.sha256(canonical_json_bytes(ordered)).hexdigest()

    assert value.evidence_manifest_hash == expected_manifest_hash
    assert value.candidate_id == candidate.id
    assert value.team_id == candidate.team_id


@pytest.mark.django_db
def test_ensure_candidate_work_reuses_exact_generation_and_creates_new_generation_for_new_evidence() -> None:
    scope = _scope('generations')
    candidate = _candidate(scope, '1')

    with transaction.atomic():
        first, first_created = ensure_candidate_decision_work_locked(candidate, sources=[])
        second, second_created = ensure_candidate_decision_work_locked(candidate, sources=[])

    assert first.id == second.id
    assert (first_created, second_created) == (True, False)

    source = {
        'window_input_hash': 'a' * 64,
        'session_sequence': 1,
        'observation_id': str(uuid.uuid4()),
        'observation_digest': 'b' * 64,
        'stage_key': 'c' * 64,
        'anchors_hash': 'd' * 64,
    }
    with transaction.atomic():
        newer, newer_created = ensure_candidate_decision_work_locked(candidate, sources=[source])

    assert newer.id != first.id
    assert newer_created is True
    assert WorkflowWork.objects.filter(subject_id=candidate.id).count() == 2


@pytest.mark.django_db
def test_terminal_generation_is_not_reopened_when_late_evidence_creates_new_generation() -> None:
    scope = _scope('terminal-generation')
    candidate = _candidate(scope, '1')
    with transaction.atomic():
        terminal, _created = ensure_candidate_decision_work_locked(candidate, sources=[])
        resolve_work_succeeded(
            terminal.id,
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
        )

    source = {
        'window_input_hash': 'a' * 64,
        'session_sequence': 1,
        'observation_id': str(uuid.uuid4()),
        'observation_digest': 'b' * 64,
        'stage_key': 'c' * 64,
        'anchors_hash': 'd' * 64,
    }
    with transaction.atomic():
        newer, created = ensure_candidate_decision_work_locked(candidate, sources=[source])

    terminal.refresh_from_db()
    assert created is True
    assert newer.id != terminal.id
    assert terminal.disposition == 'complete'
    assert terminal.execution_state == 'ready'


@pytest.mark.django_db
def test_candidate_work_snapshot_is_exact_and_scope_bound() -> None:
    scope = _scope('snapshot')
    organization, team, project, _agent, _session = scope
    candidate = _candidate(scope, '1')
    value = build_candidate_decision_input(candidate, sources=[])

    with transaction.atomic():
        work, created = create_work(
            __import__('engram.memory.workflow_work', fromlist=['CreateWorkflowWorkInput']).CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.CANDIDATE_DECISION,
                subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
                subject_id=candidate.id,
                input_snapshot={
                    'schema': 'candidate_decision_input/v1',
                    'candidate_id': str(value.candidate_id),
                    'candidate_content_hash': value.candidate_content_hash,
                    'organization_id': str(value.organization_id),
                    'project_id': str(value.project_id),
                    'team_id': str(value.team_id),
                    'evidence_manifest_hash': value.evidence_manifest_hash,
                    'policy_version': value.policy_version,
                },
            )
        )

    assert created is True
    assert work.team_id == team.id


@pytest.mark.django_db
def test_reconcile_orphan_candidate_creates_one_work_and_one_pending_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('orphan')
    organization, _team, project, _agent, _session = scope
    candidate = _candidate(scope, '1')
    _mark_cp3_candidate(scope, candidate)
    sent: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda task_name, *, args, **_kwargs: sent.append((task_name, tuple(args))),
    )
    result = candidate_work_reconciler.reconcile_candidate_work(
        organization_id=organization.id,
        project_id=project.id,
        as_of=timezone.now(),
    )

    assert result.queued == 1
    assert WorkflowWork.objects.filter(subject_id=candidate.id).count() == 1
    assert len(sent) == 1


@pytest.mark.django_db
def test_reconcile_candidate_work_never_auto_repairs_legacy_version_zero() -> None:
    scope = _scope('legacy')
    organization, _team, project, _agent, _session = scope
    candidate = _candidate(scope, 'legacy')
    candidate_work_reconciler.set_candidate_decision_work_builder(get_candidate_decision_work_builder())

    result = candidate_work_reconciler.reconcile_candidate_work(
        organization_id=organization.id,
        project_id=project.id,
        as_of=timezone.now(),
    )

    assert result.queued == 0
    assert not WorkflowWork.objects.filter(subject_id=candidate.id).exists()


@pytest.mark.django_db
def test_scheduled_candidate_reconciliation_reaches_cp3_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope('scheduled')
    candidate = _candidate(scope, 'scheduled')
    _mark_cp3_candidate(scope, candidate)
    monkeypatch.setattr('engram.memory.work_dispatch.app.send_task', lambda *_args, **_kwargs: None)

    queued = candidate_work_reconciler.reconcile_scheduled_candidate_work(as_of=timezone.now())

    assert queued == 1
    assert WorkflowWork.objects.filter(subject_id=candidate.id).count() == 1
