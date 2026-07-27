from __future__ import annotations

import pytest

from engram.celeryconfig import beat_schedule
from engram.context.context_api_tests import create_embedding_policy, create_project_scope
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput, ReembedMissingEmbeddings
from engram.core.models import (
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.transitions import PromoteMemoryCandidate
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request

pytestmark = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def create_unembedded_document(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    sequence: int,
    stale: bool = False,
    projection_contract_version: int | None = None,
) -> RetrievalDocument:
    candidate, _source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix=f'reembed-{sequence}',
        title=f'Reembed target {sequence}',
        body='Durable body for reembedding.',
        visibility_scope=VisibilityScope.PROJECT,
    )
    result = PromoteMemoryCandidate().execute(transition_request(candidate))
    memory = result.memory
    document = result.retrieval_document
    if projection_contract_version is None:
        memory.metadata = {'exact_terms': [f'reembed-{sequence}']}
        memory.save(update_fields=['metadata', 'updated_at'])
        document = (
            IndexMemoryVersion()
            .execute(
                IndexMemoryVersionInput(memory_version_id=result.memory_version.id, defer_embedding=True),
            )
            .retrieval_document
        )
    if stale:
        memory.stale = True
        memory.save(update_fields=['stale', 'updated_at'])
        document.stale = True
        document.save(update_fields=['stale', 'updated_at'])
    return document


@pytest.mark.django_db
def test_reembed_schedules_missing_embedding_work() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    document = create_unembedded_document(organization, team, project, sequence=1)
    assert document.embedding_pgvector is None

    result = ReembedMissingEmbeddings().execute()

    document.refresh_from_db()
    assert result.embedded == 1
    assert result.failed == 0
    assert document.embedding_pgvector is None
    assert WorkflowWork.objects.filter(subject_id=document.id, work_type=WorkflowWorkType.MEMORY_EMBEDDING).count() == 2


@pytest.mark.django_db
def test_reembed_skips_stale_documents() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    document = create_unembedded_document(organization, team, project, sequence=2, stale=True)

    result = ReembedMissingEmbeddings().execute()

    document.refresh_from_db()
    assert result.scanned == 0
    assert document.embedding_pgvector is None


@pytest.mark.django_db
def test_reembed_without_policy_still_schedules_embedding_work() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    document = create_unembedded_document(organization, team, project, sequence=3)

    result = ReembedMissingEmbeddings().execute()

    document.refresh_from_db()
    assert result.failed == 0
    assert result.embedded == 1
    assert document.embedding_pgvector is None


@pytest.mark.django_db
def test_reembed_does_not_duplicate_async_embedding_work() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    create_unembedded_document(organization, team, project, sequence=4)

    first = ReembedMissingEmbeddings().execute()
    second = ReembedMissingEmbeddings().execute()

    assert first.embedded == 1
    assert second.embedded == 0
    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.MEMORY_EMBEDDING).count() == 2


@pytest.mark.django_db
def test_reembed_v1_documents_use_existing_async_work() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    create_unembedded_document(
        organization,
        team,
        project,
        sequence=5,
        projection_contract_version=1,
    )

    result = ReembedMissingEmbeddings().execute()

    assert result.scanned == 1
    assert result.embedded == 0
    assert result.failed == 1


def test_reembed_beat_schedule_is_registered() -> None:
    assert 'reembed-missing-embeddings' in beat_schedule
    entry = beat_schedule['reembed-missing-embeddings']
    assert entry['task'] == 'engram.memory.reembed_missing_embeddings'


# ---------------------------------------------------------------------------
# 2026-07-27 - create_work() requires an active transaction, and the beat task
# runs in autocommit, so in production this died on the first document. The
# suite always runs inside pytest-django's ambient atomic block, which hid it;
# assert instead that the service opens its own atomic block (savepoint depth
# grows across the call) rather than relying on an ambient one.
# See docs/superpowers/specs/2026-07-27-curation-outage-resilience-design.md
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reembed_opens_its_own_transaction_per_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.db import connection

    from engram.context import services as services_module

    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    create_unembedded_document(organization, team, project, sequence=11)
    real = services_module.create_embedding_work_and_signal
    depths: list[int] = []

    def recording(*, document: RetrievalDocument) -> object:
        depths.append(len(connection.savepoint_ids))

        return real(document=document)

    monkeypatch.setattr(services_module, 'create_embedding_work_and_signal', recording)
    outer_depth = len(connection.savepoint_ids)

    result = ReembedMissingEmbeddings().execute()

    assert result.scanned == 1
    assert depths == [outer_depth + 1]
