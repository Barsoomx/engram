from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from engram.context.services import (
    RetrievalMatch,
    _pack_to_budget,
    _semantic_retrieval_matches_python,
    estimate_tokens,
    semantic_retrieval_matches,
    semantic_retrieval_matches_pgvector,
)
from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    VectorField,
)
from engram.model_policy.services import generated_embedding


@dataclass
class _MemoryStub:
    title: str
    body: str


@dataclass
class _DocumentStub:
    memory: _MemoryStub


def _make_match(title: str, body: str, score: int = 0) -> RetrievalMatch:
    return RetrievalMatch(
        document=_DocumentStub(memory=_MemoryStub(title=title, body=body)),  # type: ignore[arg-type]
        score=score,
        matched_terms=(),
        inclusion_reason='',
    )


# estimate_tokens


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens('') == 0


def test_estimate_tokens_one_char() -> None:
    assert estimate_tokens('a') == 1


def test_estimate_tokens_three_chars() -> None:
    assert estimate_tokens('abc') == 1


def test_estimate_tokens_four_chars() -> None:
    assert estimate_tokens('abcd') == 1


def test_estimate_tokens_five_chars() -> None:
    assert estimate_tokens('abcde') == 2


def test_estimate_tokens_formula() -> None:
    for length in range(0, 100):
        text = 'a' * length
        expected = (length + 3) // 4
        assert estimate_tokens(text) == expected, f'failed for length={length}'


# _pack_to_budget — None budget (item-count behavior)


def test_pack_to_budget_none_truncates_to_limit() -> None:
    matches = tuple(_make_match(f'title{i}', f'body{i}', score=10 - i) for i in range(5))
    kept, dropped = _pack_to_budget(matches, None, 3)
    assert kept == matches[:3]
    assert dropped == matches[3:]


def test_pack_to_budget_none_limit_gte_len_keeps_all() -> None:
    matches = tuple(_make_match(f'title{i}', f'body{i}') for i in range(3))
    kept, dropped = _pack_to_budget(matches, None, 10)
    assert kept == matches
    assert dropped == ()


def test_pack_to_budget_none_empty_matches() -> None:
    kept, dropped = _pack_to_budget((), None, 5)
    assert kept == ()
    assert dropped == ()


# _pack_to_budget — token budget set


