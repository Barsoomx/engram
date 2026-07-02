from __future__ import annotations

import pytest
from django.core.management import call_command

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    VectorField,
)
from engram.model_policy.services import EMBEDDING_DIMENSION

pytestmark = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def _make_document(
    organization: Organization,
    project: Project,
    *,
    sequence: int,
    embedding_vector: list[float],
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=f'memory-{sequence}',
        body='body',
        status=MemoryStatus.APPROVED,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body='body',
        content_hash=f'hash-{sequence}',
    )
    document = RetrievalDocument(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text='text',
        embedding_vector=embedding_vector,
    )
    document.save()

    return document


@pytest.fixture
def f_scope() -> tuple[Organization, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, project


@pytest.mark.django_db
def test_backfill_copies_embedding_vector_into_pgvector(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    embedding = [round(0.0001 * index, 6) for index in range(EMBEDDING_DIMENSION)]
    document = _make_document(organization, project, sequence=1, embedding_vector=embedding)
    assert document.embedding_pgvector is None

    call_command('engram_backfill_pgvector_embeddings')

    document.refresh_from_db()
    assert document.embedding_pgvector is not None
    assert list(document.embedding_pgvector) == pytest.approx(embedding, abs=1e-5)


@pytest.mark.django_db
def test_backfill_skips_wrong_dimension_vectors(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    stale = _make_document(organization, project, sequence=3, embedding_vector=[0.1] * 64)
    valid = _make_document(
        organization,
        project,
        sequence=4,
        embedding_vector=[0.2] * EMBEDDING_DIMENSION,
    )

    call_command('engram_backfill_pgvector_embeddings')

    stale.refresh_from_db()
    valid.refresh_from_db()
    assert stale.embedding_pgvector is None
    assert valid.embedding_pgvector is not None


@pytest.mark.django_db
def test_backfill_skips_documents_without_embedding_vector(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    document = _make_document(organization, project, sequence=1, embedding_vector=[])

    call_command('engram_backfill_pgvector_embeddings')

    document.refresh_from_db()
    assert document.embedding_pgvector is None


@pytest.mark.django_db
def test_backfill_is_idempotent(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    embedding = [round(0.0002 * index, 6) for index in range(EMBEDDING_DIMENSION)]
    document = _make_document(organization, project, sequence=1, embedding_vector=embedding)

    call_command('engram_backfill_pgvector_embeddings')
    document.refresh_from_db()
    first = list(document.embedding_pgvector)

    call_command('engram_backfill_pgvector_embeddings')
    document.refresh_from_db()
    second = list(document.embedding_pgvector)

    assert first == pytest.approx(second)


@pytest.mark.django_db
def test_backfill_does_not_overwrite_existing_pgvector(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    embedding = [round(0.0003 * index, 6) for index in range(EMBEDDING_DIMENSION)]
    preset = [round(0.0004 * index, 6) for index in range(EMBEDDING_DIMENSION)]
    document = _make_document(organization, project, sequence=1, embedding_vector=embedding)
    document.embedding_pgvector = preset
    document.save(update_fields=['embedding_pgvector'])

    call_command('engram_backfill_pgvector_embeddings')

    document.refresh_from_db()
    assert list(document.embedding_pgvector) == pytest.approx(preset, abs=1e-5)
