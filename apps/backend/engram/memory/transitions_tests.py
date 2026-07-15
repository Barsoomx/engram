from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    MemoryLink,
    MemoryStatus,
    Observation,
    RetrievalDocument,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked, evidence_manifest
from engram.memory.distillation_provenance import (
    candidate_source_anchors,
    canonical_source_manifest,
)
from engram.memory.transitions import (
    ArchiveMemory,
    MarkMemoryStale,
    MemoryStateInput,
    MergeMemories,
    MergeMemoriesInput,
    MergeMemoryCandidate,
    MergeMemoryCandidateInput,
    OpenMemoryConflict,
    OpenMemoryConflictInput,
    PublishDigestMemory,
    PublishDigestMemoryInput,
    RefuteMemory,
    ResolveMemoryConflict,
    ResolveMemoryConflictInput,
    RestoreMemory,
    ReviseMemory,
    ReviseMemoryFromCandidate,
    ReviseMemoryFromCandidateInput,
    ReviseMemoryInput,
    SupersedeMemories,
    SupersedeMemoriesInput,
    SupersedeMemoryWithCandidate,
    SupersedeMemoryWithCandidateInput,
)
from engram.memory.transitions_test_support import (
    candidate_fence_for as _candidate_fence_for,
)
from engram.memory.transitions_test_support import (
    candidate_in_scope as _candidate_in_scope,
)
from engram.memory.transitions_test_support import (
    open_single_conflict as _open_single_conflict,
)
from engram.memory.transitions_test_support import (
    promoted_pair as _promoted_pair,
)
from engram.memory.transitions_test_support import (
    provenanced_candidate as _provenanced_candidate,
)
from engram.memory.transitions_test_support import (
    transition_request as _request,
)
from engram.memory.transitions_test_support import (
    transition_request_for as _request_for,
)
from engram.memory.transitions_test_support import (
    transitions_module as _transitions,
)
from engram.memory.work_execution import StaleWorkFenceError, claim_work
from engram.memory.workflow_work import observation_content_digest


class InjectedPromotionFaultError(RuntimeError):
    pass


def _model(name: str) -> Any:
    from engram.core import models

    return getattr(models, name)


def _counts(candidate: MemoryCandidate) -> dict[str, int]:
    values = {
        'memory': Memory.objects.filter(project_id=candidate.project_id).count(),
        'document': RetrievalDocument.objects.filter(project_id=candidate.project_id).count(),
        'work': WorkflowWork.objects.filter(project_id=candidate.project_id).count(),
        'signal': CeleryOutbox.objects.count(),
    }
    for name, key in (
        ('MemoryTransition', 'transition'),
        ('MemoryVersionSource', 'version_source'),
        ('AuditEvent', 'audit'),
    ):
        model = getattr(__import__('engram.core.models', fromlist=[name]), name, None)
        if model is not None:
            values[key] = model.objects.filter(project_id=candidate.project_id).count()
    return values


