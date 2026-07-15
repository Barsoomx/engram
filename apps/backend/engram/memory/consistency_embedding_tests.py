from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    RetrievalDocument,
    VectorField,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.consistency import (
    ConsistencyReportInput,
    MemoryConsistencyReporter,
    RebuildMemoryProjections,
    RebuildProjectionInput,
)
from engram.memory.projections import complete_embedding_projection, create_embedding_work_and_signal
from engram.memory.transitions import PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate, transition_request
from engram.memory.work_execution import claim_work

pytestmark = pytest.mark.django_db

_EMBEDDING_TASK = 'engram.memory.embed_memory_projection_work_v1'


def _report_input(organization_id: uuid.UUID, project_id: uuid.UUID) -> ConsistencyReportInput:
    return ConsistencyReportInput(
        organization_id=organization_id,
        project_id=project_id,
        as_of=timezone.now(),
        after_id=None,
        sample_limit=20,
    )


def _rebuild_input(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    apply: bool,
) -> RebuildProjectionInput:
    return RebuildProjectionInput(
        organization_id=organization_id,
        project_id=project_id,
        as_of=timezone.now(),
        kind='embedding',
        apply=apply,
        after_id=None,
        batch_size=200,
    )


def _promoted_embedding_fixture(suffix: str) -> tuple[RetrievalDocument, WorkflowWork]:
    candidate, _source, _scope = provenanced_candidate(suffix)
    result = PromoteMemoryCandidate().execute(transition_request(candidate))
    assert result.embedding_work is not None
    return result.retrieval_document, result.embedding_work


def _missing_embedding_fixture(suffix: str) -> RetrievalDocument:
    document, old_work = _promoted_embedding_fixture(suffix)
    WorkflowWork.objects.filter(id=old_work.id).update(
        input_fingerprint='0' * 64,
        input_snapshot={**old_work.input_snapshot, 'exact_projection_hash': '0' * 64},
        disposition=WorkflowWorkDisposition.COMPLETE,
        resolution_reason=WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED,
        resolved_at=timezone.now(),
        execution_state=WorkflowWorkExecutionState.SETTLED,
    )
    CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).delete()
    return document


def _vector(value: float) -> list[float]:
    return [value, 1.0 - value, *([0.0] * 1534)]


