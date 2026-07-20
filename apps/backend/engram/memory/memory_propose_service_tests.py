from __future__ import annotations

import hashlib
import uuid

import pytest
from django_celery_outbox.models import CeleryOutbox

from engram.access.services import EffectiveScope
from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    MemoryCandidate,
    MemoryCandidateSource,
    Organization,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.memory_propose_service import (
    ProposeMemory,
    ProposeMemoryError,
    ProposeMemoryInput,
)
from engram.memory.workflow_work import canonical_json_bytes


@pytest.fixture
def f_scope() -> tuple[Organization, Project, Team]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    ProjectTeam.objects.create(organization=organization, team=team, project=project)

    return organization, project, team


def _bearer_scope(
    organization: Organization,
    *,
    team: Team | None = None,
    team_bound: bool = False,
) -> EffectiveScope:
    api_key_id = uuid.uuid4()

    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=api_key_id,
        project_ids=(),
        team_ids=(team.id,) if team is not None else (),
        capabilities=('memories:propose',),
        actor_type='api_key',
        actor_id=str(api_key_id),
        project_bound=False,
        team_bound=team_bound,
    )


def _session_scope(organization: Organization) -> EffectiveScope:
    user_id = uuid.uuid4()

    return EffectiveScope(
        organization_id=organization.id,
        identity_id=user_id,
        api_key_id=uuid.UUID(int=0),
        project_ids=(),
        team_ids=(),
        capabilities=('memories:propose',),
        actor_type='user',
        actor_id=str(user_id),
        project_bound=False,
        team_bound=False,
    )


def _input(scope: EffectiveScope, project: Project, **overrides: object) -> ProposeMemoryInput:
    values: dict[str, object] = {
        'scope': scope,
        'project': project,
        'team_id': None,
        'title': 'Deploy requires approval',
        'body': 'The production deploy pipeline requires a manual approval step.',
        'kind': '',
        'request_id': f'req-{uuid.uuid4()}',
        'correlation_id': '',
    }
    values.update(overrides)

    return ProposeMemoryInput(**values)


@pytest.mark.django_db
def test_propose_creates_proposed_candidate_with_agent_source(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)

    result = ProposeMemory().execute(_input(scope, project, title='  Deploy fact  ', kind='digest'))

    candidate = MemoryCandidate.objects.get(id=result.candidate_id)
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.confidence is None
    assert candidate.kind == ''
    assert candidate.title == 'Deploy fact'
    assert candidate.decision_work_contract_version == 1
    assert candidate.visibility_scope == VisibilityScope.PROJECT
    assert result.decision_work_queued is True

    source = MemoryCandidateSource.objects.get(candidate=candidate)
    assert source.source_kind == 'agent_proposal'
    assert source.anchors['actor_type'] == 'api_key'
    assert source.anchors['api_key_id'] == str(scope.api_key_id)

    assert WorkflowWork.objects.filter(
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate.id,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
    ).exists()