def _lineage_shape(project_id: uuid.UUID) -> dict[str, int]:
    return {
        'transitions': _model('MemoryTransition').objects.filter(project_id=project_id).count(),
        'links': MemoryLink.objects.filter(project_id=project_id).count(),
        'audits': AuditEvent.objects.filter(project_id=project_id, event_type='MemoryTransitionCommitted').count(),
        'conflicts': MemoryConflict.objects.filter(project_id=project_id).count(),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    'boundary',
    ('memory', 'version', 'source', 'exact_document', 'audit', 'work_package', 'transition', 'candidate_pointer'),
)
def test_promote_rolls_back_at_every_named_boundary(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    candidate, _source, _scope = _provenanced_candidate(f'fault-{boundary}')
    before = _counts(candidate)
    transitions = _transitions()

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedPromotionFaultError(point)

    monkeypatch.setattr(transitions, '_fault_boundary', fault)
    with pytest.raises(InjectedPromotionFaultError, match=boundary):
        transitions.PromoteMemoryCandidate().execute(_request(candidate))

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert _counts(candidate) == before
    assert not Memory.objects.filter(project_id=candidate.project_id).exists()


@pytest.mark.django_db(transaction=True)
def test_post_commit_activity_can_be_suppressed_without_losing_exact_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate, _source, _scope = _provenanced_candidate('suppressed-post-commit')
    transitions = _transitions()
    before_signals = CeleryOutbox.objects.count()
    monkeypatch.setattr(transitions.transaction, 'on_commit', lambda _callback, **_kwargs: None)

    result = transitions.PromoteMemoryCandidate().execute(_request(candidate))

    assert result.retrieval_document.memory_version_id == result.memory_version.id
    assert RetrievalDocument.objects.filter(memory_version_id=result.memory_version.id).exists()
    assert result.embedding_work is not None
    assert WorkflowWork.objects.filter(subject_id=result.retrieval_document.id).count() == 1
    assert CeleryOutbox.objects.filter(task_name='engram.memory.embed_memory_projection_work_v1').count() == 1
    assert CeleryOutbox.objects.count() == before_signals + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_two_postgresql_threads_promote_one_candidate_into_one_chain() -> None:
    candidate, _source, _scope = _provenanced_candidate('concurrent')
    payload = _request(candidate)
    barrier = threading.Barrier(2)

    def worker() -> tuple[object, BaseException | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return _transitions().PromoteMemoryCandidate().execute(payload), None
        except BaseException as error:  # pragma: no cover - surfaced by assertion below
            return None, error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: worker(), (1, 2)))

    errors = [error for _result, error in results if error is not None]
    assert not errors
    transitions = [result for result, _error in results]
    assert sum(1 for result in transitions if getattr(result, 'duplicate', False)) == 1
    assert _model('MemoryTransition').objects.filter(candidate_id=candidate.id).count() == 1
    assert Memory.objects.filter(project_id=candidate.project_id).count() == 1
    assert RetrievalDocument.objects.filter(project_id=candidate.project_id).count() == 1
    assert (
        WorkflowWork.objects.filter(
            project_id=candidate.project_id,
            work_type='memory_embedding',
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name='engram.memory.embed_memory_projection_work_v1').count() == 1


@pytest.mark.django_db
def test_replay_is_idempotent_and_collision_or_stale_fence_writes_nothing() -> None:
    candidate, _source, _scope = _provenanced_candidate('replay')
    transitions = _transitions()
    first = transitions.PromoteMemoryCandidate().execute(_request(candidate))
    replay = transitions.PromoteMemoryCandidate().execute(_request(candidate))
    assert replay.duplicate is True
    assert replay.transition.id == first.transition.id

    with pytest.raises(Exception, match='idempotency_collision'):
        transitions.PromoteMemoryCandidate().execute(_request(candidate, reason='changed request'))

    second_candidate, _second_source, _second_scope = _provenanced_candidate('stale-fence')
    stale = _request(second_candidate)
    stale = replace(stale, candidate_fence=replace(stale.candidate_fence, candidate_content_hash='b' * 64))
    before = _counts(second_candidate)
    with pytest.raises(Exception, match='stale_decision'):
        transitions.PromoteMemoryCandidate().execute(stale)
    assert _counts(second_candidate) == before


@pytest.mark.django_db
def test_locked_candidate_content_is_recomputed_before_promotion_writes() -> None:
    candidate, _source, _scope = _provenanced_candidate('content-recompute')
    payload = _request(candidate)
    MemoryCandidate.objects.filter(id=candidate.id).update(body='mutated without refreshing the stored hash')
    before = _counts(candidate)

    with pytest.raises(Exception, match='stale_decision'):
        _transitions().PromoteMemoryCandidate().execute(payload)

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert _counts(candidate) == before


def _claim_candidate_decision_work(candidate: MemoryCandidate) -> tuple[WorkflowWork, Any]:
    with transaction.atomic():
        locked = MemoryCandidate.objects.select_for_update().get(id=candidate.id)
        work, _created = ensure_candidate_decision_work_locked(locked)
    now = timezone.now()
    claimed = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.CANDIDATE_DECISION,
        lease_owner=f'transition-test:{uuid.uuid4()}',
        now=now,
        lease_for=timedelta(minutes=5),
    )
    assert claimed.claim is not None
    return work, claimed.claim


@pytest.mark.django_db
def test_promotion_rejects_stale_owning_work_claim_before_semantic_writes() -> None:
    candidate, _source, _scope = _provenanced_candidate('stale-work-claim')
    _work, claim = _claim_candidate_decision_work(candidate)
    payload = replace(_request(candidate), work_claim=replace(claim, fencing_token=claim.fencing_token + 1))
    before = _counts(candidate)

    with pytest.raises(StaleWorkFenceError):
        _transitions().PromoteMemoryCandidate().execute(payload)

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.promoted_memory_id is None
    assert _counts(candidate) == before


@pytest.mark.django_db
def test_promotion_finishes_owning_work_claim_in_the_semantic_transaction() -> None:
    candidate, _source, _scope = _provenanced_candidate('valid-work-claim')
    work, claim = _claim_candidate_decision_work(candidate)

    result = _transitions().PromoteMemoryCandidate().execute(replace(_request(candidate), work_claim=claim))

    work.refresh_from_db()
    run = WorkflowRun.objects.get(id=claim.workflow_run_id)
    assert work.execution_state == WorkflowWorkExecutionState.SETTLED
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.result_memory_id == result.memory.id


