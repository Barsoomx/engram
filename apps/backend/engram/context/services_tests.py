from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import AccessDeniedError, EffectiveScope, api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.services import (
    BuildContextBundle,
    ContextBundleInput,
    IndexMemoryVersion,
    IndexMemoryVersionInput,
    RetrievalMatch,
    _pack_to_budget,
    _render_annotation,
    _semantic_retrieval_matches_python,
    authorized_retrieval_documents,
    contains_match_query_terms,
    derive_retrieval_terms,
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
    score_retrieval_document,
    semantic_retrieval_matches,
    semantic_retrieval_matches_pgvector,
)
from engram.core.models import (
    Agent,
    AgentSession,
    ContextBundle,
    ContextBundleStatus,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Observation,
    Organization,
    OrganizationSettings,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
)
from engram.model_policy.models import ProviderCallRecord
from engram.model_policy.services import EMBEDDING_DIMENSION, generated_embedding

PROVENANCE_RAW_KEY = 'egk_test_services_provenance_0123456789abcdefghijklmnopqrstuvwxyz'


@dataclass
class _MemoryStub:
    title: str
    body: str
    kind: str = ''
    confidence: Decimal | None = None


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


# _render_annotation


def test_render_annotation_kind_and_confidence() -> None:
    assert _render_annotation('gotcha', Decimal('0.950')) == ' (gotcha, confidence 0.950)'


def test_render_annotation_kind_only() -> None:
    assert _render_annotation('gotcha', None) == ' (gotcha)'


def test_render_annotation_confidence_only() -> None:
    assert _render_annotation('', Decimal('0.950')) == ' (confidence 0.950)'


def test_render_annotation_neither() -> None:
    assert _render_annotation('', None) == ''


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
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0

    return vector


def _blend_vector(primary: int, secondary: int, weight: float) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
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


# authorized_retrieval_documents — deferred embedding columns


def _effective_scope(organization: Organization) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(),
        capabilities=('search:query',),
        actor_type='api_key',
        actor_id='key',
        project_bound=False,
    )


@pytest.mark.django_db
def test_authorized_documents_defer_embedding_columns(f_scope: tuple[Organization, Project]) -> None:
    organization, project = f_scope
    _seed_document(organization, project, title='doc', body='doc', embedding=_basis_vector(0), sequence=1)

    documents = authorized_retrieval_documents(organization, project, _effective_scope(organization))

    deferred = documents[0].get_deferred_fields()
    assert 'embedding_vector' in deferred
    if VectorField is not None:
        assert 'embedding_pgvector' in deferred


@pytest.mark.django_db
def test_authorized_documents_include_embeddings_loads_columns(f_scope: tuple[Organization, Project]) -> None:
    organization, project = f_scope
    _seed_document(organization, project, title='doc', body='doc', embedding=_basis_vector(0), sequence=1)

    documents = authorized_retrieval_documents(
        organization,
        project,
        _effective_scope(organization),
        include_embeddings=True,
    )

    assert documents[0].get_deferred_fields() == set()


@pytest.mark.django_db
def test_python_semantic_bulk_loads_deferred_vectors(f_scope: tuple[Organization, Project]) -> None:
    organization, project = f_scope
    for index in range(3):
        _seed_document(
            organization,
            project,
            title=f'doc-{index}',
            body='doc',
            embedding=_basis_vector(0),
            sequence=index,
            with_pgvector=False,
        )
    documents = authorized_retrieval_documents(organization, project, _effective_scope(organization))

    with CaptureQueriesContext(connection) as captured:
        matches = _semantic_retrieval_matches_python(documents, [], _basis_vector(0))

    assert len(matches) == 3
    assert len(captured.captured_queries) == 1


@pytestmark_pgvector
@pytest.mark.django_db
def test_semantic_matches_work_on_deferred_documents(f_scope: tuple[Organization, Project]) -> None:
    organization, project = f_scope
    near = _seed_document(organization, project, title='near', body='near', embedding=_basis_vector(0), sequence=1)
    _seed_document(organization, project, title='far', body='far', embedding=_basis_vector(1), sequence=2)
    documents = authorized_retrieval_documents(organization, project, _effective_scope(organization))

    matches = semantic_retrieval_matches(documents, [], _basis_vector(0))

    assert [match.document.id for match in matches] == [near.id]
    assert matches[0].inclusion_reason == 'semantic match: cosine 1.00'


