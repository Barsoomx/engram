from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.services import (
    BuildContextBundle,
    ContextBundleInput,
    RetrievalMatch,
    _pack_to_budget,
    _semantic_retrieval_matches_python,
    estimate_tokens,
    fuse_retrieval_legs,
    fuse_semantic_lexical,
    lexical_fusion_matches,
    lexical_recall_matches,
    lexical_retrieval_ranks,
    resolve_lexical_fusion_enabled,
    resolve_lexical_recall_enabled,
    resolve_require_provenance_enabled,
    resolve_retrieval_strategy,
    semantic_retrieval_matches,
    semantic_retrieval_matches_pgvector,
)
from engram.core.models import (
    Agent,
    AgentSession,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Observation,
    Organization,
    OrganizationSettings,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
)
from engram.model_policy.services import generated_embedding

PROVENANCE_RAW_KEY = 'egk_test_services_provenance_0123456789abcdefghijklmnopqrstuvwxyz'


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


# lexical fusion (RRF)


_FIXED_TS = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


@dataclass
class _FusionMemoryStub:
    title: str


@dataclass
class _FusionDocumentStub:
    id: uuid.UUID
    updated_at: datetime
    memory: _FusionMemoryStub


def _fusion_match(title: str, score: int = 30) -> RetrievalMatch:
    return RetrievalMatch(
        document=_FusionDocumentStub(  # type: ignore[arg-type]
            id=uuid.uuid4(),
            updated_at=_FIXED_TS,
            memory=_FusionMemoryStub(title=title),
        ),
        score=score,
        matched_terms=(),
        inclusion_reason='semantic match: cosine 0.90',
    )


def test_fuse_semantic_lexical_blends_ranks_deterministically() -> None:
    both = _fusion_match('both')
    semantic_only = _fusion_match('semantic_only')
    lexical_strong = _fusion_match('lexical_strong')
    semantic_matches = [both, semantic_only, lexical_strong]
    lexical_ranks = {both.document.id: 1, lexical_strong.document.id: 2}

    fused = fuse_semantic_lexical(semantic_matches, lexical_ranks)

    assert [match.document.id for match in fused] == [
        both.document.id,
        lexical_strong.document.id,
        semantic_only.document.id,
    ]
    assert fused[0].document.id == both.document.id


def test_fuse_semantic_lexical_empty_lexical_preserves_semantic_order() -> None:
    first = _fusion_match('first')
    second = _fusion_match('second')
    third = _fusion_match('third')
    semantic_matches = [first, second, third]

    fused = fuse_semantic_lexical(semantic_matches, {})

    assert [match.document.id for match in fused] == [match.document.id for match in semantic_matches]