@pytest.mark.django_db
@pytest.mark.parametrize('command_name', ('revise', 'state'))
def test_unrelated_same_scope_claim_cannot_be_consumed_by_lineage_command(command_name: str) -> None:
    candidate, source, _scope = _provenanced_candidate(f'unrelated-work-{command_name}')
    promoted = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    unrelated, _unrelated_source = _candidate_in_scope(
        candidate,
        source,
        title=f'Unrelated claimed work {command_name}',
        body=f'Unrelated claimed work body {command_name}',
    )
    work, claim = _claim_candidate_decision_work(unrelated)
    request = _request_for(candidate, key=f'request:{uuid.uuid4()}:{command_name}:{promoted.memory.id}:v1')
    if command_name == 'revise':
        command = ReviseMemory()
        payload = ReviseMemoryInput(
            request=request,
            memory_fence=_transitions().build_memory_fence(promoted.memory),
            title='Unrelated work revise',
            body='Unrelated work revise body',
            work_claim=claim,
        )
    else:
        command = MarkMemoryStale()
        payload = MemoryStateInput(
            request=request,
            memory_fence=_transitions().build_memory_fence(promoted.memory),
            work_claim=claim,
        )

    with pytest.raises(Exception, match='stale_decision'):
        command.execute(payload)

    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.LEASED
    assert work.disposition == WorkflowWorkDisposition.REQUIRED


@pytest.mark.django_db
def test_foreign_scope_is_rejected_before_semantic_or_package_writes() -> None:
    candidate, _source, _scope = _provenanced_candidate('foreign-target')
    foreign_candidate, _foreign_source, _foreign_scope = _provenanced_candidate('foreign-request')
    request = _request(candidate)
    request = replace(
        request,
        request=replace(
            request.request,
            scope=replace(
                request.request.scope,
                organization_id=foreign_candidate.organization_id,
                project_id=foreign_candidate.project_id,
                team_id=foreign_candidate.team_id,
            ),
        ),
    )
    before = _counts(candidate)

    with pytest.raises(Exception, match='scope'):
        _transitions().PromoteMemoryCandidate().execute(request)
    assert _counts(candidate) == before


@pytest.mark.django_db
def test_source_committed_before_request_is_copied_into_version_provenance() -> None:
    candidate, source, _scope = _provenanced_candidate('source-before')
    result = _transitions().PromoteMemoryCandidate().execute(_request(candidate))

    version_source = _model('MemoryVersionSource').objects.get(memory_version_id=result.memory_version.id)
    assert version_source.candidate_source_id == source.id
    assert result.transition.audit_event.metadata['exact_document_id'] == str(result.retrieval_document.id)
    assert (
        result.transition.audit_event.metadata['exact_projection_hash']
        == result.retrieval_document.exact_projection_hash
    )