# IndexMemoryVersion — extracted symbols/exact_terms


def test_derive_retrieval_terms_merges_metadata_and_extracted_values() -> None:
    symbols, exact_terms = derive_retrieval_terms(
        {'symbols': ['legacy_symbol'], 'exact_terms': ['LEGACY-1']},
        'Scope resolver gotcha',
        '`resolve_scope()` raises AccessDeniedError when ENGRAM_MODE is unset.',
    )

    assert 'legacy_symbol' in symbols
    assert 'resolve_scope' in symbols
    assert 'legacy-1' in exact_terms
    assert 'accessdeniederror' in exact_terms


def test_derive_retrieval_terms_defaults_missing_metadata_to_empty_lists() -> None:
    symbols, exact_terms = derive_retrieval_terms({}, 'Plain title', 'plain body without markers')

    assert symbols == []
    assert exact_terms == ['plain title']


@pytest.mark.django_db
def test_index_memory_version_merges_extracted_symbols_and_exact_terms(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title='Scope resolver gotcha',
        body='`resolve_scope()` raises AccessDeniedError when ENGRAM_MODE is unset.',
        status=MemoryStatus.APPROVED,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='hash-resolve-scope',
    )

    result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

    document = result.retrieval_document
    assert 'resolve_scope' in document.symbols
    assert 'accessdeniederror' in document.exact_terms
    match = score_retrieval_document(
        document,
        query='',
        file_paths=(),
        symbols=('resolve_scope',),
        has_request_terms=True,
    )
    assert match is not None
    assert match.score == 80


# score_retrieval_document — contains-tier term filtering (D7)


@dataclass
class _ScoreDocumentStub:
    file_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = ()
    full_text: str = ''


def test_contains_match_query_terms_drops_short_tokens_keeps_whole_query() -> None:
    terms = contains_match_query_terms('please fix the config')

    assert terms == ('please fix the config', 'please', 'config')


def test_score_retrieval_document_short_token_does_not_produce_contains_match() -> None:
    document = _ScoreDocumentStub(exact_terms=('prefix',))

    match = score_retrieval_document(
        document,  # type: ignore[arg-type]
        query='please fix the config',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
    )

    assert match is None


def test_score_retrieval_document_distinctive_token_still_produces_contains_match() -> None:
    document = _ScoreDocumentStub(exact_terms=('config',))

    match = score_retrieval_document(
        document,  # type: ignore[arg-type]
        query='please fix the config',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
    )

    assert match is not None
    assert match.score == 60
    assert match.matched_terms == ('config',)


def test_score_retrieval_document_whole_query_term_matches_contained_document_term() -> None:
    document = _ScoreDocumentStub(exact_terms=('fix session cookie bug',))

    match = score_retrieval_document(
        document,  # type: ignore[arg-type]
        query='please fix session cookie bug asap',
        file_paths=(),
        symbols=(),
        has_request_terms=True,
    )

    assert match is not None
    assert match.score == 60
    assert match.matched_terms == ('fix session cookie bug',)


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


def _create_project_scoped_context_key(
    organization: Organization,
    project: Project,
    raw_key: str,
) -> str:
    owner = Identity.objects.get(organization=organization, external_id='svc-context-provenance')
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Project context key',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        project=project,
    )
    ApiKeyCapability.objects.create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:read'),
    )

    return raw_key


@pytest.mark.django_db
def test_get_or_create_session_initializes_observation_cursor_for_new_session() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-context-session')
    data = _context_bundle_input(
        project,
        team,
        file_paths=(),
        request_id='request-session-new',
        session_id='session-new',
    )

    session = BuildContextBundle()._get_or_create_session(organization, project, team, agent, data)

    assert session.observation_sequence_cursor == 0
    assert AgentSession.objects.get(id=session.id).observation_sequence_cursor == 0


