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