@pytest.mark.django_db
def test_late_source_attaches_once_without_reopening_terminal_decision_work() -> None:
    candidate, source, (organization, project, session) = _provenanced_candidate('late-source')
    result = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title='late source',
        body='late source body',
        content_hash='late-source-content',
        session_sequence=2,
    )
    window = source.window
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
        window=window,
        observation=observation,
        stage=source.stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )
    transitions = _transitions()
    _entries, manifest_hash = evidence_manifest(candidate)
    candidate_fence = transitions.CandidateFence(candidate.id, candidate.content_hash, manifest_hash)
    request = _request(candidate, key=f'candidate-source:{late_source.id}:attach:v1').request
    memory_fence = transitions.build_memory_fence(result.memory)
    attach = transitions.AttachPromotedCandidateSourceInput(
        request=request,
        candidate_fence=candidate_fence,
        memory_fence=memory_fence,
        candidate_source_id=late_source.id,
    )

    attached = transitions.AttachPromotedCandidateSource().execute(attach)
    replay = transitions.AttachPromotedCandidateSource().execute(attach)
    candidate.refresh_from_db()
    assert attached.duplicate is False
    assert replay.duplicate is True
    assert candidate.status == CandidateStatus.PROMOTED
    assert _model('MemoryTransition').objects.filter(candidate_id=candidate.id).count() == 2
    assert _model('MemoryVersionSource').objects.filter(memory_version_id=result.memory_version.id).count() == 2
    assert (
        WorkflowWork.objects.filter(
            subject_id=result.retrieval_document.id,
            work_type='memory_embedding',
        ).count()
        == 2
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'boundary',
    ('memory', 'version', 'source', 'exact_document', 'link', 'audit', 'transition', 'candidate_pointer'),
)
def test_supersession_fault_preserves_one_coherent_lineage_transition(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    loser_candidate, source, _scope = _provenanced_candidate(f'supersede-fault-{boundary}')
    loser_result = _transitions().PromoteMemoryCandidate().execute(_request(loser_candidate))
    old_version_id = loser_result.memory_version.id
    winner_candidate, _winner_source = _candidate_in_scope(
        loser_candidate,
        source,
        title=f'Superseding candidate {boundary}',
        body=f'Superseding body {boundary}',
    )
    data = SupersedeMemoryWithCandidateInput(
        request=_request_for(winner_candidate, key=f'request:{winner_candidate.id}:supersede:v1'),
        candidate_fence=_candidate_fence_for(winner_candidate),
        loser_memory_fence=_transitions().build_memory_fence(loser_result.memory),
    )

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedPromotionFaultError(point)

    monkeypatch.setattr(_transitions(), '_fault_boundary', fault)
    before = _lineage_shape(loser_candidate.project_id)
    with pytest.raises(InjectedPromotionFaultError, match=boundary):
        SupersedeMemoryWithCandidate().execute(data)

    loser_result.memory.refresh_from_db()
    winner_candidate.refresh_from_db()
    after = _lineage_shape(loser_candidate.project_id)
    complete_delta = {
        **before,
        'transitions': before['transitions'] + 1,
        'links': before['links'] + 1,
        'audits': before['audits'] + 1,
    }
    assert after in (before, complete_delta)
    if after == before:
        assert loser_result.memory.stale is False
        assert winner_candidate.status == CandidateStatus.PROPOSED
    else:
        transition = _model('MemoryTransition').objects.get(
            project_id=loser_candidate.project_id,
            transition_type='supersede',
            candidate=winner_candidate,
        )
        assert transition.from_version_id == old_version_id
        assert transition.result_memory_id == winner_candidate.promoted_memory_id
        assert transition.semantic_link_id is not None
        assert loser_result.memory.stale is True
        assert winner_candidate.status == CandidateStatus.PROMOTED


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_reverse_order_concurrent_lineage_commands_are_deadlock_free_and_one_fence_wins() -> None:
    first, second, first_result, second_result = _promoted_pair('reverse-lineage')
    barrier = threading.Barrier(2)
    first_request = _request_for(first, key=f'request:{uuid.uuid4()}:merge:{first.id}:v1')
    second_request = _request_for(second, key=f'request:{uuid.uuid4()}:merge:{second.id}:v1')
    payloads = (
        MergeMemoriesInput(
            request=first_request,
            source_memory_fence=_transitions().build_memory_fence(first_result.memory),
            result_memory_fence=_transitions().build_memory_fence(second_result.memory),
            title='Merged reverse order',
            body='Merged reverse order body',
        ),
        MergeMemoriesInput(
            request=second_request,
            source_memory_fence=_transitions().build_memory_fence(second_result.memory),
            result_memory_fence=_transitions().build_memory_fence(first_result.memory),
            title='Merged reverse order',
            body='Merged reverse order body',
        ),
    )

    def worker(payload: MergeMemoriesInput) -> tuple[object | None, BaseException | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return MergeMemories().execute(payload), None
        except BaseException as error:  # pragma: no cover - surfaced by assertions below
            return None, error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, payloads))
    errors = [error for _result, error in results if error is not None]
    assert len(errors) == 1
    assert getattr(errors[0], 'code', None) == 'stale_decision'
    assert sum(result is not None for result, _error in results) == 1
    assert Memory.objects.filter(project_id=first.project_id, stale=False).count() == 2
    assert _model('MemoryTransition').objects.filter(project_id=first.project_id, transition_type='merge').count() == 1


@pytest.mark.django_db
def test_supersede_memories_stales_only_source_and_preserves_result_projection_and_embedding_work() -> None:
    first, second, first_result, second_result = _promoted_pair('supersede-preserves-result')
    source_memory = first_result.memory
    result_memory = second_result.memory
    source_document = first_result.retrieval_document
    result_document = second_result.retrieval_document
    result_document_fields = (
        result_document.full_text,
        result_document.exact_projection_hash,
        result_document.embedding_reference,
        list(result_document.embedding_vector),
        getattr(result_document, 'embedding_pgvector', None),
        result_document.embedding_projection_hash,
        result_document.embedding_projected_at,
    )
    result_transition_id = result_memory.current_transition_id
    result_version = result_memory.current_version
    embedding_work_ids = (first_result.embedding_work.id, second_result.embedding_work.id)
    embedding_work_before = list(
        WorkflowWork.objects.filter(id__in=embedding_work_ids).values().order_by('id'),
    )

    superseded = SupersedeMemories().execute(
        SupersedeMemoriesInput(
            request=_request_for(first, key=f'request:{uuid.uuid4()}:supersede-preserves-result:{first.id}:v1'),
            source_memory_fence=_transitions().build_memory_fence(source_memory),
            result_memory_fence=_transitions().build_memory_fence(result_memory),
        )
    )

    source_memory.refresh_from_db()
    result_memory.refresh_from_db()
    source_document.refresh_from_db()
    result_document.refresh_from_db()
    assert source_memory.stale is True
    assert source_memory.current_transition_id == superseded.transition.id
    assert result_memory.stale is False
    assert result_memory.current_transition_id == result_transition_id
    assert result_memory.current_version == result_version
    assert (
        result_document.full_text,
        result_document.exact_projection_hash,
        result_document.embedding_reference,
        list(result_document.embedding_vector),
        getattr(result_document, 'embedding_pgvector', None),
        result_document.embedding_projection_hash,
        result_document.embedding_projected_at,
    ) == result_document_fields
    assert result_document.stale is False
    assert source_document.stale is True
    assert list(WorkflowWork.objects.filter(id__in=embedding_work_ids).values().order_by('id')) == embedding_work_before