@pytest.mark.django_db
def test_get_or_create_session_adopts_legacy_null_team_once() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-context-legacy')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=None,
        agent=agent,
        external_session_id='session-legacy',
        runtime='codex',
    )
    data = _context_bundle_input(
        project,
        team,
        file_paths=(),
        request_id='request-session-legacy',
        session_id=session.external_session_id,
    )

    result = BuildContextBundle()._get_or_create_session(organization, project, team, agent, data)

    assert result.team_id == team.id
    assert AgentSession.objects.get(id=session.id).team_id == team.id


@pytest.mark.django_db
def test_build_context_bundle_rejects_legacy_null_team_session_with_history_without_effects() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='codex-local',
        version='old-version',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=None,
        agent=agent,
        external_session_id='session-legacy-history',
        runtime='codex',
        platform_source='original-platform',
        repository_url='https://original.example/repo',
        repository_root='/original/root',
        branch='original-branch',
        cwd='/original/cwd',
        observation_sequence_cursor=1,
    )
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=None,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        payload={'tool_name': 'bash'},
        source_adapter='codex',
        client_event_id='legacy-history-event',
        idempotency_key='legacy-history-idempotency',
        content_hash='legacy-history-hash',
        runtime='codex',
        sequence_number=1,
        normalization_contract_version=0,
    )
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=None,
        agent=agent,
        session=session,
        raw_event=raw_event,
        observation_type='tool_use',
        title='Legacy history observation',
        content_hash=raw_event.content_hash,
        session_sequence=1,
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-session-legacy-history',
            session_id=session.external_session_id,
        ),
        agent_version='new-version',
    )
    initial_entity_state = {
        'agent': Agent.objects.filter(id=agent.id).values().get(),
        'session': AgentSession.objects.filter(id=session.id).values().get(),
        'raw_event': RawEventEnvelope.objects.filter(id=raw_event.id).values().get(),
        'observation': Observation.objects.filter(id=observation.id).values().get(),
    }
    bundle_count = ContextBundle.objects.count()
    provider_call_count = ProviderCallRecord.objects.count()
    retrieval_document_count = RetrievalDocument.objects.count()

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle().execute(data)

    assert exc_info.value.code == 'team_scope_denied'
    assert {
        'agent': Agent.objects.filter(id=agent.id).values().get(),
        'session': AgentSession.objects.filter(id=session.id).values().get(),
        'raw_event': RawEventEnvelope.objects.filter(id=raw_event.id).values().get(),
        'observation': Observation.objects.filter(id=observation.id).values().get(),
    } == initial_entity_state
    assert ContextBundle.objects.count() == bundle_count
    assert ProviderCallRecord.objects.count() == provider_call_count
    assert RetrievalDocument.objects.count() == retrieval_document_count


@pytest.mark.django_db
def test_get_or_create_session_rejects_different_team_without_mutation() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    different_team = Team.objects.create(organization=organization, name='Different', slug='different')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-context-scope')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-scope-locked',
        runtime='codex',
        repository_root='/original/root',
    )
    data = _context_bundle_input(
        project,
        different_team,
        file_paths=(),
        request_id='request-session-scope-locked',
        session_id=session.external_session_id,
    )

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle()._get_or_create_session(organization, project, different_team, agent, data)

    assert exc_info.value.code == 'team_scope_denied'
    session.refresh_from_db()
    assert session.team_id == team.id
    assert session.repository_root == '/original/root'


@pytest.mark.django_db
def test_get_or_create_session_preserves_legacy_null_team_without_requested_team() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-context-null-team')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=None,
        agent=agent,
        external_session_id='session-null-team',
        runtime='codex',
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-session-null-team',
            session_id=session.external_session_id,
        ),
        team_id=None,
    )

    result = BuildContextBundle()._get_or_create_session(organization, project, None, agent, data)

    assert result.team_id is None
    assert AgentSession.objects.get(id=session.id).team_id is None