@pytest.mark.transactional
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_concurrent_embedding_reconciliation_converges_on_one_work_and_signal() -> None:
    document = _missing_embedding_fixture('consistency-concurrent')
    data = _rebuild_input(document.organization_id, document.project_id, apply=True)
    barrier = threading.Barrier(2)

    def reconcile() -> tuple[object, Exception | None]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return RebuildMemoryProjections().execute(data), None
        except Exception as error:  # pragma: no cover - surfaced below
            return None, error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: reconcile(), (1, 2)))

    errors = [error for _result, error in results if error is not None]
    assert errors == []
    assert (
        WorkflowWork.objects.filter(
            project_id=document.project_id,
            work_type=WorkflowWorkType.MEMORY_EMBEDDING,
            subject_id=document.id,
            input_snapshot__exact_projection_hash=document.exact_projection_hash,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count() == 1


@pytest.mark.transactional
@pytest.mark.django_db(transaction=True)
def test_late_old_hash_result_supersedes_only_old_work() -> None:
    document, old_work = _promoted_embedding_fixture('consistency-late-old-hash')
    old_hash = document.exact_projection_hash
    new_hash = 'c' * 64
    document.exact_projection_hash = new_hash
    document.embedding_reference = ''
    document.embedding_vector = []
    document.embedding_projection_hash = ''
    document.embedding_projected_at = None
    document.save(
        update_fields=[
            'exact_projection_hash',
            'embedding_reference',
            'embedding_vector',
            'embedding_projection_hash',
            'embedding_projected_at',
            'updated_at',
        ]
    )
    with transaction.atomic():
        new_work, created = create_embedding_work_and_signal(document=document)
    assert created is True
    assert new_work.id != old_work.id

    claim_result = claim_work(
        work_id=old_work.id,
        expected_work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        lease_owner='embedding:late-old-hash',
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert claim_result.claim is not None
    assert (
        complete_embedding_projection(
            claim=claim_result.claim,
            expected_projection_hash=old_hash,
            embedding=_vector(0.9),
            provider_call_id=uuid.uuid4(),
            now=datetime(2026, 7, 15, 12, 1, tzinfo=UTC),
        )
        is None
    )

    document.refresh_from_db()
    old_work.refresh_from_db()
    new_work.refresh_from_db()
    assert document.embedding_vector == []
    assert old_work.disposition == WorkflowWorkDisposition.COMPLETE
    assert old_work.resolution_reason == WorkflowWorkResolutionReason.PROJECTION_SUPERSEDED
    assert new_work.disposition == WorkflowWorkDisposition.REQUIRED
    assert WorkflowWork.objects.filter(subject_id=document.id, work_type=WorkflowWorkType.MEMORY_EMBEDDING).count() == 2


@pytest.mark.transactional
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(VectorField is None, reason='pgvector not installed')
def test_embedding_json_pgvector_mismatch_is_reported_and_repaired_once() -> None:
    document, work = _promoted_embedding_fixture('consistency-vector-mismatch')
    document.embedding_reference = 'provider:stale'
    document.embedding_vector = _vector(0.1)
    document.embedding_pgvector = _vector(0.2)
    document.embedding_projection_hash = document.exact_projection_hash
    document.embedding_projected_at = timezone.now()
    document.save(
        update_fields=[
            'embedding_reference',
            'embedding_vector',
            'embedding_pgvector',
            'embedding_projection_hash',
            'embedding_projected_at',
            'updated_at',
        ]
    )

    report = MemoryConsistencyReporter().execute(_report_input(document.organization_id, document.project_id))
    issue = next(issue for issue in report.issues if issue.memory_id == document.memory_id)
    assert issue.code == 'embedding_projection_stale'
    assert issue.classification == 'enqueue_embedding'

    current_work = WorkflowWork.objects.filter(subject_id=document.id, work_type=WorkflowWorkType.MEMORY_EMBEDDING)
    before_work_count = current_work.count()
    before_signal_count = CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count()
    repaired = RebuildMemoryProjections().execute(
        _rebuild_input(document.organization_id, document.project_id, apply=True)
    )
    assert repaired.changed == 0
    assert repaired.skipped == 1
    assert current_work.count() == before_work_count == 1
    assert CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count() == before_signal_count == 1

    claim_result = claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        lease_owner='embedding:consistency-repair',
        now=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
        lease_for=timedelta(minutes=5),
    )
    assert claim_result.claim is not None
    assert (
        complete_embedding_projection(
            claim=claim_result.claim,
            expected_projection_hash=document.exact_projection_hash,
            embedding=_vector(0.3),
            provider_call_id=uuid.uuid4(),
            now=datetime(2026, 7, 15, 13, 1, tzinfo=UTC),
        )
        is not None
    )

    report = MemoryConsistencyReporter().execute(_report_input(document.organization_id, document.project_id))
    assert report.issues == ()
    rerun = RebuildMemoryProjections().execute(
        _rebuild_input(document.organization_id, document.project_id, apply=True)
    )
    assert rerun.changed == 0
    assert rerun.skipped == 0
    assert WorkflowWork.objects.filter(subject_id=document.id, work_type=WorkflowWorkType.MEMORY_EMBEDDING).count() == 1
    assert CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count() == 1


@pytest.mark.transactional
@pytest.mark.django_db(transaction=True)
def test_embedding_rebuild_missing_work_creates_one_work_and_signal() -> None:
    document = _missing_embedding_fixture('consistency-missing-work')
    assert (
        WorkflowWork.objects.filter(
            subject_id=document.id,
            work_type=WorkflowWorkType.MEMORY_EMBEDDING,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count() == 0

    repaired = RebuildMemoryProjections().execute(
        _rebuild_input(document.organization_id, document.project_id, apply=True)
    )

    assert repaired.changed == 1
    assert repaired.skipped == 0
    assert (
        WorkflowWork.objects.filter(
            subject_id=document.id,
            work_type=WorkflowWorkType.MEMORY_EMBEDDING,
            input_snapshot__exact_projection_hash=document.exact_projection_hash,
        ).count()
        == 1
    )
    assert CeleryOutbox.objects.filter(task_name=_EMBEDDING_TASK).count() == 1