@pytest.mark.django_db
def test_memory_fence_for_version_n_cannot_mutate_version_n_plus_one() -> None:
    candidate, _source, _scope = _provenanced_candidate('version-fence')
    promoted = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    stale_fence = _transitions().build_memory_fence(promoted.memory)
    revised = ReviseMemory().execute(
        ReviseMemoryInput(
            request=_request_for(candidate, key=f'request:{uuid.uuid4()}:revise:{promoted.memory.id}:v1'),
            memory_fence=stale_fence,
            title='Version N plus one',
            body='Version N plus one body',
        )
    )
    before = _lineage_shape(candidate.project_id)
    with pytest.raises(Exception, match='stale_decision'):
        RefuteMemory().execute(
            MemoryStateInput(
                request=_request_for(candidate, key=f'request:{uuid.uuid4()}:refute:{promoted.memory.id}:v1'),
                memory_fence=stale_fence,
            )
        )
    promoted.memory.refresh_from_db()
    assert promoted.memory.current_transition_id == revised.transition.id
    assert promoted.memory.current_version == revised.memory_version.version
    assert _lineage_shape(candidate.project_id) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_refute_restore_concurrency_serializes_to_one_coherent_state() -> None:
    candidate, _source, _scope = _provenanced_candidate('refute-restore')
    promoted = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    refute = MemoryStateInput(
        request=_request_for(candidate, key=f'request:{uuid.uuid4()}:refute:{promoted.memory.id}:v1'),
        memory_fence=_transitions().build_memory_fence(promoted.memory),
    )
    RefuteMemory().execute(refute)
    promoted.memory.refresh_from_db()
    restore = MemoryStateInput(
        request=_request_for(candidate, key=f'request:{uuid.uuid4()}:restore:{promoted.memory.id}:v1'),
        memory_fence=_transitions().build_memory_fence(promoted.memory),
    )
    stale_refute = replace(
        refute,
        request=_request_for(candidate, key=f'request:{uuid.uuid4()}:refute:{promoted.memory.id}:v1'),
    )
    barrier = threading.Barrier(2)

    def worker(command: object, payload: MemoryStateInput) -> tuple[object | None, BaseException | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return command.execute(payload), None
        except BaseException as error:  # pragma: no cover - surfaced by assertions below
            return None, error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, (RestoreMemory(), RefuteMemory()), (restore, stale_refute)))
    assert sum(result is not None for result, _error in results) == 1
    assert sum(getattr(error, 'code', None) == 'stale_decision' for _result, error in results) == 1
    promoted.memory.refresh_from_db()
    document = RetrievalDocument.objects.get(
        memory=promoted.memory,
        memory_version__version=promoted.memory.current_version,
    )
    assert document.refuted == promoted.memory.refuted
    assert document.stale == promoted.memory.stale
    assert promoted.memory.status in (MemoryStatus.APPROVED, MemoryStatus.REFUTED)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_supersede_restore_concurrency_serializes_to_one_coherent_state() -> None:
    loser_candidate, source, _scope = _provenanced_candidate('supersede-restore')
    loser_result = _transitions().PromoteMemoryCandidate().execute(_request(loser_candidate))
    winner_candidate, _winner_source = _candidate_in_scope(
        loser_candidate,
        source,
        title='Supersede restore winner',
        body='Supersede restore winner body',
    )
    old_loser_fence = _transitions().build_memory_fence(loser_result.memory)
    supersede = SupersedeMemoryWithCandidateInput(
        request=_request_for(winner_candidate, key=f'request:{uuid.uuid4()}:supersede:{winner_candidate.id}:v1'),
        candidate_fence=_candidate_fence_for(winner_candidate),
        loser_memory_fence=old_loser_fence,
    )
    restore = MemoryStateInput(
        request=_request_for(loser_candidate, key=f'request:{uuid.uuid4()}:restore:{loser_result.memory.id}:v1'),
        memory_fence=old_loser_fence,
    )
    barrier = threading.Barrier(2)

    def worker(command: object, payload: object) -> tuple[object | None, BaseException | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return command.execute(payload), None
        except BaseException as error:  # pragma: no cover - surfaced by assertions below
            return None, error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, (SupersedeMemoryWithCandidate(), RestoreMemory()), (supersede, restore)))
    assert sum(result is not None for result, _error in results) == 1
    failures = [getattr(error, 'code', None) for _result, error in results if error is not None]
    assert failures and failures[0] in {'memory_state', 'stale_decision'}
    loser_result.memory.refresh_from_db()
    document = RetrievalDocument.objects.get(memory=loser_result.memory, memory_version__version=1)
    assert loser_result.memory.stale is True
    assert document.stale is True
    assert _model('MemoryTransition').objects.filter(
        project_id=loser_candidate.project_id,
        transition_type='supersede',
    ).count() == 1


