from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, TrigramWordSimilarity
from django.core.exceptions import FieldError
from django.db import DatabaseError
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When

from engram.context.term_extraction import normalize_lookup_values
from engram.core.models import Memory, MemoryConflict, MemoryStatus, MemoryVersion, RetrievalDocument, VisibilityScope
from engram.memory.deterministic_gates import EffectiveCandidateScope
from engram.memory.workflow_work import canonical_json_bytes

try:
    from pgvector.django import CosineDistance
except ImportError:
    CosineDistance = None


@dataclass(frozen=True, slots=True)
class BuildCurationShortlistInput:
    organization_id: UUID
    project_id: UUID
    effective_scope: EffectiveCandidateScope
    title: str
    body: str
    query_embedding: tuple[float, ...] | None = None
    exact_terms: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurationShortlistEntry:
    memory_id: UUID
    memory_version_id: UUID
    current_transition_id: UUID
    visibility_scope: str
    team_id: UUID | None
    title: str
    body: str
    kind: str
    body_hash: str
    exact_overlap: int
    vector_distance: float | None
    lexical_rank: float | None
    trigram_similarity: float | None
    has_open_conflict: bool


@dataclass(frozen=True, slots=True)
class CurationShortlist:
    entries: tuple[CurationShortlistEntry, ...]
    manifest_hash: str
    authorized_corpus_count: int
    comparison_complete: bool


class CurationShortlistError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _scope_q(data: BuildCurationShortlistInput) -> Q:
    scope = data.effective_scope
    if scope.visibility_scope == VisibilityScope.PROJECT and scope.team_id is None:
        return Q(visibility_scope=VisibilityScope.PROJECT)
    if scope.visibility_scope == VisibilityScope.TEAM and scope.team_id is not None:
        return Q(visibility_scope=VisibilityScope.PROJECT) | Q(
            visibility_scope=VisibilityScope.TEAM,
            team_id=scope.team_id,
        )
    raise CurationShortlistError('invalid_effective_scope')


def _nullable_equal(left: str, right: str) -> Q:
    return Q(**{f'{left}__isnull': True, f'{right}__isnull': True}) | Q(**{left: F(right)})


def _authorized_memories(data: BuildCurationShortlistInput) -> object:
    return Memory.objects.filter(
        organization_id=data.organization_id,
        project_id=data.project_id,
        status__in=(MemoryStatus.APPROVED, MemoryStatus.CONFLICT),
        transition_contract_version=1,
        current_transition__isnull=False,
        stale=False,
        refuted=False,
    ).filter(_scope_q(data))


def _coherent_documents(data: BuildCurationShortlistInput) -> object:
    transition = (
        Q(memory__current_transition__organization_id=data.organization_id)
        & Q(memory__current_transition__project_id=data.project_id)
        & (
            Q(
                memory__current_transition__memory_id=F('memory_id'),
                memory__current_transition__to_version_id=F('memory_version_id'),
                memory__current_transition__exact_document_id=F('id'),
            )
            | Q(
                memory__current_transition__result_memory_id=F('memory_id'),
                memory__current_transition__result_version_id=F('memory_version_id'),
                memory__current_transition__result_exact_document_id=F('id'),
            )
        )
    )
    visibility = _scope_q(data)
    return (
        RetrievalDocument.objects.filter(
            organization_id=data.organization_id,
            project_id=data.project_id,
            memory__organization_id=data.organization_id,
            memory__project_id=data.project_id,
            memory_version__organization_id=data.organization_id,
            memory_version__project_id=data.project_id,
            memory_version__memory_id=F('memory_id'),
            memory__status__in=(MemoryStatus.APPROVED, MemoryStatus.CONFLICT),
            memory__transition_contract_version=1,
            memory__current_transition__isnull=False,
            memory__stale=False,
            memory__refuted=False,
            memory_version__version=F('memory__current_version'),
            stale=False,
            refuted=False,
            projection_contract_version=1,
            visibility_scope=F('memory__visibility_scope'),
            memory__body=F('memory_version__body'),
        )
        .filter(visibility)
        .filter(_nullable_equal('team_id', 'memory__team_id'))
        .filter(_nullable_equal('memory__current_transition__team_id', 'memory__team_id'))
        .filter(transition)
    )


