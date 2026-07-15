from __future__ import annotations

import importlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    Observation,
    RetrievalDocument,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked, evidence_manifest
from engram.memory.distillation_provenance import (
    candidate_source_anchors,
    canonical_source_manifest,
    session_candidate_content_hash,
)
from engram.memory.distillation_window import materialize_distillation_window
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import ended_session_work
from engram.memory.work_execution import StaleWorkFenceError, claim_work
from engram.memory.workflow_work import observation_content_digest


class InjectedPromotionFaultError(RuntimeError):
    pass


def _transitions() -> Any:
    return importlib.import_module('engram.memory.transitions')


def _model(name: str) -> Any:
    from engram.core import models

    return getattr(models, name)


def _provenanced_candidate(suffix: str = 'promotion') -> tuple[MemoryCandidate, Any, Any]:
    organization, project, session = create_scope(suffix)
    work = ended_session_work((organization, project, session))
    window = materialize_distillation_window(work)
    from engram.memory.invariant_queries_tests import _make_stage_history

    stage, _primary = _make_stage_history((organization, project, session), window)
    observation = Observation.objects.get(session=session, session_sequence=1)
    title = f'Promotion candidate {suffix}'
    body = f'Promotion body {suffix}'
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        source_observation=observation,
        title=title,
        body=body,
        status=CandidateStatus.PROPOSED,
        visibility_scope=VisibilityScope.PROJECT,
        evidence=[{'observation_id': str(observation.id)}],
        content_hash=session_candidate_content_hash(session.id, title, body),
        confidence=Decimal('0.900'),
        decision_work_contract_version=1,
    )
    anchors = candidate_source_anchors(
        observation,
        observation_id=str(observation.id),
        session_sequence=observation.session_sequence,
        observation_digest=observation_content_digest(observation),
    )
    source = MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        candidate=candidate,
        window=window,
        observation=observation,
        stage=stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )
    return candidate, source, (organization, project, session)


def _request(candidate: MemoryCandidate, *, key: str | None = None, reason: str = 'test promotion') -> Any:
    transitions = _transitions()
    scope = transitions.TransitionScope(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
    )
    request = transitions.TransitionRequest(
        scope=scope,
        idempotency_key=key or f'candidate:{candidate.id}:settle:v1',
        actor_type='test',
        actor_id='promotion-tests',
        capability='memories:write',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason=reason,
        origin='promotion-tests',
    )
    _entries, manifest_hash = evidence_manifest(candidate)
    fence = transitions.CandidateFence(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        evidence_manifest_hash=manifest_hash,
    )
    return transitions.PromoteMemoryCandidateInput(request=request, candidate_fence=fence)


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