@pytest.mark.django_db
def test_build_context_bundle_denial_rolls_back_new_agent_creation() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    raw_key = _create_project_scoped_context_key(
        organization,
        project,
        'egk_test_services_null_team_new_agent_0123456789',
    )
    existing_agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='codex-existing',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        external_session_id='session-context-denied-new-agent',
        runtime='codex',
        platform_source='original-platform',
        repository_url='https://original.example/repo',
        repository_root='/original/root',
        branch='original-branch',
        cwd='/original/cwd',
        observation_sequence_cursor=7,
    )
    data = _context_bundle_input(
        project,
        team,
        file_paths=(),
        request_id='request-context-denied-new-agent',
        session_id=session.external_session_id,
        raw_key=raw_key,
    )
    data = replace(data, team_id=None, agent_external_id='codex-new')
    agent_count = Agent.objects.count()
    session_before = AgentSession.objects.get(id=session.id)
    bundle_count = ContextBundle.objects.count()
    provider_call_count = ProviderCallRecord.objects.count()
    retrieval_document_count = RetrievalDocument.objects.count()

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle().execute(data)

    assert exc_info.value.code == 'team_scope_denied'
    assert Agent.objects.count() == agent_count
    assert not Agent.objects.filter(organization=organization, external_id='codex-new').exists()
    session_after = AgentSession.objects.get(id=session.id)
    assert session_after.team_id == session_before.team_id
    assert session_after.agent_id == session_before.agent_id
    assert session_after.runtime == session_before.runtime
    assert session_after.platform_source == session_before.platform_source
    assert session_after.repository_url == session_before.repository_url
    assert session_after.repository_root == session_before.repository_root
    assert session_after.branch == session_before.branch
    assert session_after.cwd == session_before.cwd
    assert session_after.observation_sequence_cursor == session_before.observation_sequence_cursor
    assert session_after.created_at == session_before.created_at
    assert session_after.started_at == session_before.started_at
    assert session_after.updated_at == session_before.updated_at
    assert ContextBundle.objects.count() == bundle_count
    assert ProviderCallRecord.objects.count() == provider_call_count
    assert RetrievalDocument.objects.count() == retrieval_document_count


@pytest.mark.django_db
def test_build_context_bundle_denial_rolls_back_agent_version_update() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    raw_key = _create_project_scoped_context_key(
        organization,
        project,
        'egk_test_services_null_team_agent_update_0123456789',
    )
    agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='codex-versioned',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-context-denied-agent-update',
        runtime='codex',
        platform_source='original-platform',
        repository_url='https://original.example/repo',
        repository_root='/original/root',
        branch='original-branch',
        cwd='/original/cwd',
        observation_sequence_cursor=11,
    )
    data = _context_bundle_input(
        project,
        team,
        file_paths=(),
        request_id='request-context-denied-agent-update',
        session_id=session.external_session_id,
        raw_key=raw_key,
    )
    data = replace(data, team_id=None, agent_external_id='codex-versioned', agent_version='0.2.0')
    agent_before = Agent.objects.get(id=agent.id)
    session_before = AgentSession.objects.get(id=session.id)
    bundle_count = ContextBundle.objects.count()
    provider_call_count = ProviderCallRecord.objects.count()
    retrieval_document_count = RetrievalDocument.objects.count()

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle().execute(data)

    assert exc_info.value.code == 'team_scope_denied'
    agent_after = Agent.objects.get(id=agent.id)
    assert agent_after.version == agent_before.version
    assert agent_after.updated_at == agent_before.updated_at
    session_after = AgentSession.objects.get(id=session.id)
    assert session_after.team_id == session_before.team_id
    assert session_after.agent_id == session_before.agent_id
    assert session_after.runtime == session_before.runtime
    assert session_after.platform_source == session_before.platform_source
    assert session_after.repository_url == session_before.repository_url
    assert session_after.repository_root == session_before.repository_root
    assert session_after.branch == session_before.branch
    assert session_after.cwd == session_before.cwd
    assert session_after.observation_sequence_cursor == session_before.observation_sequence_cursor
    assert session_after.created_at == session_before.created_at
    assert session_after.started_at == session_before.started_at
    assert session_after.updated_at == session_before.updated_at
    assert ContextBundle.objects.count() == bundle_count
    assert ProviderCallRecord.objects.count() == provider_call_count
    assert RetrievalDocument.objects.count() == retrieval_document_count


