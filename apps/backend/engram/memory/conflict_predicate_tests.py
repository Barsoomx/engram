from __future__ import annotations

import uuid

import pytest

from engram.core.models import Memory, MemoryCandidate, MemoryConflict
from engram.memory.conflict_predicate import open_memory_conflict_exists
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    open_single_conflict,
    provenanced_candidate,
    transition_request,
    transitions_module,
)


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
        actor_id='conflict-predicate-tests',
        capability='memories:admin',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason='resolved by test',
        origin='conflict-predicate-tests',
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


@pytest.mark.django_db
def test_open_memory_conflict_exists_true_for_open_conflict() -> None:
    _candidate, conflict = open_single_conflict('pred-open')

    memory = (
        Memory.objects.filter(pk=conflict.memory_id)
        .annotate(has_open_conflict=open_memory_conflict_exists('pk'))
        .first()
    )

    assert memory.has_open_conflict is True


@pytest.mark.django_db
def test_open_memory_conflict_exists_false_after_resolution() -> None:
    candidate, conflict = open_single_conflict('pred-resolved')
    _resolve_conflict(candidate, conflict)

    memory = (
        Memory.objects.filter(pk=conflict.memory_id)
        .annotate(has_open_conflict=open_memory_conflict_exists('pk'))
        .first()
    )

    assert memory.has_open_conflict is False


@pytest.mark.django_db
def test_open_memory_conflict_exists_false_when_no_conflict() -> None:
    candidate, _source, _scope = provenanced_candidate('pred-clean')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))

    memory = (
        Memory.objects.filter(pk=result.memory.id)
        .annotate(has_open_conflict=open_memory_conflict_exists('pk'))
        .first()
    )

    assert memory.has_open_conflict is False