def _normalize_symbols(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw = (values,)
    elif isinstance(values, (list, tuple, set)):
        raw = tuple(values)
    else:
        raw = (values,)
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return tuple(result)


def _validate_embedding(embedding: tuple[float, ...] | None) -> tuple[float, ...] | None:
    if embedding is None:
        return None
    values = tuple(embedding)
    if (
        len(values) != 1536
        or not any(
            float(value) != 0.0 for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in values
        )
    ):
        raise CurationShortlistError('embedding_invalid_result')
    return values


def _manifest_hash(
    entries: tuple[CurationShortlistEntry, ...],
    authorized_corpus_count: int,
    comparison_complete: bool,
) -> str:
    payload = {
        'authorized_corpus_count': authorized_corpus_count,
        'comparison_complete': comparison_complete,
        'entries': [
            {
                'memory_id': str(entry.memory_id),
                'memory_version_id': str(entry.memory_version_id),
                'current_transition_id': str(entry.current_transition_id),
                'scope_key': f'{entry.visibility_scope}:{entry.team_id or ""}',
                'exact_overlap': entry.exact_overlap,
                'vector_distance': None if entry.vector_distance is None else f'{entry.vector_distance:.12f}',
                'lexical_rank': None if entry.lexical_rank is None else f'{entry.lexical_rank:.12f}',
                'trigram_similarity': (
                    None if entry.trigram_similarity is None else f'{entry.trigram_similarity:.12f}'
                ),
                'has_open_conflict': entry.has_open_conflict,
                'body_hash': entry.body_hash,
            }
            for entry in entries
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class BuildCurationShortlist:
    MAX_VECTOR: ClassVar[int] = 8
    MAX_EXACT: ClassVar[int] = 4
    MAX_LEXICAL: ClassVar[int] = 4
    MAX_ENTRIES: ClassVar[int] = 12

    @classmethod
    def execute(cls, data: BuildCurationShortlistInput) -> CurationShortlist:  # noqa: C901
        terms = normalize_lookup_values(data.exact_terms)
        symbols = _normalize_symbols(data.symbols)
        if len(terms) > 32 or len(symbols) > 32:
            raise CurationShortlistError('shortlist_input_invalid')
        embedding = _validate_embedding(data.query_embedding)
        try:
            corpus = _authorized_memories(data)
            corpus_count = corpus.count()
            if corpus_count == 0:
                return CurationShortlist((), _manifest_hash((), 0, True), 0, True)
            base = _coherent_documents(data)
            if base.count() != corpus_count:
                raise CurationShortlistError('transition_dependency_unavailable')
            corpus_fully_embedded = not (
                embedding is not None
                and CosineDistance is not None
                and base.filter(embedding_pgvector__isnull=True).exists()
            )
            scores: dict[UUID, dict[str, float | int | None]] = {}
            if embedding is not None and CosineDistance is not None:
                vector_rows = (
                    base.exclude(embedding_pgvector__isnull=True)
                    .annotate(
                        distance=CosineDistance('embedding_pgvector', embedding),
                    )
                    .filter(distance__lte=0.45)
                    .order_by('distance', 'memory_version_id')
                    .values(
                        'memory_version_id',
                        'distance',
                    )[: cls.MAX_VECTOR]
                )
                for row in vector_rows:
                    scores.setdefault(row['memory_version_id'], {})['vector_distance'] = row['distance']
            if terms or symbols:
                exact = Value(0, output_field=IntegerField())
                for term in terms:
                    exact = exact + Case(
                        When(exact_terms__contains=[term], then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                for symbol in symbols:
                    exact = exact + Case(
                        When(symbols__contains=[symbol], then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                for row in (
                    base.annotate(exact_overlap=exact)
                    .filter(exact_overlap__gt=0)
                    .order_by(
                        '-exact_overlap',
                        'memory_version_id',
                    )
                    .values('memory_version_id', 'exact_overlap')[: cls.MAX_EXACT]
                ):
                    scores.setdefault(row['memory_version_id'], {})['exact_overlap'] = row['exact_overlap']
            query_text = f'{data.title}\n{data.body}'.strip()
            if query_text:
                query = SearchQuery(query_text, search_type='plain')
                lexical = (
                    base.annotate(
                        lexical_rank=SearchRank(SearchVector('full_text'), query),
                        trigram_similarity=TrigramWordSimilarity(query_text, 'full_text'),
                    )
                    .filter(Q(lexical_rank__gt=0) | Q(trigram_similarity__gte=0.30))
                    .order_by(
                        '-lexical_rank',
                        '-trigram_similarity',
                        'memory_version_id',
                    )
                    .values('memory_version_id', 'lexical_rank', 'trigram_similarity')[: cls.MAX_LEXICAL]
                )
                for row in lexical:
                    scores.setdefault(row['memory_version_id'], {}).update(
                        lexical_rank=row['lexical_rank'], trigram_similarity=row['trigram_similarity']
                    )
            if embedding is None and not scores:
                raise CurationShortlistError('embedding_unavailable')
            selected_ids = tuple(scores)[:16]
            conflict = MemoryConflict.objects.filter(
                memory_version_id=OuterRef('memory_version_id'), resolved_transition__isnull=True
            )
            rows = (
                base.select_related('memory', 'memory_version')
                .annotate(has_open_conflict=Exists(conflict))
                .filter(memory_version_id__in=selected_ids)
            )
            hydrated = {row.memory_version_id: row for row in rows}
            if len(hydrated) != len(selected_ids):
                raise CurationShortlistError('transition_dependency_unavailable')
            entries: list[CurationShortlistEntry] = []
            for version_id, signal in scores.items():
                row = hydrated[version_id]
                memory: Memory = row.memory
                version: MemoryVersion = row.memory_version
                entries.append(
                    CurationShortlistEntry(
                        memory_id=memory.id,
                        memory_version_id=version.id,
                        current_transition_id=memory.current_transition_id,
                        visibility_scope=memory.visibility_scope,
                        team_id=memory.team_id,
                        title=memory.title,
                        body=version.body,
                        kind=memory.kind,
                        body_hash=hashlib.sha256(version.body.encode()).hexdigest(),
                        exact_overlap=int(signal.get('exact_overlap') or 0),
                        vector_distance=(
                            None if signal.get('vector_distance') is None else float(signal['vector_distance'])
                        ),
                        lexical_rank=None if signal.get('lexical_rank') is None else float(signal['lexical_rank']),
                        trigram_similarity=(
                            None if signal.get('trigram_similarity') is None else float(signal['trigram_similarity'])
                        ),
                        has_open_conflict=bool(row.has_open_conflict),
                    )
                )
            entries.sort(
                key=lambda item: (
                    -item.exact_overlap,
                    item.vector_distance is None,
                    item.vector_distance if item.vector_distance is not None else 0.0,
                    -(item.lexical_rank or 0.0),
                    str(item.memory_version_id),
                )
            )
            final = tuple(entries[: cls.MAX_ENTRIES])
            revalidated_count = (
                _coherent_documents(data)
                .filter(memory_version_id__in=[entry.memory_version_id for entry in final])
                .count()
            )
            if revalidated_count != len(final):
                raise CurationShortlistError('transition_dependency_unavailable')
            if not final and not corpus_fully_embedded:
                raise CurationShortlistError('embedding_unavailable')
            comparison_complete = embedding is not None and CosineDistance is not None and corpus_fully_embedded
            return CurationShortlist(
                final,
                _manifest_hash(final, corpus_count, comparison_complete),
                corpus_count,
                comparison_complete,
            )
        except CurationShortlistError:
            raise
        except (DatabaseError, FieldError, TypeError, ValueError) as exc:
            raise CurationShortlistError('shortlist_query_failed') from exc