@pytest.mark.django_db
def test_history_bearing_context_rejects_same_team_different_agent_without_side_effects() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    existing_agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='context-history-agent',
        version='0.1.0',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        external_session_id='context-history-agent-session',
        runtime='codex',
        platform_source='codex',
        observation_sequence_cursor=1,
    )
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        session=session,
        event_type='post_tool_use',
        payload={'tool_name': 'bash'},
        source_adapter='codex',
        client_event_id='context-history-agent-event',
        idempotency_key='context-history-agent-idempotency',
        content_hash='context-history-agent-hash',
        runtime='codex',
        sequence_number=1,
        normalization_contract_version=0,
    )
    Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        session=session,
        raw_event=raw_event,
        observation_type='tool_use',
        title='History',
        content_hash=raw_event.content_hash,
        session_sequence=1,
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-context-history-agent',
            session_id=session.external_session_id,
        ),
        agent_external_id='different-context-agent',
    )
    bundle_count = ContextBundle.objects.count()
    provider_call_count = ProviderCallRecord.objects.count()
    retrieval_document_count = RetrievalDocument.objects.count()

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle().execute(data)

    assert exc_info.value.code == 'team_scope_denied'
    assert str(exc_info.value) == 'Session is outside the requested team scope'
    assert Agent.objects.count() == 1
    session.refresh_from_db()
    assert session.agent_id == existing_agent.id
    assert session.runtime == 'codex'
    assert session.platform_source == 'codex'
    assert ContextBundle.objects.count() == bundle_count
    assert ProviderCallRecord.objects.count() == provider_call_count
    assert RetrievalDocument.objects.count() == retrieval_document_count


@pytest.mark.django_db
def test_history_free_context_corrects_agent_and_runtime() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    existing_agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='history-free-context-agent',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        external_session_id='history-free-context-session',
        runtime='codex',
        platform_source='',
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-history-free-context',
            session_id=session.external_session_id,
        ),
        agent_runtime='claude_code',
        agent_external_id='corrected-context-agent',
    )

    result = BuildContextBundle().execute(data)

    session.refresh_from_db()
    assert result.bundle.session_id == session.id
    assert session.runtime == 'claude_code'
    assert session.platform_source == 'claude_code'
    assert session.agent.external_id == 'corrected-context-agent'