def test_fuse_semantic_lexical_keeps_inclusion_reasons() -> None:
    first = _fusion_match('first')
    second = _fusion_match('second')

    fused = fuse_semantic_lexical([first, second], {second.document.id: 1})

    assert {match.inclusion_reason for match in fused} == {'semantic match: cosine 0.90'}


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_retrieval_ranks_orders_by_fts_relevance(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    strong = _seed_document(
        organization,
        project,
        title='strong',
        body='alpha alpha alpha alpha',
        embedding=_basis_vector(0),
        sequence=1,
    )
    weak = _seed_document(
        organization,
        project,
        title='weak',
        body='alpha',
        embedding=_basis_vector(1),
        sequence=2,
    )
    absent = _seed_document(
        organization,
        project,
        title='absent',
        body='gamma gamma',
        embedding=_basis_vector(2),
        sequence=3,
    )

    ranks = lexical_retrieval_ranks((strong, weak, absent), 'alpha')

    assert ranks == {strong.id: 1, weak.id: 2}


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_fusion_matches_reorders_by_combined_relevance(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    query_vector = _basis_vector(0)
    both = _seed_document(
        organization,
        project,
        title='both',
        body='alpha alpha alpha alpha',
        embedding=_basis_vector(0),
        sequence=1,
    )
    semantic_only = _seed_document(
        organization,
        project,
        title='semantic_only',
        body='gamma gamma',
        embedding=_blend_vector(0, 1, 0.75),
        sequence=2,
    )
    lexical_strong = _seed_document(
        organization,
        project,
        title='lexical_strong',
        body='alpha',
        embedding=_blend_vector(0, 1, math.sqrt(3.0)),
        sequence=3,
    )
    documents = (semantic_only, lexical_strong, both)

    semantic_matches = semantic_retrieval_matches(documents, [], query_vector)
    assert [match.document.id for match in semantic_matches] == [both.id, semantic_only.id, lexical_strong.id]

    fused = lexical_fusion_matches(semantic_matches, 'alpha')

    fused_ids = [match.document.id for match in fused]
    assert fused_ids == [both.id, lexical_strong.id, semantic_only.id]
    assert fused_ids.index(both.id) < fused_ids.index(semantic_only.id)
    assert all(match.inclusion_reason.startswith('semantic match: cosine') for match in fused)


@pytest.mark.django_db
def test_resolve_lexical_fusion_enabled_defaults_false(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope

    assert resolve_lexical_fusion_enabled(organization) is False


@pytest.mark.django_db
def test_resolve_lexical_fusion_enabled_true_when_set(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope
    OrganizationSettings.objects.create(organization=organization, lexical_fusion_enabled=True)

    assert resolve_lexical_fusion_enabled(organization) is True


# lexical recall (pg_trgm + FTS)


_FUZZY_RECALL_BODY = (
    'The retrieval pipeline resolves the effective api key scope, then ranks candidate memories '
    'and packs them into a context bundle for the agent. Tenant isolation is enforced and the '
    'authorisation gate runs before any ranking happens, so unapproved memory never reaches the model.'
)
_UNRELATED_RECALL_BODY = (
    'Boil a large pot of salted water, add the dried pasta, and stir occasionally. Cook until al dente, '
    'reserve a cup of the starchy liquid, then drain and toss the noodles with warm tomato sauce and '
    'fresh basil before serving.'
)


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_recall_matches_surfaces_trigram_near_document(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    fuzzy = _seed_document(
        organization,
        project,
        title='Access control playbook',
        body=_FUZZY_RECALL_BODY,
        embedding=_basis_vector(0),
        sequence=1,
    )
    unrelated = _seed_document(
        organization,
        project,
        title='Pasta recipe',
        body=_UNRELATED_RECALL_BODY,
        embedding=_basis_vector(1),
        sequence=2,
    )
    assert 'authorization' not in fuzzy.full_text.casefold()

    matches = lexical_recall_matches((fuzzy, unrelated), set(), 'authorization')

    assert [match.document.id for match in matches] == [fuzzy.id]
    assert matches[0].score == 20
    assert matches[0].inclusion_reason.startswith('lexical match: trigram')


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_recall_matches_excludes_already_matched(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    fuzzy = _seed_document(
        organization,
        project,
        title='authorisation',
        body='authorisation',
        embedding=_basis_vector(0),
        sequence=1,
    )

    assert lexical_recall_matches((fuzzy,), {fuzzy.id}, 'authorization') == []


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_recall_matches_empty_query_returns_empty(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    fuzzy = _seed_document(
        organization,
        project,
        title='authorisation',
        body='authorisation',
        embedding=_basis_vector(0),
        sequence=1,
    )

    assert lexical_recall_matches((fuzzy,), set(), '   ') == []


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_recall_matches_orders_by_relevance(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    strong = _seed_document(
        organization,
        project,
        title='strong',
        body='alpha alpha alpha alpha',
        embedding=_basis_vector(0),
        sequence=1,
    )
    weak = _seed_document(
        organization,
        project,
        title='weak',
        body='alpha',
        embedding=_basis_vector(1),
        sequence=2,
    )

    matches = lexical_recall_matches((weak, strong), set(), 'alpha')

    assert [match.document.id for match in matches] == [strong.id, weak.id]


@pytestmark_pgvector
@pytest.mark.django_db
def test_lexical_recall_matches_only_over_passed_documents(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    included = _seed_document(
        organization,
        project,
        title='alpha included',
        body='alpha alpha',
        embedding=_basis_vector(0),
        sequence=1,
    )
    excluded = _seed_document(
        organization,
        project,
        title='alpha excluded',
        body='alpha alpha',
        embedding=_basis_vector(1),
        sequence=2,
    )

    matches = lexical_recall_matches((included,), set(), 'alpha')

    matched_ids = {match.document.id for match in matches}
    assert included.id in matched_ids
    assert excluded.id not in matched_ids


def test_fuse_retrieval_legs_ranks_union_deterministically() -> None:
    both = _fusion_match('both')
    semantic_only = _fusion_match('semantic_only')
    lexical_only = _fusion_match('lexical_only', score=20)
    both_lexical = RetrievalMatch(
        document=both.document,
        score=20,
        matched_terms=(),
        inclusion_reason='lexical match: trigram 0.50',
    )
    semantic_matches = [both, semantic_only]
    lexical_matches = [both_lexical, lexical_only]

    fused = fuse_retrieval_legs(semantic_matches, lexical_matches)

    assert [match.document.id for match in fused] == [
        both.document.id,
        semantic_only.document.id,
        lexical_only.document.id,
    ]
    assert fused[0].score == 30
    assert lexical_only.document.id in {match.document.id for match in fused}


def test_fuse_retrieval_legs_includes_lexical_only_without_semantic() -> None:
    first = _fusion_match('first', score=20)
    second = _fusion_match('second', score=20)

    fused = fuse_retrieval_legs([], [first, second])

    assert [match.document.id for match in fused] == [first.document.id, second.document.id]


def test_resolve_retrieval_strategy_semantic_when_any_semantic_match() -> None:
    matches = [
        _fusion_match('exact', score=60),
        _fusion_match('semantic', score=30),
        _fusion_match('lexical', score=20),
    ]

    assert resolve_retrieval_strategy(matches) == 'semantic_fallback'


def test_resolve_retrieval_strategy_lexical_when_only_lexical_tail() -> None:
    matches = [_fusion_match('exact', score=60), _fusion_match('lexical', score=20)]

    assert resolve_retrieval_strategy(matches) == 'lexical_recall'


def test_resolve_retrieval_strategy_exact_without_tail() -> None:
    assert resolve_retrieval_strategy([_fusion_match('exact', score=60)]) == 'exact'
    assert resolve_retrieval_strategy([]) == 'exact'


@pytest.mark.django_db
def test_resolve_lexical_recall_enabled_defaults_false(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope

    assert resolve_lexical_recall_enabled(organization) is False


@pytest.mark.django_db
def test_resolve_lexical_recall_enabled_true_when_set(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope
    OrganizationSettings.objects.create(organization=organization, lexical_recall_enabled=True)

    assert resolve_lexical_recall_enabled(organization) is True


@pytest.mark.django_db
def test_resolve_require_provenance_enabled_defaults_false(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope

    assert resolve_require_provenance_enabled(organization) is False


@pytest.mark.django_db
def test_resolve_require_provenance_enabled_true_when_set(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope
    OrganizationSettings.objects.create(organization=organization, require_provenance=True)

    assert resolve_require_provenance_enabled(organization) is True


def _provenance_project_scope() -> tuple[Organization, Team, Project, ApiKey]:
    organization = Organization.objects.create(name='Engram Provenance', slug='engram-provenance')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    ProjectTeam.objects.create(organization=organization, team=team, project=project)
    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-context-provenance',
        display_name='Context service account',
    )
    role = Role.objects.get(code='developer')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Context key',
        key_prefix=api_key_prefix(PROVENANCE_RAW_KEY),
        key_hash=hash_api_key(PROVENANCE_RAW_KEY),
        key_fingerprint=api_key_fingerprint(PROVENANCE_RAW_KEY),
        team=team,
        project=project,
    )
    ApiKeyCapability.objects.create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:read'),
    )

    return organization, team, project, api_key


def _create_memory_document(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    title: str,
    body: str,
    file_paths: list[str],
    source_observation: Observation | None = None,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={'file_paths': file_paths},
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'{title}-hash',
        source_observation=source_observation,
    )

    return RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        file_paths=file_paths,
        full_text=f'{title}\n\n{body}',
    )


def _context_bundle_input(
    project: Project,
    team: Team,
    *,
    file_paths: tuple[str, ...],
    request_id: str,
    session_id: str,
    raw_key: str = PROVENANCE_RAW_KEY,
) -> ContextBundleInput:
    return ContextBundleInput(
        raw_key=raw_key,
        project_id=project.id,
        team_id=team.id,
        agent_runtime='codex',
        agent_version='0.1.0',
        agent_external_id='codex-local',
        session_id=session_id,
        request_id=request_id,
        correlation_id=f'correlation-{request_id}',
        trace_id=f'trace-{request_id}',
        repository_url='',
        repository_root='',
        branch='',
        cwd='',
        query='retrieval latency and provenance',
        file_paths=file_paths,
        symbols=(),
        limit=5,
        token_budget=None,
        purpose='session_start',
    )


@pytest.mark.django_db
def test_build_context_bundle_records_non_null_retrieval_latency_ms() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    _create_memory_document(
        organization,
        team,
        project,
        title='Latency memory',
        body='Retrieval latency should be measured for every bundle build.',
        file_paths=['apps/backend/engram/context/services.py'],
    )

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=('apps/backend/engram/context/services.py',),
            request_id='request-latency-1',
            session_id='session-latency-1',
        ),
    )

    assert result.bundle.retrieval_latency_ms is not None
    assert result.bundle.retrieval_latency_ms >= 0


@pytest.mark.django_db
def test_build_context_bundle_excludes_unprovenanced_memory_when_required() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    OrganizationSettings.objects.create(organization=organization, require_provenance=True)
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-provenance')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-provenance-source',
        runtime='codex',
    )
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title='Provenance source',
        content_hash='observation-provenance-hash',
    )
    _create_memory_document(
        organization,
        team,
        project,
        title='Provenanced memory',
        body='Provenanced memory has a source observation.',
        file_paths=['apps/backend/engram/context/services.py'],
        source_observation=observation,
    )
    _create_memory_document(
        organization,
        team,
        project,
        title='Unprovenanced memory',
        body='Unprovenanced memory has no source observation.',
        file_paths=['apps/backend/engram/context/views.py'],
    )

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=(
                'apps/backend/engram/context/services.py',
                'apps/backend/engram/context/views.py',
            ),
            request_id='request-provenance-required-1',
            session_id='session-provenance-required-1',
        ),
    )

    titles = {match.document.memory.title for match in result.matches}
    assert titles == {'Provenanced memory'}


@pytest.mark.django_db
def test_build_context_bundle_includes_unprovenanced_memory_when_not_required() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    _create_memory_document(
        organization,
        team,
        project,
        title='Provenanced memory',
        body='Provenanced memory has a source observation.',
        file_paths=['apps/backend/engram/context/services.py'],
    )
    _create_memory_document(
        organization,
        team,
        project,
        title='Unprovenanced memory',
        body='Unprovenanced memory has no source observation.',
        file_paths=['apps/backend/engram/context/views.py'],
    )

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=(
                'apps/backend/engram/context/services.py',
                'apps/backend/engram/context/views.py',
            ),
            request_id='request-provenance-not-required-1',
            session_id='session-provenance-not-required-1',
        ),
    )

    titles = {match.document.memory.title for match in result.matches}
    assert titles == {'Provenanced memory', 'Unprovenanced memory'}
