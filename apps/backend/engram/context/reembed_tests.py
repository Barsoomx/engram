from __future__ import annotations

import pytest

from engram.celeryconfig import beat_schedule
from engram.context.context_api_tests import create_embedding_policy, create_project_scope
from engram.context.services import ReembedMissingEmbeddings
from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
)

pytestmark = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def create_unembedded_document(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    sequence: int,
    stale: bool = False,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=f'Reembed target {sequence}',
        body='Durable body for reembedding.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        stale=stale,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash=f'reembed-{sequence}',
    )

    return RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=memory.visibility_scope,
        full_text=f'{memory.title}\n\n{memory.body}',
        stale=stale,
    )


@pytest.mark.django_db
def test_reembed_fills_missing_embedding() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    document = create_unembedded_document(organization, team, project, sequence=1)
    assert document.embedding_pgvector is None

    result = ReembedMissingEmbeddings().execute()

    document.refresh_from_db()
    assert result.embedded == 1
    assert result.failed == 0
    assert document.embedding_pgvector is not None
    assert document.embedding_reference.startswith('provider:')


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
def test_reembed_without_policy_counts_failed_without_raising() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    document = create_unembedded_document(organization, team, project, sequence=3)

    result = ReembedMissingEmbeddings().execute()

    document.refresh_from_db()
    assert result.failed == 1
    assert result.embedded == 0
    assert document.embedding_pgvector is None


@pytest.mark.django_db
def test_reembed_is_idempotent_once_embedded() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_embedding_policy(organization, team, project)
    create_unembedded_document(organization, team, project, sequence=4)

    first = ReembedMissingEmbeddings().execute()
    second = ReembedMissingEmbeddings().execute()

    assert first.embedded == 1
    assert second.scanned == 0


def test_reembed_beat_schedule_is_registered() -> None:
    assert 'reembed-missing-embeddings' in beat_schedule
    entry = beat_schedule['reembed-missing-embeddings']
    assert entry['task'] == 'engram.memory.reembed_missing_embeddings'