@pytest.mark.django_db
def test_propose_dispatches_within_transaction_via_outbox(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    before = CeleryOutbox.objects.count()

    result = ProposeMemory().execute(_input(_bearer_scope(organization), project))

    candidate = MemoryCandidate.objects.get(id=result.candidate_id)
    work = WorkflowWork.objects.get(subject_id=candidate.id, work_type=WorkflowWorkType.CANDIDATE_DECISION)
    assert WorkflowRun.objects.filter(work_id=work.id).exists()
    assert CeleryOutbox.objects.count() == before + 1


@pytest.mark.django_db
def test_content_hash_depends_on_kind_and_team(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, team = f_scope
    project_scope = _bearer_scope(organization)
    team_scope = _bearer_scope(organization, team=team)

    project_result = ProposeMemory().execute(_input(project_scope, project))
    team_result = ProposeMemory().execute(_input(team_scope, project, team_id=team.id))

    project_candidate = MemoryCandidate.objects.get(id=project_result.candidate_id)
    team_candidate = MemoryCandidate.objects.get(id=team_result.candidate_id)
    assert project_candidate.content_hash != team_candidate.content_hash


@pytest.mark.django_db
def test_identical_input_is_idempotent(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)

    first = ProposeMemory().execute(_input(scope, project))
    second = ProposeMemory().execute(_input(scope, project))

    assert first.candidate_id == second.candidate_id
    assert second.decision_work_queued is False
    assert MemoryCandidateSource.objects.filter(candidate_id=first.candidate_id).count() == 1
    assert AuditEvent.objects.filter(event_type='MemoryProposeReused', target_id=str(first.candidate_id)).exists()


@pytest.mark.django_db
def test_integrity_race_reloads_winner_without_second_source(
    f_scope: tuple[Organization, Project, Team],
) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)
    winner = ProposeMemory().execute(_input(scope, project))
    winner_candidate = MemoryCandidate.objects.get(id=winner.candidate_id)

    service = ProposeMemory()
    anchors = service._build_anchors(scope, _input(scope, project))
    result = service._create_new(
        scope,
        _input(scope, project),
        effective_team_id=None,
        title=winner_candidate.title,
        body=winner_candidate.body,
        clamped_kind='',
        content_hash=winner_candidate.content_hash,
        anchors=anchors,
        anchors_hash=hashlib.sha256(canonical_json_bytes(anchors)).hexdigest(),
    )

    assert result.candidate_id == winner_candidate.id
    assert MemoryCandidateSource.objects.filter(candidate_id=winner_candidate.id).count() == 1


@pytest.mark.django_db
def test_team_not_linked_to_project_raises(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    other_team = Team.objects.create(organization=organization, name='Unlinked', slug='unlinked')
    scope = _bearer_scope(organization, team=other_team)

    with pytest.raises(ProposeMemoryError) as error:
        ProposeMemory().execute(_input(scope, project, team_id=other_team.id))

    assert error.value.code == 'team_not_in_project'
    assert not MemoryCandidate.objects.exists()


@pytest.mark.django_db
def test_blank_after_redaction_raises_empty_content(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)

    with pytest.raises(ProposeMemoryError) as error:
        ProposeMemory().execute(_input(scope, project, body='   '))

    assert error.value.code == 'empty_content'
    assert not MemoryCandidate.objects.exists()


@pytest.mark.django_db
def test_post_redaction_overflow_raises_content_too_long(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)
    filler = ','.join(f'"key{index:03d}":"value{index:03d}"' for index in range(20))
    title = '{' + filler + ',"secret":"sk-abcdefghijklmnop"}'

    with pytest.raises(ProposeMemoryError) as error:
        ProposeMemory().execute(_input(scope, project, title=title))

    assert error.value.code == 'content_too_long'
    assert not MemoryCandidate.objects.exists()


@pytest.mark.django_db
def test_team_proposal_is_team_visible(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, team = f_scope
    scope = _bearer_scope(organization, team=team)

    result = ProposeMemory().execute(_input(scope, project, team_id=team.id))

    candidate = MemoryCandidate.objects.get(id=result.candidate_id)
    assert candidate.visibility_scope == VisibilityScope.TEAM
    assert candidate.team_id == team.id


@pytest.mark.django_db
def test_session_user_records_null_api_key(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _session_scope(organization)

    result = ProposeMemory().execute(_input(scope, project))

    source = MemoryCandidateSource.objects.get(candidate_id=result.candidate_id)
    assert source.anchors['actor_type'] == 'user'
    assert source.anchors['api_key_id'] is None


@pytest.mark.django_db
def test_audit_event_scopes_team_for_team_proposal(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, team = f_scope
    scope = _bearer_scope(organization, team=team)
    request_id = f'req-{uuid.uuid4()}'

    result = ProposeMemory().execute(_input(scope, project, team_id=team.id, request_id=request_id))

    audit = AuditEvent.objects.get(event_type='MemoryProposed', target_id=str(result.candidate_id))
    assert audit.team_id == team.id
    assert audit.request_id == request_id


@pytest.mark.django_db
def test_project_proposal_audit_team_is_null(f_scope: tuple[Organization, Project, Team]) -> None:
    organization, project, _team = f_scope
    scope = _bearer_scope(organization)

    result = ProposeMemory().execute(_input(scope, project))

    audit = AuditEvent.objects.get(event_type='MemoryProposed', target_id=str(result.candidate_id))
    assert audit.team_id is None