@pytest.mark.django_db
def test_merge_preserves_both_histories_and_exact_relational_provenance() -> None:
    first, second, first_result, second_result = _promoted_pair('merge-provenance')
    result = MergeMemories().execute(
        MergeMemoriesInput(
            request=_request_for(first, key=f'request:{uuid.uuid4()}:merge:{first.id}:v1'),
            source_memory_fence=_transitions().build_memory_fence(second_result.memory),
            result_memory_fence=_transitions().build_memory_fence(first_result.memory),
            title='Merged memory',
            body='Merged memory body',
        )
    )
    source_rows = _model('MemoryVersionSource').objects.filter(memory_version_id=result.memory_version.id)
    assert source_rows.filter(source_memory_version_id=first_result.memory_version.id).exists()
    assert source_rows.filter(source_memory_version_id=second_result.memory_version.id).exists()
    assert first_result.memory.id != second_result.memory.id
    assert _model('MemoryTransition').objects.filter(
        memory_id=first_result.memory.id,
        transition_type='promote',
    ).exists()
    assert _model('MemoryTransition').objects.filter(
        memory_id=second_result.memory.id,
        transition_type='promote',
    ).exists()
    assert result.transition.semantic_link.link_type == LinkType.NARROWED_BY
    assert result.transition.from_version_id == second_result.memory_version.id
    assert result.transition.result_version_id == result.memory_version.id


