from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest
from django.db import transaction

from engram.core.models import (
    Agent,
    AgentSession,
    MemoryCandidate,
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
)
from engram.memory.workflow_work import canonical_json_bytes, create_work, resolve_work_succeeded


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
    sent: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda task_name, *, args, **_kwargs: sent.append((task_name, tuple(args))),
    )
    candidate_work_reconciler.register_default_candidate_decision_builder()

    result = candidate_work_reconciler.reconcile_candidate_work(
        organization_id=organization.id,
        project_id=project.id,
        as_of=__import__('django.utils.timezone', fromlist=['now']).now(),
    )

    assert result.queued == 1
    assert WorkflowWork.objects.filter(subject_id=candidate.id).count() == 1
    assert len(sent) == 1