def test_pack_to_budget_small_budget_trims_lower_ranked() -> None:
    top = _make_match('A', 'B', score=100)
    lower = _make_match('C' * 500, 'D' * 500, score=50)
    matches = (top, lower)
    # top block: "- [M1] A\n  B" = 12 chars = 3 tokens; budget=4 fits top but not both
    kept, dropped = _pack_to_budget(matches, 4, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert len(dropped) == 1
    assert dropped[0] is lower


def test_pack_to_budget_large_budget_keeps_all() -> None:
    matches = tuple(_make_match(f'T{i}', f'B{i}', score=10 - i) for i in range(3))
    kept, dropped = _pack_to_budget(matches, 100000, 5)
    assert kept == matches
    assert dropped == ()


def test_pack_to_budget_over_budget_top_match_is_kept() -> None:
    top = _make_match('T' * 400, 'B' * 400, score=100)
    matches = (top,)
    # block is ~200 tokens; budget=1 < cost but top match must still be kept
    kept, dropped = _pack_to_budget(matches, 1, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert dropped == ()


def test_pack_to_budget_over_budget_top_with_more_matches() -> None:
    top = _make_match('T' * 400, 'B' * 400, score=100)
    second = _make_match('S', 'S', score=50)
    matches = (top, second)
    # budget=1; top exceeds budget but is kept; second would put us over
    kept, dropped = _pack_to_budget(matches, 1, 5)
    assert len(kept) == 1
    assert kept[0] is top
    assert len(dropped) == 1
    assert dropped[0] is second


def test_pack_to_budget_limit_respected_with_large_budget() -> None:
    matches = tuple(_make_match(f'T{i}', f'B{i}', score=10 - i) for i in range(5))
    kept, dropped = _pack_to_budget(matches, 100000, 3)
    assert len(kept) == 3
    assert len(dropped) == 2
    assert kept == matches[:3]


def test_pack_to_budget_preserves_rank_order() -> None:
    m1 = _make_match('first', 'body1', score=100)
    m2 = _make_match('second', 'body2', score=50)
    m3 = _make_match('third', 'body3', score=10)
    matches = (m1, m2, m3)
    kept, dropped = _pack_to_budget(matches, 100000, 5)
    assert list(kept) == [m1, m2, m3]


def test_pack_to_budget_two_fit_three_dropped() -> None:
    # Each block: "- [M{i}] X\n  Y" is 12 chars = 3 tokens
    # budget=8 fits 2 matches (3+3=6 <= 8), 3rd would be 9 > 8
    matches = tuple(_make_match('X', 'Y', score=10 - i) for i in range(3))
    kept, dropped = _pack_to_budget(matches, 8, 5)
    assert len(kept) == 2
    assert len(dropped) == 1
    assert kept[0] is matches[0]
    assert kept[1] is matches[1]
    assert dropped[0] is matches[2]


# semantic_retrieval_matches_pgvector


pytestmark_pgvector = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def _basis_vector(index: int) -> list[float]:
    vector = [0.0] * 64
    vector[index] = 1.0

    return vector


def _blend_vector(primary: int, secondary: int, weight: float) -> list[float]:
    vector = [0.0] * 64
    vector[primary] = 1.0
    vector[secondary] = weight
    norm = math.sqrt(1.0 + weight * weight)

    return [component / norm for component in vector]


def _seed_document(
    organization: Organization,
    project: Project,
    *,
    title: str,
    body: str,
    embedding: list[float],
    sequence: int,
    with_pgvector: bool = True,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'hash-{sequence}',
    )
    document = RetrievalDocument(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text=f'{title}\n\n{body}',
        embedding_vector=embedding,
    )
    if with_pgvector and VectorField is not None:
        document.embedding_pgvector = embedding
    document.save()

    return document


@pytest.fixture
def f_scope() -> tuple[Organization, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, project


@pytestmark_pgvector
@pytest.mark.django_db
def test_pgvector_returns_matches_in_descending_similarity(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    near = _seed_document(
        organization,
        project,
        title='near',
        body='near',
        embedding=_basis_vector(0),
        sequence=1,
    )
    mid = _seed_document(
        organization,
        project,
        title='mid',
        body='mid',
        embedding=_blend_vector(0, 1, 0.75),
        sequence=2,
    )
    low = _seed_document(
        organization,
        project,
        title='low',
        body='low',
        embedding=_blend_vector(0, 1, math.sqrt(3.0)),
        sequence=3,
    )
    documents = (mid, low, near)

    matches = semantic_retrieval_matches_pgvector(documents, [], query_vector)

    assert [match.document.id for match in matches] == [near.id, mid.id, low.id]
    assert [match.inclusion_reason for match in matches] == [
        'semantic match: cosine 1.00',
        'semantic match: cosine 0.80',
        'semantic match: cosine 0.50',
    ]


@pytestmark_pgvector
@pytest.mark.django_db
def test_pgvector_excludes_documents_below_floor(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    above = _seed_document(
        organization,
        project,
        title='above',
        body='above',
        embedding=_blend_vector(0, 1, 0.75),
        sequence=1,
    )
    below = _seed_document(
        organization,
        project,
        title='below',
        body='below',
        embedding=_blend_vector(0, 1, math.sqrt(24.0)),
        sequence=2,
    )
    orthogonal = _seed_document(
        organization,
        project,
        title='orthogonal',
        body='orthogonal',
        embedding=_basis_vector(1),
        sequence=3,
    )

    matches = semantic_retrieval_matches_pgvector((above, below, orthogonal), [], query_vector)

    assert [match.document.id for match in matches] == [above.id]


@pytestmark_pgvector
@pytest.mark.django_db
def test_pgvector_excludes_already_matched(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    first = _seed_document(
        organization,
        project,
        title='first',
        body='first',
        embedding=_basis_vector(0),
        sequence=1,
    )
    second = _seed_document(
        organization,
        project,
        title='second',
        body='second',
        embedding=_blend_vector(0, 1, 0.75),
        sequence=2,
    )
    exact = [
        RetrievalMatch(document=first, score=100, matched_terms=(), inclusion_reason='exact match: first'),
    ]

    matches = semantic_retrieval_matches_pgvector((first, second), exact, query_vector)

    assert [match.document.id for match in matches] == [second.id]


@pytestmark_pgvector
@pytest.mark.django_db
def test_pgvector_preserves_tie_break_input_order(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    left = _seed_document(
        organization,
        project,
        title='left',
        body='left',
        embedding=_basis_vector(0),
        sequence=1,
    )
    right = _seed_document(
        organization,
        project,
        title='right',
        body='right',
        embedding=_basis_vector(0),
        sequence=2,
    )

    forward = semantic_retrieval_matches_pgvector((left, right), [], query_vector)
    reverse = semantic_retrieval_matches_pgvector((right, left), [], query_vector)

    assert [match.document.id for match in forward] == [left.id, right.id]
    assert [match.document.id for match in reverse] == [right.id, left.id]


@pytestmark_pgvector
@pytest.mark.django_db
def test_pgvector_and_python_paths_are_identical(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = generated_embedding('authorization before ranking protects context bundles')
    seeds = [
        ('Authorization before ranking', 'Authorization before ranking protects context bundles.'),
        ('Ranking pipeline', 'Ranking pipeline orders authorized retrieval documents deterministically.'),
        ('Token budget packing', 'Token budget packing trims lower ranked context bundle items.'),
        ('Provider secret rotation', 'Provider secret rotation narrows api key scope for tenants.'),
        ('Unrelated cooking note', 'A recipe for sourdough bread with rye flour and water.'),
    ]
    documents = tuple(
        _seed_document(
            organization,
            project,
            title=title,
            body=body,
            embedding=generated_embedding(f'{title}\n\n{body}'),
            sequence=index,
        )
        for index, (title, body) in enumerate(seeds)
    )

    pgvector_matches = semantic_retrieval_matches_pgvector(documents, [], query_vector)
    python_matches = _semantic_retrieval_matches_python(documents, [], query_vector)

    assert [match.document.id for match in pgvector_matches] == [match.document.id for match in python_matches]
    assert [match.inclusion_reason for match in pgvector_matches] == [
        match.inclusion_reason for match in python_matches
    ]
    assert [match.matched_terms for match in pgvector_matches] == [match.matched_terms for match in python_matches]


@pytestmark_pgvector
@pytest.mark.django_db
def test_dispatcher_uses_pgvector_when_column_populated(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    document = _seed_document(
        organization,
        project,
        title='near',
        body='near',
        embedding=_basis_vector(0),
        sequence=1,
        with_pgvector=True,
    )

    matches = semantic_retrieval_matches((document,), [], query_vector)

    assert [match.document.id for match in matches] == [document.id]


@pytest.mark.django_db
def test_dispatcher_falls_back_to_python_without_pgvector_column(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    document = _seed_document(
        organization,
        project,
        title='near',
        body='near',
        embedding=_basis_vector(0),
        sequence=1,
        with_pgvector=False,
    )
    assert document.embedding_pgvector is None if VectorField is not None else True

    matches = semantic_retrieval_matches((document,), [], query_vector)

    assert [match.document.id for match in matches] == [document.id]
    assert matches[0].inclusion_reason == 'semantic match: cosine 1.00'