@pytest.mark.django_db
@pytest.mark.parametrize('boundary', ('link', 'conflict', 'audit', 'transition'))
def test_conflict_open_fault_leaves_none_or_all_durable_rows(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    candidate, source, _scope = _provenanced_candidate(f'conflict-open-{boundary}')
    memory_result = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    compared, _compared_source = _candidate_in_scope(
        candidate,
        source,
        title=f'Conflict candidate {boundary}',
        body=f'Conflict body {boundary}',
    )
    data = OpenMemoryConflictInput(
        request=_request_for(compared, key=f'request:{uuid.uuid4()}:conflict-open:{compared.id}:v1'),
        candidate_fence=_candidate_fence_for(compared),
        memory_fence=_transitions().build_memory_fence(memory_result.memory),
        evidence_hash='a' * 64,
        redacted_reason='conflicting evidence',
    )
    before = _lineage_shape(candidate.project_id)

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedPromotionFaultError(point)

    monkeypatch.setattr(_transitions(), '_fault_boundary', fault)
    with pytest.raises(InjectedPromotionFaultError, match=boundary):
        OpenMemoryConflict().execute(data)
    after = _lineage_shape(candidate.project_id)
    complete_delta = {
        **before,
        'transitions': before['transitions'] + 1,
        'links': before['links'] + 1,
        'audits': before['audits'] + 1,
        'conflicts': before['conflicts'] + 1,
    }
    assert after in (before, complete_delta)
    conflict_rows = MemoryConflict.objects.filter(candidate=compared, memory=memory_result.memory)
    if after == before:
        assert not conflict_rows.exists()
        assert not MemoryLink.objects.filter(
            project_id=candidate.project_id,
            link_type=LinkType.CONFLICTS_WITH,
        ).exists()
    else:
        conflict = conflict_rows.get()
        assert conflict.opened_transition.transition_type == 'conflict_open'
        assert conflict.semantic_link.link_type == LinkType.CONFLICTS_WITH
        assert conflict.resolved_transition_id is None


@pytest.mark.django_db
def test_conflict_open_replay_is_idempotent_and_link_delete_cannot_remove_evidence() -> None:
    candidate, source, _scope = _provenanced_candidate('conflict-replay')
    memory_result = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    compared, _source = _candidate_in_scope(candidate, source, title='Conflict replay', body='Conflict replay body')
    data = OpenMemoryConflictInput(
        request=_request_for(compared, key=f'request:{uuid.uuid4()}:conflict-open:{compared.id}:v1'),
        candidate_fence=_candidate_fence_for(compared),
        memory_fence=_transitions().build_memory_fence(memory_result.memory),
        evidence_hash='b' * 64,
        redacted_reason='replayable conflict',
    )
    first = OpenMemoryConflict().execute(data)
    replay = OpenMemoryConflict().execute(data)
    assert replay.id == first.id
    assert MemoryConflict.objects.filter(candidate=compared, memory=memory_result.memory).count() == 1
    link = MemoryLink.objects.get(memory_conflict__candidate=compared)
    with pytest.raises(ProtectedError):
        link.delete()
    assert MemoryConflict.objects.filter(id=first.id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('boundary', ('resolution', 'conflict', 'audit', 'transition'))
def test_conflict_resolution_fault_closes_all_or_none_with_one_outcome(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    first_candidate, source, _scope = _provenanced_candidate(f'conflict-resolve-{boundary}')
    first_memory = _transitions().PromoteMemoryCandidate().execute(_request(first_candidate))
    second_candidate, _second_source = _candidate_in_scope(
        first_candidate,
        source,
        title=f'Conflict second memory {boundary}',
        body=f'Conflict second memory body {boundary}',
    )
    second_memory = _transitions().PromoteMemoryCandidate().execute(_request(second_candidate))
    candidate, _conflict_source = _candidate_in_scope(
        first_candidate,
        source,
        title=f'Conflict candidate {boundary}',
        body=f'Conflict candidate body {boundary}',
    )
    for index, memory_result in enumerate((first_memory, second_memory)):
        OpenMemoryConflict().execute(
            OpenMemoryConflictInput(
                request=_request_for(candidate, key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:{index}:v1'),
                candidate_fence=_candidate_fence_for(candidate),
                memory_fence=_transitions().build_memory_fence(memory_result.memory),
                evidence_hash=f'{index + 1}' * 64,
                redacted_reason='multiple conflict evidence',
            )
        )
    open_conflicts = tuple(MemoryConflict.objects.filter(candidate=candidate).order_by('id'))
    data = ResolveMemoryConflictInput(
        request=_request_for(candidate, key=f'request:{uuid.uuid4()}:conflict-resolve:{candidate.id}:v1'),
        candidate_fence=_candidate_fence_for(candidate),
        conflict_ids=tuple(conflict.id for conflict in open_conflicts),
        conflict_memory_fences=tuple(_transitions().build_memory_fence(conflict.memory) for conflict in open_conflicts),
        resolution='reject_candidate',
    )

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedPromotionFaultError(point)

    monkeypatch.setattr(_transitions(), '_fault_boundary', fault)
    with pytest.raises(InjectedPromotionFaultError, match=boundary):
        ResolveMemoryConflict().execute(data)
    rows = list(MemoryConflict.objects.filter(id__in=data.conflict_ids))
    assert all(row.resolved_transition_id is None for row in rows) or all(
        row.resolved_transition_id is not None and row.resolution == 'reject_candidate' for row in rows
    )


@pytest.mark.django_db
def test_conflict_resolution_replay_closes_the_complete_set_once() -> None:
    candidate, source, _scope = _provenanced_candidate('conflict-resolve-replay')
    memory_result = _transitions().PromoteMemoryCandidate().execute(_request(candidate))
    compared, _compared_source = _candidate_in_scope(
        candidate,
        source,
        title='Conflict target',
        body='Conflict target body',
    )
    conflict = OpenMemoryConflict().execute(
        OpenMemoryConflictInput(
            request=_request_for(compared, key=f'request:{uuid.uuid4()}:conflict-open:{compared.id}:v1'),
            candidate_fence=_candidate_fence_for(compared),
            memory_fence=_transitions().build_memory_fence(memory_result.memory),
            evidence_hash='c' * 64,
            redacted_reason='one conflict',
        )
    )
    data = ResolveMemoryConflictInput(
        request=_request_for(compared, key=f'request:{uuid.uuid4()}:conflict-resolve:{compared.id}:v1'),
        candidate_fence=_candidate_fence_for(compared),
        conflict_ids=(conflict.id,),
        conflict_memory_fences=(_transitions().build_memory_fence(memory_result.memory),),
        resolution='reject_candidate',
    )
    first = ResolveMemoryConflict().execute(data)
    replay = ResolveMemoryConflict().execute(data)
    assert replay.duplicate is True
    assert replay.transition.id == first.transition.id
    conflict_row = MemoryConflict.objects.get(id=conflict.id)
    assert conflict_row.resolved_transition_id == first.transition.id
    assert MemoryConflict.objects.filter(resolved_transition_id=first.transition.id).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('resolution', ('publish_candidate', 'merge_candidate', 'supersede_memory', 'reject_candidate'))
def test_conflict_resolution_outcomes_are_typed_and_retain_links(resolution: str) -> None:
    candidate, conflict = _open_single_conflict(f'conflict-outcome-{resolution}')
    selected_fence = _transitions().build_memory_fence(conflict.memory)
    data = ResolveMemoryConflictInput(
        request=_request_for(candidate, key=f'request:{uuid.uuid4()}:conflict-resolve:{candidate.id}:v1'),
        candidate_fence=_candidate_fence_for(candidate),
        conflict_ids=(conflict.id,),
        conflict_memory_fences=(selected_fence,),
        resolution=resolution,
        selected_memory_fence=selected_fence if resolution in ('merge_candidate', 'supersede_memory') else None,
        title=f'Resolved {resolution}',
        body=f'Resolved body {resolution}',
    )
    result = ResolveMemoryConflict().execute(data)
    candidate.refresh_from_db()
    conflict.refresh_from_db()
    assert conflict.resolved_transition_id == result.transition.id
    assert conflict.resolution == resolution
    assert conflict.semantic_link_id is not None
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id).exists()
    if resolution == 'reject_candidate':
        assert candidate.status == CandidateStatus.REJECTED
    else:
        assert candidate.status == CandidateStatus.PROMOTED
    if resolution == 'supersede_memory':
        conflict.memory.refresh_from_db()
        assert conflict.memory.stale is True
    if resolution == 'merge_candidate':
        assert result.memory.id == conflict.memory_id


@pytest.mark.django_db
def test_foreign_scope_cannot_satisfy_or_mutate_transition_and_conflict_rows() -> None:
    target, source, _scope = _provenanced_candidate('foreign-lineage-target')
    target_result = _transitions().PromoteMemoryCandidate().execute(_request(target))
    foreign, _foreign_source, _foreign_scope = _provenanced_candidate('foreign-lineage-source')
    foreign_result = _transitions().PromoteMemoryCandidate().execute(_request(foreign))
    foreign_candidate, _source = _candidate_in_scope(
        target,
        source,
        title='Foreign candidate input',
        body='Foreign candidate input body',
    )
    request = _request_for(foreign_candidate, key=f'request:{uuid.uuid4()}:conflict-open:{foreign_candidate.id}:v1')
    request = replace(
        request,
        scope=replace(
            request.scope,
            organization_id=foreign.organization_id,
            project_id=foreign.project_id,
        ),
    )
    before_target = _lineage_shape(target.project_id)
    with pytest.raises(Exception, match='scope'):
        OpenMemoryConflict().execute(
            OpenMemoryConflictInput(
                request=request,
                candidate_fence=_candidate_fence_for(foreign_candidate),
                memory_fence=_transitions().build_memory_fence(target_result.memory),
                evidence_hash='d' * 64,
                redacted_reason='foreign scope',
            )
        )
    with pytest.raises(Exception, match='scope'):
        MergeMemories().execute(
            MergeMemoriesInput(
                request=request,
                source_memory_fence=_transitions().build_memory_fence(foreign_result.memory),
                result_memory_fence=_transitions().build_memory_fence(target_result.memory),
                title='foreign merge',
                body='foreign merge body',
            )
        )
    assert _lineage_shape(target.project_id) == before_target


@pytest.mark.django_db
def test_remaining_typed_lineage_commands_commit_named_transitions() -> None:
    candidate, source, _scope = _provenanced_candidate('typed-lineage-commands')
    promoted = _transitions().PromoteMemoryCandidate().execute(_request(candidate))

    revise_candidate, _revise_source = _candidate_in_scope(
        candidate,
        source,
        title='Revision candidate',
        body='Revision candidate body',
    )
    revised = ReviseMemoryFromCandidate().execute(
        ReviseMemoryFromCandidateInput(
            request=_request_for(
                revise_candidate,
                key=f'request:{uuid.uuid4()}:revise-candidate:{revise_candidate.id}:v1',
            ),
            candidate_fence=_candidate_fence_for(revise_candidate),
            memory_fence=_transitions().build_memory_fence(promoted.memory),
            title='Revised from candidate',
            body='Revised from candidate body',
        )
    )
    merge_candidate, _merge_source = _candidate_in_scope(
        candidate,
        source,
        title='Merge evidence candidate',
        body='Merge evidence candidate body',
    )
    merged_candidate = MergeMemoryCandidate().execute(
        MergeMemoryCandidateInput(
            request=_request_for(
                merge_candidate,
                key=f'request:{uuid.uuid4()}:merge-candidate:{merge_candidate.id}:v1',
            ),
            candidate_fence=_candidate_fence_for(merge_candidate),
            memory_fence=_transitions().build_memory_fence(revised.memory),
            title='Merged candidate result',
            body='Merged candidate result body',
        )
    )
    digest = PublishDigestMemory().execute(
        PublishDigestMemoryInput(
            request=_request_for(candidate, key=f'request:{uuid.uuid4()}:publish-digest:{candidate.id}:v1'),
            source_memory_fences=(_transitions().build_memory_fence(merged_candidate.memory),),
            title='Digest publication',
            body='Digest publication body',
            work_claim=None,
        )
    )
    state_memory = digest.memory
    for command, expected_type in (
        (MarkMemoryStale(), 'mark_stale'),
        (RefuteMemory(), 'refute'),
        (RestoreMemory(), 'restore'),
        (ArchiveMemory(), 'archive'),
    ):
        state_memory.refresh_from_db()
        result = command.execute(
            MemoryStateInput(
                request=_request_for(candidate, key=f'request:{uuid.uuid4()}:{expected_type}:{state_memory.id}:v1'),
                memory_fence=_transitions().build_memory_fence(state_memory),
            )
        )
        assert result.transition.transition_type == expected_type
        state_memory = result.memory

    first, second, first_result, second_result = _promoted_pair('typed-supersede-memories')
    superseded = SupersedeMemories().execute(
        SupersedeMemoriesInput(
            request=_request_for(first, key=f'request:{uuid.uuid4()}:supersede-memories:{first.id}:v1'),
            source_memory_fence=_transitions().build_memory_fence(first_result.memory),
            result_memory_fence=_transitions().build_memory_fence(second_result.memory),
        )
    )
    assert superseded.transition.transition_type == 'supersede'
    assert _model('MemoryTransition').objects.filter(
        project_id=candidate.project_id,
        transition_type__in=('revise', 'merge', 'publish_digest', 'mark_stale', 'refute', 'restore', 'archive'),
    ).count() >= 7