@pytest.mark.django_db
def test_history_bearing_context_adopts_blank_platform_and_updates_agent_version() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='blank-platform-context-agent',
        version='old-version',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='blank-platform-context-session',
        runtime='codex',
        platform_source='',
        observation_sequence_cursor=1,
    )
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        payload={'tool_name': 'bash'},
        source_adapter='codex',
        client_event_id='blank-platform-context-event',
        idempotency_key='blank-platform-context-idempotency',
        content_hash='blank-platform-context-hash',
        runtime='codex',
        sequence_number=1,
        normalization_contract_version=0,
    )
    Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        raw_event=raw_event,
        observation_type='tool_use',
        title='History',
        content_hash=raw_event.content_hash,
        session_sequence=1,
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-blank-platform-context',
            session_id=session.external_session_id,
        ),
        agent_external_id=agent.external_id,
        agent_version='new-version',
    )

    result = BuildContextBundle().execute(data)

    agent.refresh_from_db()
    session.refresh_from_db()
    assert result.bundle.session_id == session.id
    assert agent.version == 'new-version'
    assert session.agent_id == agent.id
    assert session.runtime == 'codex'
    assert session.platform_source == 'codex'


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('stored_runtime', 'stored_platform_source', 'requested_runtime'),
    [
        ('claude_code', 'claude_code', 'codex'),
        ('codex', 'legacy-platform', 'codex'),
    ],
)
def test_history_bearing_context_rejects_identity_runtime_conflict_without_side_effects(
    stored_runtime: str,
    stored_platform_source: str,
    requested_runtime: str,
) -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    existing_agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id='context-history-runtime',
        version='old-version',
    )
    initial_agent_state = (existing_agent.version, existing_agent.updated_at)
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        external_session_id='context-history-runtime-session',
        runtime=stored_runtime,
        platform_source=stored_platform_source,
        observation_sequence_cursor=1,
    )
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        session=session,
        event_type='post_tool_use',
        payload={'tool_name': 'bash'},
        source_adapter='codex',
        client_event_id='context-history-runtime-event',
        idempotency_key='context-history-runtime-idempotency',
        content_hash='context-history-runtime-hash',
        runtime=stored_runtime,
        sequence_number=1,
        normalization_contract_version=0,
    )
    Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=existing_agent,
        session=session,
        raw_event=raw_event,
        observation_type='tool_use',
        title='History',
        content_hash=raw_event.content_hash,
        session_sequence=1,
    )
    data = replace(
        _context_bundle_input(
            project,
            team,
            file_paths=(),
            request_id='request-context-history-runtime',
            session_id=session.external_session_id,
        ),
        agent_runtime=requested_runtime,
        agent_external_id=existing_agent.external_id,
        agent_version='new-version',
    )
    bundle_count = ContextBundle.objects.count()
    provider_call_count = ProviderCallRecord.objects.count()
    retrieval_document_count = RetrievalDocument.objects.count()

    with pytest.raises(AccessDeniedError) as exc_info:
        BuildContextBundle().execute(data)

    assert exc_info.value.code == 'team_scope_denied'
    assert str(exc_info.value) == 'Session is outside the requested team scope'
    existing_agent.refresh_from_db()
    assert (existing_agent.version, existing_agent.updated_at) == initial_agent_state
    assert Agent.objects.count() == 1
    session.refresh_from_db()
    assert session.runtime == stored_runtime
    assert session.platform_source == stored_platform_source
    assert ContextBundle.objects.count() == bundle_count
    assert ProviderCallRecord.objects.count() == provider_call_count
    assert RetrievalDocument.objects.count() == retrieval_document_count


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
        observation_sequence_cursor=1,
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
        session_sequence=1,
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


@pytest.mark.django_db
def test_build_context_bundle_sets_injected_status_when_items_packed() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    _create_memory_document(
        organization,
        team,
        project,
        title='Injected memory',
        body='This memory is packed into the bundle and injected.',
        file_paths=['apps/backend/engram/context/services.py'],
    )

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=('apps/backend/engram/context/services.py',),
            request_id='request-status-injected-1',
            session_id='session-status-injected-1',
        ),
    )

    assert result.bundle.selected_count == 1
    assert result.bundle.status == ContextBundleStatus.INJECTED


@pytest.mark.django_db
def test_build_context_bundle_sets_skipped_status_when_no_items() -> None:
    _organization, team, project, _api_key = _provenance_project_scope()

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=('apps/backend/engram/context/services.py',),
            request_id='request-status-skipped-1',
            session_id='session-status-skipped-1',
        ),
    )

    assert result.bundle.selected_count == 0
    assert result.bundle.status == ContextBundleStatus.SKIPPED


@pytest.mark.django_db
def test_build_context_bundle_session_start_response_carries_injected_status() -> None:
    organization, team, project, _api_key = _provenance_project_scope()
    _create_memory_document(
        organization,
        team,
        project,
        title='Session-start memory',
        body='Injected into the session-start hook response.',
        file_paths=['apps/backend/engram/context/services.py'],
    )

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=('apps/backend/engram/context/services.py',),
            request_id='request-status-hook-1',
            session_id='session-status-hook-1',
        ),
    )
    response = result.to_response()

    assert response['status'] == ContextBundleStatus.INJECTED
    assert response['hook_specific_output']['hookEventName'] == 'SessionStart'


@pytest.mark.django_db
def test_build_context_bundle_session_start_response_carries_skipped_status() -> None:
    _organization, team, project, _api_key = _provenance_project_scope()

    result = BuildContextBundle().execute(
        _context_bundle_input(
            project,
            team,
            file_paths=('apps/backend/engram/context/services.py',),
            request_id='request-status-hook-skipped-1',
            session_id='session-status-hook-skipped-1',
        ),
    )
    response = result.to_response()

    assert response['status'] == ContextBundleStatus.SKIPPED
    assert response['hook_specific_output']['hookEventName'] == 'SessionStart'
