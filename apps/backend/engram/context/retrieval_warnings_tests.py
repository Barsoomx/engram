from __future__ import annotations

import uuid

import pytest

from engram.access.services import EffectiveScope
from engram.context.context_api_tests import create_project_scope
from engram.context.retrieval_warnings import RetrievalWarning, compute_retrieval_warnings
from engram.core.models import (
    Memory,
    MemoryCandidate,
    MemoryConflict,
    Organization,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    candidate_in_scope,
    provenanced_candidate_in_scope,
    transition_request,
    transition_request_for,
    transitions_module,
)


def _effective_scope(organization: Organization, team: Team | None) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(team.id,) if team else (),
        capabilities=(),
        actor_type='api_key',
        actor_id='svc-warning-test',
        project_bound=False,
    )


def _open_conflict_in_scope(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    suffix: str,
    title: str,
    body: str,
    visibility_scope: str = VisibilityScope.PROJECT,
) -> tuple[Memory, MemoryCandidate, MemoryConflict]:
    base_candidate, source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix=suffix,
        title=title,
        body=body,
        visibility_scope=visibility_scope,
    )
    memory_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(base_candidate))
    candidate, _candidate_source = candidate_in_scope(
        base_candidate,
        source,
        title=f'Resolution {suffix}',
        body=f'Resolution body {suffix}',
    )
    conflict = (
        transitions_module()
        .OpenMemoryConflict()
        .execute(
            transitions_module().OpenMemoryConflictInput(
                request=transition_request_for(
                    candidate, key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:v1'
                ),
                candidate_fence=candidate_fence_for(candidate),
                memory_fence=transitions_module().build_memory_fence(memory_result.memory),
                evidence_hash='e' * 64,
                redacted_reason='resolution outcome contract',
            )
        )
    )

    return memory_result.memory, candidate, MemoryConflict.objects.get(id=conflict.id)


def _resolve_conflict(candidate: MemoryCandidate, conflict: MemoryConflict) -> None:
    transitions = transitions_module()
    request = transitions.TransitionRequest(
        scope=transitions.TransitionScope(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
            team_id=candidate.team_id,
        ),
        idempotency_key=f'candidate:{candidate.id}:conflict-resolve:v1',
        actor_type='system',
        actor_id='warning-tests',
        capability='memories:admin',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason='resolved by test',
        origin='warning-tests',
    )
    transitions.ResolveMemoryConflict().execute(
        transitions.ResolveMemoryConflictInput(
            request=request,
            candidate_fence=candidate_fence_for(candidate),
            conflict_ids=(conflict.id,),
            conflict_memory_fences=(transitions.build_memory_fence(conflict.memory),),
            resolution='reject_candidate',
        ),
    )


def _conflict_excluded(warnings: list[RetrievalWarning]) -> list[RetrievalWarning]:
    return [warning for warning in warnings if warning.code == 'conflict_excluded']


@pytest.mark.django_db
def test_conflict_excluded_warning_emitted_for_matching_open_conflict() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _candidate, conflict = _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-emit',
        title='Conflict excluded emitted memory',
        body='Conflict excluded emitted body',
    )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query=memory.title,
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    conflict_warnings = _conflict_excluded(warnings)
    assert len(conflict_warnings) == 1
    assert conflict_warnings[0].memory_id == str(conflict.memory_id)
    assert memory.title in conflict_warnings[0].message


@pytest.mark.django_db
def test_conflict_excluded_absent_when_no_request_terms() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-noterms',
        title='Conflict excluded no terms memory',
        body='Conflict excluded no terms body',
    )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query='',
        file_paths=(),
        symbols=(),
        has_request_terms=False,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    assert _conflict_excluded(warnings) == []


@pytest.mark.django_db
def test_conflict_excluded_absent_when_conflict_resolved() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, candidate, conflict = _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-resolved',
        title='Conflict excluded resolved memory',
        body='Conflict excluded resolved body',
    )
    _resolve_conflict(candidate, conflict)

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query=memory.title,
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    assert _conflict_excluded(warnings) == []


@pytest.mark.django_db
def test_stale_wins_over_conflict_excluded() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _candidate, _conflict = _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-stale',
        title='Conflict excluded stale memory',
        body='Conflict excluded stale body',
    )
    Memory.objects.filter(id=memory.id).update(stale=True)
    RetrievalDocument.objects.filter(memory=memory).update(stale=True)

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query=memory.title,
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    codes = {warning.code for warning in warnings}
    assert 'stale_match' in codes
    assert _conflict_excluded(warnings) == []


@pytest.mark.django_db
def test_refuted_wins_over_conflict_excluded() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _candidate, _conflict = _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-refuted',
        title='Conflict excluded refuted memory',
        body='Conflict excluded refuted body',
    )
    Memory.objects.filter(id=memory.id).update(refuted=True)
    RetrievalDocument.objects.filter(memory=memory).update(refuted=True)

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query=memory.title,
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    codes = {warning.code for warning in warnings}
    assert 'refuted_match' in codes
    assert _conflict_excluded(warnings) == []


@pytest.mark.django_db
def test_conflict_excluded_capped_at_three() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    for index in range(4):
        _open_conflict_in_scope(
            organization,
            project,
            team,
            suffix=f'ce-cap-{index}',
            title=f'Capsharedtoken conflicted memory {index}',
            body=f'Capsharedtoken conflicted body {index}',
        )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query='capsharedtoken',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    conflict_warnings = _conflict_excluded(warnings)
    assert len(conflict_warnings) == 3
    assert len({warning.memory_id for warning in conflict_warnings}) == 3


@pytest.mark.django_db
def test_conflict_excluded_below_min_score_not_emitted() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-lowscore',
        title='Score forty title alpha',
        body='uniquebodyword sits only inside the body prose',
    )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query='uniquebodyword',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    assert _conflict_excluded(warnings) == []


@pytest.mark.django_db
def test_conflict_excluded_excludes_memory_outside_team_scope() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    other_team = Team.objects.create(organization=organization, name='Security', slug='security-ce')
    ProjectTeam.objects.create(organization=organization, team=other_team, project=project)
    memory, _candidate, conflict = _open_conflict_in_scope(
        organization,
        project,
        other_team,
        suffix='ce-teamscope',
        title='Team scoped conflicted memory',
        body='Team scoped conflicted body',
        visibility_scope=VisibilityScope.TEAM,
    )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query=memory.title,
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    assert _conflict_excluded(warnings) == []
    rendered = str([warning.to_dict() for warning in warnings])
    assert memory.title not in rendered
    assert str(conflict.memory_id) not in rendered


@pytest.mark.django_db
def test_conflict_excluded_message_redacts_secret_shaped_title() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    leaked_token = 'egk_memory_secret_0123456789abcdefghijklmnopqrstuvwxyz'
    _memory, _candidate, _conflict = _open_conflict_in_scope(
        organization,
        project,
        team,
        suffix='ce-redact',
        title=f'Redactword conflicted {leaked_token}',
        body='Redactword conflicted body',
    )

    warnings = compute_retrieval_warnings(
        organization=organization,
        project=project,
        scope=_effective_scope(organization, team),
        query='redactword',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
        included_matches=(),
        semantic_unavailable=False,
        kinds=(),
    )

    conflict_warnings = _conflict_excluded(warnings)
    assert len(conflict_warnings) == 1
    assert leaked_token not in conflict_warnings[0].message
