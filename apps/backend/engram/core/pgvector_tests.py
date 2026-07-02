import importlib.util

import pytest

from engram.context.services import cosine_similarity
from engram.core.models import (
    Memory,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    VectorField,
)
from engram.model_policy.services import EMBEDDING_DIMENSION


def _has_pgvector() -> bool:
    return importlib.util.find_spec('pgvector') is not None


def test_cosine_similarity_unchanged_for_jsonfield_path() -> None:
    left = [1.0, 0.0, 0.0]
    right = [1.0, 0.0, 0.0]

    assert cosine_similarity(left, right) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    left = [1.0, 0.0]
    right = [0.0, 1.0]

    assert cosine_similarity(left, right) == pytest.approx(0.0)


def test_embedding_pgvector_field_optional_when_pgvector_missing() -> None:
    if _has_pgvector():
        field = RetrievalDocument._meta.get_field('embedding_pgvector')
        assert field is not None
        assert field.null is True
        assert field.blank is True

        return

    assert VectorField is None
    field_names = {f.name for f in RetrievalDocument._meta.get_fields()}
    assert 'embedding_pgvector' not in field_names


@pytest.mark.django_db
def test_retrieval_document_resave_with_populated_pgvector_does_not_raise() -> None:
    if not _has_pgvector():
        pytest.skip('pgvector not installed')

    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    memory = Memory.objects.create(organization=organization, project=project, title='Memory', body='body')
    version = MemoryVersion.objects.create(
        organization=organization, project=project, memory=memory, version=1, body='body', content_hash='hash'
    )
    embedding = [round(0.0001 * index, 6) for index in range(EMBEDDING_DIMENSION)]
    document = RetrievalDocument(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text='text',
        embedding_vector=embedding,
        embedding_pgvector=embedding,
    )
    document.save()

    refreshed = RetrievalDocument.objects.get(id=document.id)
    refreshed.full_text = 'updated'
    refreshed.save()

    assert RetrievalDocument.objects.get(id=document.id).full_text == 'updated'


@pytest.mark.django_db
def test_retrieval_document_stores_embedding_vector_via_jsonfield() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    memory = Memory.objects.create(organization=organization, project=project, title='Memory', body='body')
    version = MemoryVersion.objects.create(
        organization=organization, project=project, memory=memory, version=1, body='body', content_hash='hash'
    )
    document = RetrievalDocument(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text='text',
        embedding_vector=[0.1, 0.2, 0.3],
    )

    document.full_clean()
    document.save()

    refreshed = RetrievalDocument.objects.get(id=document.id)
    assert refreshed.embedding_vector == [0.1, 0.2, 0.3]
