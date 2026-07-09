from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, TrigramWordSimilarity
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.context.retrieval_warnings import (
    RetrievalWarning,
    compute_retrieval_warnings,
    semantic_retrieval_gap,
)
from engram.context.term_extraction import extract_exact_terms, extract_symbols
from engram.core.domain.usecases.errors import DomainError
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    ContextBundleItem,
    ContextBundleStatus,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    OrganizationSettings,
    Project,
    RetrievalDocument,
    Team,
    VectorField,
    VisibilityScope,
)
from engram.core.redaction import redact_value
from engram.core.repository import resolve_project_for_scope
from engram.model_policy.services import (
    EmbeddingCallInput,
    EmbeddingCallResult,
    ModelPolicyError,
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)

try:
    from pgvector.django import CosineDistance
except ImportError:
    CosineDistance = None

logger = structlog.get_logger(__name__)


class ContextIndexError(DomainError):
    default_error_code = 'context_index_error'


@dataclass(frozen=True)
class IndexMemoryVersionInput:
    memory_version_id: uuid.UUID
    defer_embedding: bool = False


@dataclass(frozen=True)
class IndexMemoryVersionResult:
    retrieval_document: RetrievalDocument
    created: bool


@dataclass(frozen=True)
class ContextBundleInput:
    raw_key: str
    project_id: uuid.UUID | None
    team_id: uuid.UUID | None
    agent_runtime: str
    agent_version: str
    agent_external_id: str
    session_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    repository_url: str
    repository_root: str
    branch: str
    cwd: str
    query: str
    file_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    limit: int
    token_budget: int | None
    purpose: str
    kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalMatch:
    document: RetrievalDocument
    score: int
    matched_terms: tuple[str, ...]
    inclusion_reason: str


@dataclass(frozen=True)
class ContextBundleResult:
    bundle: ContextBundle
    matches: tuple[RetrievalMatch, ...]

    def to_response(self) -> dict[str, object]:
        rendered_context = self.bundle.rendered_text
        hook_specific_output: dict[str, str] = {}
        if self.bundle.purpose == 'session_start':
            hook_specific_output = {
                'hookEventName': 'SessionStart',
                'additionalContext': rendered_context,
            }
        elif self.bundle.purpose == 'user_prompt_submit':
            hook_specific_output = {
                'hookEventName': 'UserPromptSubmit',
                'additionalContext': rendered_context,
            }

        return {
            'status': self.bundle.status,
            'request_id': self.bundle.request_id,
            'context_bundle_id': str(self.bundle.id),
            'purpose': self.bundle.purpose,
            'rendered_context': rendered_context,
            'hook_specific_output': hook_specific_output,
            'items': [self._item_response(match) for match in self.matches],
            'warnings': list(self.bundle.metadata.get('warnings', [])),
        }

    def _item_response(self, match: RetrievalMatch) -> dict[str, object]:
        document = match.document
        memory = document.memory

        return {
            'citation': self._citation_for(document),
            'memory_id': str(memory.id),
            'memory_version_id': str(document.memory_version_id),
            'retrieval_document_id': str(document.id),
            'title': redact_text(memory.title),
            'body': redact_text(memory.body),
            'confidence': str(memory.confidence) if memory.confidence is not None else None,
            'kind': memory.kind,
            'inclusion_reason': match.inclusion_reason,
            'scope_evidence': self._scope_evidence(document),
            'matched_terms': list(match.matched_terms),
        }

    def _citation_for(self, document: RetrievalDocument) -> str:
        for item in self.bundle.items.all():
            if item.retrieval_document_id == document.id:
                return item.citation

        return ''

    def _scope_evidence(self, document: RetrievalDocument) -> dict[str, str]:
        for item in self.bundle.items.all():
            if item.retrieval_document_id == document.id:
                return dict(item.scope_evidence)

        return scope_evidence(document)


def normalize_lookup_value(value: object) -> str:
    return str(value).strip().casefold()


def normalize_lookup_values(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw_values: tuple[object, ...] = (values,)
    elif isinstance(values, list | tuple | set):
        raw_values = tuple(values)
    else:
        raw_values = (values,)

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        item = normalize_lookup_value(value)
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return tuple(normalized)


def unique_text_values(*groups: object) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        if isinstance(group, str):
            raw_values: tuple[object, ...] = (group,)
        elif isinstance(group, list | tuple | set):
            raw_values = tuple(group)
        else:
            raw_values = (group,)
        for raw_value in raw_values:
            item = str(raw_value).strip()
            key = item.casefold()
            if not item or key in seen:
                continue
            seen.add(key)
            values.append(item)

    return values


def redact_text(value: object) -> str:
    return str(redact_value(value).value)


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _render_annotation(kind: str, confidence: Decimal | None) -> str:
    parts = []
    if kind:
        parts.append(kind)
    if confidence is not None:
        parts.append(f'confidence {confidence}')
    if not parts:
        return ''

    return f' ({", ".join(parts)})'


def _render_block(memory: Memory, index: int) -> str:
    annotation = _render_annotation(memory.kind, memory.confidence)

    return f'- [M{index}] {redact_text(memory.title)}{annotation}\n  {redact_text(memory.body)}'


def _pack_to_budget(
    matches: tuple[RetrievalMatch, ...],
    token_budget: int | None,
    limit: int,
) -> tuple[tuple[RetrievalMatch, ...], tuple[RetrievalMatch, ...]]:
    if token_budget is None:
        return matches[:limit], matches[limit:]

    kept: list[RetrievalMatch] = []
    dropped: list[RetrievalMatch] = []
    tokens_used = 0

    for match in matches:
        if len(kept) >= limit:
            dropped.append(match)
            continue

        memory = match.document.memory
        index = len(kept) + 1
        block = _render_block(memory, index)
        cost = estimate_tokens(block)

        if not kept or tokens_used + cost <= token_budget:
            kept.append(match)
            tokens_used += cost
        else:
            dropped.append(match)

    return tuple(kept), tuple(dropped)


SEMANTIC_MIN_SIMILARITY = 0.3


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0

    return dot / (left_norm * right_norm)


def filter_documents_by_team_visibility(
    documents: Iterable[RetrievalDocument],
    scope: EffectiveScope,
) -> tuple[RetrievalDocument, ...]:
    authorized: list[RetrievalDocument] = []
    allowed_team_ids = set(scope.team_ids)
    for document in documents:
        if document.visibility_scope == VisibilityScope.PROJECT:
            authorized.append(document)
        elif document.visibility_scope == VisibilityScope.TEAM and document.team_id in allowed_team_ids:
            authorized.append(document)

    return tuple(authorized)


def authorized_retrieval_documents(
    organization: Organization,
    project: Project,
    scope: EffectiveScope,
    kinds: tuple[str, ...] = (),
    include_embeddings: bool = False,
) -> tuple[RetrievalDocument, ...]:
    documents = RetrievalDocument.objects.select_related(
        'memory',
        'memory_version',
        'team',
    ).filter(
        organization=organization,
        project=project,
        memory__status=MemoryStatus.APPROVED,
        memory__stale=False,
        memory__refuted=False,
        stale=False,
        refuted=False,
    )
    if kinds:
        documents = documents.filter(memory__kind__in=kinds)
    if not include_embeddings:
        deferred_fields = ['embedding_vector']
        if VectorField is not None:
            deferred_fields.append('embedding_pgvector')
        documents = documents.defer(*deferred_fields)

    return filter_documents_by_team_visibility(documents, scope)


def request_has_terms(query: str, file_paths: tuple[str, ...], symbols: tuple[str, ...]) -> bool:
    return bool(query.strip() or file_paths or symbols)


CONTAINS_MATCH_MIN_TOKEN_LENGTH = 4


def score_retrieval_document(
    document: RetrievalDocument,
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    has_request_terms: bool,
) -> RetrievalMatch | None:
    document_file_paths = tuple(str(value) for value in document.file_paths)
    file_match = first_path_match(file_paths, document_file_paths)
    if file_match:
        return RetrievalMatch(
            document=document,
            score=100,
            matched_terms=(file_match,),
            inclusion_reason=f'exact match: {file_match}',
        )

    document_symbols = tuple(str(value) for value in document.symbols)
    symbol_match = first_exact_match(symbols, document_symbols)
    if symbol_match:
        return RetrievalMatch(
            document=document,
            score=80,
            matched_terms=(symbol_match,),
            inclusion_reason=f'exact match: {symbol_match}',
        )

    contains_terms = contains_match_query_terms(query)
    exact_match = first_contains_match(contains_terms, tuple(str(value) for value in document.exact_terms))
    if exact_match:
        return RetrievalMatch(
            document=document,
            score=60,
            matched_terms=(exact_match,),
            inclusion_reason=f'exact match: {exact_match}',
        )

    full_text_match = first_full_text_match(contains_terms, document.full_text)
    if full_text_match:
        return RetrievalMatch(
            document=document,
            score=40,
            matched_terms=(full_text_match,),
            inclusion_reason=f'full-text match: {full_text_match}',
        )

    if not has_request_terms:
        return RetrievalMatch(
            document=document,
            score=1,
            matched_terms=(),
            inclusion_reason='filter-only authorized memory',
        )

    return None


PGVECTOR_FLOOR_DISTANCE_EPSILON = 1e-6


def _document_embedding_vectors(
    documents: tuple[RetrievalDocument, ...],
) -> dict[uuid.UUID, list[float]]:
    deferred_ids = [document.id for document in documents if 'embedding_vector' in document.get_deferred_fields()]
    loaded: dict[uuid.UUID, list[float]] = {}
    if deferred_ids:
        loaded = dict(
            RetrievalDocument.objects.filter(id__in=deferred_ids).values_list('id', 'embedding_vector'),
        )
    vectors: dict[uuid.UUID, list[float]] = {}
    for document in documents:
        if document.id in loaded:
            vectors[document.id] = loaded[document.id]
        else:
            vectors[document.id] = document.embedding_vector

    return vectors


def _semantic_retrieval_matches_python(
    documents: tuple[RetrievalDocument, ...],
    exact_matches: list[RetrievalMatch],
    query_vector: list[float],
) -> list[RetrievalMatch]:
    already_matched = {match.document.id for match in exact_matches}
    candidates = tuple(document for document in documents if document.id not in already_matched)
    vectors = _document_embedding_vectors(candidates)
    scored: list[tuple[float, RetrievalMatch]] = []
    for document in candidates:
        vector = vectors.get(document.id)
        if not vector:
            continue
        similarity = cosine_similarity(query_vector, list(vector))
        if similarity < SEMANTIC_MIN_SIMILARITY:
            continue
        scored.append(
            (
                similarity,
                RetrievalMatch(
                    document=document,
                    score=30,
                    matched_terms=(f'cosine {similarity:.2f}',),
                    inclusion_reason=f'semantic match: cosine {similarity:.2f}',
                ),
            ),
        )
    scored.sort(key=lambda item: -item[0])

    return [match for _similarity, match in scored]


def semantic_retrieval_matches_pgvector(
    documents: tuple[RetrievalDocument, ...],
    exact_matches: list[RetrievalMatch],
    query_vector: list[float],
) -> list[RetrievalMatch]:
    already_matched = {match.document.id for match in exact_matches}
    remaining = tuple(document for document in documents if document.id not in already_matched)
    if not remaining:
        return []

    remaining_ids = [document.id for document in remaining]
    pgvector_ids = set(
        RetrievalDocument.objects.filter(id__in=remaining_ids)
        .exclude(embedding_pgvector__isnull=True)
        .values_list('id', flat=True),
    )
    passing_ids: set[uuid.UUID] = set()
    if pgvector_ids and CosineDistance is not None and any(query_vector):
        max_distance = (1 - SEMANTIC_MIN_SIMILARITY) + PGVECTOR_FLOOR_DISTANCE_EPSILON
        passing_ids = set(
            RetrievalDocument.objects.filter(id__in=list(pgvector_ids))
            .annotate(distance=CosineDistance('embedding_pgvector', query_vector))
            .filter(distance__lte=max_distance)
            .values_list('id', flat=True),
        )

    candidates = tuple(
        document for document in remaining if document.id not in pgvector_ids or document.id in passing_ids
    )

    return _semantic_retrieval_matches_python(candidates, exact_matches, query_vector)


def semantic_retrieval_matches(
    documents: tuple[RetrievalDocument, ...],
    exact_matches: list[RetrievalMatch],
    query_vector: list[float],
) -> list[RetrievalMatch]:
    if VectorField is None or CosineDistance is None:
        return _semantic_retrieval_matches_python(documents, exact_matches, query_vector)

    return semantic_retrieval_matches_pgvector(documents, exact_matches, query_vector)


RECIPROCAL_RANK_FUSION_K = 60


def resolve_lexical_fusion_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('lexical_fusion_enabled', flat=True)
        .first()
    )
    if enabled is None:
        return False

    return enabled


def _lexical_search_query(query: str) -> SearchQuery | None:
    tokens = request_query_terms(query)
    if not tokens:
        return None

    search_query: SearchQuery | None = None
    for token in tokens:
        clause = SearchQuery(token, search_type='plain')
        search_query = clause if search_query is None else search_query | clause

    return search_query


def lexical_retrieval_ranks(
    documents: tuple[RetrievalDocument, ...],
    query: str,
) -> dict[uuid.UUID, int]:
    if not documents:
        return {}

    search_query = _lexical_search_query(query)
    if search_query is None:
        return {}

    document_ids = [document.id for document in documents]
    scored = dict(
        RetrievalDocument.objects.filter(id__in=document_ids)
        .annotate(lexical_rank=SearchRank(SearchVector('full_text'), search_query))
        .filter(lexical_rank__gt=0)
        .values_list('id', 'lexical_rank'),
    )
    if not scored:
        return {}

    documents_by_id = {document.id: document for document in documents}
    ordered = sorted(
        scored,
        key=lambda document_id: (
            -scored[document_id],
            -documents_by_id[document_id].updated_at.timestamp(),
            documents_by_id[document_id].memory.title.casefold(),
            str(document_id),
        ),
    )

    return {document_id: position for position, document_id in enumerate(ordered, start=1)}


def fuse_semantic_lexical(
    semantic_matches: list[RetrievalMatch],
    lexical_ranks: dict[uuid.UUID, int],
) -> list[RetrievalMatch]:
    semantic_ranks = {match.document.id: position for position, match in enumerate(semantic_matches, start=1)}

    def _rrf_score(match: RetrievalMatch) -> float:
        score = 1.0 / (RECIPROCAL_RANK_FUSION_K + semantic_ranks[match.document.id])
        lexical_rank = lexical_ranks.get(match.document.id)
        if lexical_rank is not None:
            score += 1.0 / (RECIPROCAL_RANK_FUSION_K + lexical_rank)

        return score

    return sorted(
        semantic_matches,
        key=lambda match: (
            -_rrf_score(match),
            -match.score,
            -match.document.updated_at.timestamp(),
            match.document.memory.title.casefold(),
            str(match.document.id),
        ),
    )


def lexical_fusion_matches(
    semantic_matches: list[RetrievalMatch],
    query: str,
) -> list[RetrievalMatch]:
    if not semantic_matches:
        return semantic_matches

    candidate_documents = tuple(match.document for match in semantic_matches)
    lexical_ranks = lexical_retrieval_ranks(candidate_documents, query)

    return fuse_semantic_lexical(semantic_matches, lexical_ranks)


TRIGRAM_MIN_SIMILARITY = 0.4


def resolve_lexical_recall_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('lexical_recall_enabled', flat=True)
        .first()
    )
    if enabled is None:
        return False

    return enabled


def resolve_require_provenance_enabled(organization: Organization) -> bool:
    enabled = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('require_provenance', flat=True)
        .first()
    )
    if enabled is None:
        return False

    return enabled


def lexical_recall_matches(
    documents: tuple[RetrievalDocument, ...],
    already_matched_ids: set[uuid.UUID],
    query: str,
) -> list[RetrievalMatch]:
    candidate_documents = tuple(document for document in documents if document.id not in already_matched_ids)
    if not candidate_documents:
        return []

    search_query = _lexical_search_query(query)
    if search_query is None:
        return []

    candidate_ids = [document.id for document in candidate_documents]
    signals = {
        row[0]: (float(row[1]), float(row[2]))
        for row in RetrievalDocument.objects.filter(id__in=candidate_ids)
        .annotate(
            ts=SearchRank(SearchVector('full_text'), search_query),
            trgm=TrigramWordSimilarity(query, 'full_text'),
        )
        .filter(Q(ts__gt=0) | Q(trgm__gte=TRIGRAM_MIN_SIMILARITY))
        .values_list('id', 'ts', 'trgm')
    }
    if not signals:
        return []

    documents_by_id = {document.id: document for document in candidate_documents}
    ordered_ids = sorted(
        signals,
        key=lambda document_id: (
            -signals[document_id][0],
            -signals[document_id][1],
            -documents_by_id[document_id].updated_at.timestamp(),
            documents_by_id[document_id].memory.title.casefold(),
            str(document_id),
        ),
    )
    matches: list[RetrievalMatch] = []
    for document_id in ordered_ids:
        ts, trgm = signals[document_id]
        if ts > 0:
            matched_term = f'ts_rank {ts:.3f}'
        else:
            matched_term = f'trigram {trgm:.2f}'
        matches.append(
            RetrievalMatch(
                document=documents_by_id[document_id],
                score=20,
                matched_terms=(matched_term,),
                inclusion_reason=f'lexical match: {matched_term}',
            ),
        )

    return matches


def fuse_retrieval_legs(
    semantic_matches: list[RetrievalMatch],
    lexical_matches: list[RetrievalMatch],
) -> list[RetrievalMatch]:
    semantic_ranks = {match.document.id: position for position, match in enumerate(semantic_matches, start=1)}
    lexical_ranks = {match.document.id: position for position, match in enumerate(lexical_matches, start=1)}

    fused_by_id: dict[uuid.UUID, RetrievalMatch] = {}
    for match in semantic_matches:
        fused_by_id[match.document.id] = match
    for match in lexical_matches:
        fused_by_id.setdefault(match.document.id, match)

    def _rrf_score(match: RetrievalMatch) -> float:
        score = 0.0
        semantic_rank = semantic_ranks.get(match.document.id)
        if semantic_rank is not None:
            score += 1.0 / (RECIPROCAL_RANK_FUSION_K + semantic_rank)
        lexical_rank = lexical_ranks.get(match.document.id)
        if lexical_rank is not None:
            score += 1.0 / (RECIPROCAL_RANK_FUSION_K + lexical_rank)

        return score

    return sorted(
        fused_by_id.values(),
        key=lambda match: (
            -_rrf_score(match),
            -match.score,
            -match.document.updated_at.timestamp(),
            match.document.memory.title.casefold(),
            str(match.document.id),
        ),
    )


def resolve_retrieval_strategy(matches: tuple[RetrievalMatch, ...] | list[RetrievalMatch]) -> str:
    if any(match.score == 30 for match in matches):
        return 'semantic_fallback'
    if any(match.score == 20 for match in matches):
        return 'lexical_recall'

    return 'exact'


def resolve_query_embedding(
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    organization: Organization,
    project: Project,
    team: Team | None,
    request_id: str,
    trace_id: str,
) -> EmbeddingCallResult | None:
    try:
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id if team is not None else None,
                task_type='embedding',
            ),
        )
        result = get_provider_gateway(resolved.policy).embed(
            EmbeddingCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team.id if team is not None else None,
                policy=resolved.policy,
                request_id=request_id,
                trace_id=trace_id,
                text='\n'.join([query, *file_paths, *symbols]),
            ),
        )
    except ModelPolicyError:
        return None
    except ProviderSecretError as error:
        logger.warning(
            'query_embedding_skipped',
            organization_id=str(organization.id),
            project_id=str(project.id),
            request_id=request_id,
            error=str(error),
        )

        return None

    return result


def derive_retrieval_terms(metadata: dict[str, object], title: str, body: str) -> tuple[list[str], list[str]]:
    symbols = unique_text_values(
        metadata.get('symbols', []),
        extract_symbols(title, body),
    )
    exact_terms = list(
        normalize_lookup_values(
            [
                *metadata.get('exact_terms', []),
                title,
                *extract_exact_terms(title, body),
            ],
        ),
    )

    return symbols, exact_terms


class IndexMemoryVersion:
    def execute(self, data: IndexMemoryVersionInput) -> IndexMemoryVersionResult:
        version = MemoryVersion.objects.select_related(
            'memory',
            'source_observation',
            'organization',
            'project',
        ).get(id=data.memory_version_id)
        memory = version.memory
        if memory.status != MemoryStatus.APPROVED or memory.stale or memory.refuted:
            raise ContextIndexError('Only approved memory can be indexed')

        observation = version.source_observation
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        file_paths = unique_text_values(
            metadata.get('file_paths', []),
            observation.files_read if observation is not None else [],
            observation.files_modified if observation is not None else [],
        )
        symbols, exact_terms = derive_retrieval_terms(metadata, memory.title, version.body)
        full_text = f'{memory.title}\n\n{version.body}'.strip()

        retrieval_document, created = RetrievalDocument.objects.update_or_create(
            memory_version=version,
            defaults={
                'organization': memory.organization,
                'project': memory.project,
                'team': memory.team,
                'memory': memory,
                'visibility_scope': memory.visibility_scope,
                'source_observation_ids': [str(observation.id)] if observation is not None else [],
                'file_paths': file_paths,
                'symbols': symbols,
                'exact_terms': exact_terms,
                'full_text': full_text,
                'embedding_reference': '',
                'stale': memory.stale,
                'refuted': memory.refuted,
                'metadata': {},
            },
        )
        RetrievalDocument.objects.filter(memory=memory).exclude(memory_version=version).update(
            stale=True,
            updated_at=timezone.now(),
        )
        if not data.defer_embedding:
            self._embed_document(retrieval_document, memory, version)

        return IndexMemoryVersionResult(retrieval_document=retrieval_document, created=created)

    def _embed_document(
        self,
        document: RetrievalDocument,
        memory: Memory,
        version: MemoryVersion,
    ) -> None:
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    task_type='embedding',
                ),
            )
            result = get_provider_gateway(resolved.policy).embed(
                EmbeddingCallInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    policy=resolved.policy,
                    request_id=f'memory-indexer:{version.id}:embedding',
                    trace_id=f'memory-indexer:{version.id}',
                    text=document.full_text,
                ),
            )
        except ModelPolicyError:
            return
        except ProviderSecretError as error:
            logger.warning(
                'context_embedding_skipped',
                organization_id=str(memory.organization_id),
                project_id=str(memory.project_id),
                memory_version_id=str(version.id),
                error=str(error),
            )

            return

        document.embedding_vector = list(result.embedding)
        document.embedding_reference = f'provider:{result.call_record_id}'
        update_fields = ['embedding_vector', 'embedding_reference', 'updated_at']
        if VectorField is not None:
            document.embedding_pgvector = list(result.embedding)
            update_fields.append('embedding_pgvector')
        document.save(update_fields=update_fields)


@dataclass(frozen=True)
class ReembedResult:
    scanned: int
    embedded: int
    failed: int


class ReembedMissingEmbeddings:
    def execute(self, *, batch_size: int = 200) -> ReembedResult:
        if VectorField is None:
            return ReembedResult(scanned=0, embedded=0, failed=0)

        documents = list(
            RetrievalDocument.objects.select_related('memory')
            .filter(embedding_pgvector__isnull=True, stale=False, refuted=False)
            .order_by('updated_at')[: max(1, batch_size)],
        )
        embedded = 0
        failed = 0
        for document in documents:
            if self._embed(document):
                embedded += 1
            else:
                failed += 1

        return ReembedResult(scanned=len(documents), embedded=embedded, failed=failed)

    def _embed(self, document: RetrievalDocument) -> bool:
        memory = document.memory
        attempt_id = uuid.uuid4()
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    task_type='embedding',
                ),
            )
            result = get_provider_gateway(resolved.policy).embed(
                EmbeddingCallInput(
                    organization_id=memory.organization_id,
                    project_id=memory.project_id,
                    team_id=memory.team_id,
                    policy=resolved.policy,
                    request_id=f'memory-reembed:{document.id}:{attempt_id}',
                    trace_id=f'memory-reembed:{document.id}',
                    text=document.full_text,
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            logger.warning(
                'context_reembed_failed',
                organization_id=str(memory.organization_id),
                project_id=str(memory.project_id),
                retrieval_document_id=str(document.id),
                error=str(error),
            )

            return False

        document.embedding_vector = list(result.embedding)
        document.embedding_pgvector = list(result.embedding)
        document.embedding_reference = f'provider:{result.call_record_id}'
        document.save(update_fields=['embedding_vector', 'embedding_pgvector', 'embedding_reference', 'updated_at'])

        return True


class BuildContextBundle:
    def execute(self, data: ContextBundleInput) -> ContextBundleResult:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='memories:read',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='context_bundle',
            target_id=data.request_id,
        )
        organization = Organization.objects.get(id=scope.organization_id)
        project = resolve_project_for_scope(
            scope=scope,
            project_id=data.project_id,
            repository_url=data.repository_url,
            allow_create=True,
            repository_root=data.repository_root,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
        )
        existing_bundle = self._existing_bundle(organization, project, data.request_id)
        if existing_bundle is not None:
            if not self._bundle_authorized_for_scope(existing_bundle, scope):
                raise AccessDeniedError('team_scope_denied', 'Context bundle is outside effective team scope')

            return self._result_from_bundle(existing_bundle)

        team = self._resolve_team(organization, data.team_id, scope)
        agent = self._get_or_create_agent(organization, data)
        session = self._get_or_create_session(organization, project, team, agent, data)
        retrieval_started_at = time.monotonic()
        authorized_documents = self._authorized_documents(organization, project, scope, data.kinds)
        matches, has_semantic, embedding_result, semantic_unavailable = self._rank_matches(
            authorized_documents,
            data,
            organization,
            project,
            team,
        )
        kept, budget_dropped = _pack_to_budget(matches, data.token_budget, data.limit)
        retrieval_latency_ms = round((time.monotonic() - retrieval_started_at) * 1000)
        tokens_used = sum(estimate_tokens(_render_block(m.document.memory, i)) for i, m in enumerate(kept, start=1))
        query_result = redact_value(data.query)
        retrieval_strategy = resolve_retrieval_strategy(matches)
        has_request_terms = request_has_terms(data.query, data.file_paths, data.symbols)
        warnings = compute_retrieval_warnings(
            organization=organization,
            project=project,
            scope=scope,
            query=data.query,
            file_paths=data.file_paths,
            symbols=data.symbols,
            has_request_terms=has_request_terms,
            included_matches=kept,
            semantic_unavailable=semantic_unavailable,
            dropped_for_budget=len(budget_dropped),
            kinds=data.kinds,
        )
        metadata: dict[str, object] = {'retrieval_strategy': retrieval_strategy}
        if query_result.redacted:
            metadata['redaction'] = {'query_text': True}
        if has_semantic and embedding_result is not None:
            metadata['semantic_provider_call_id'] = str(embedding_result.call_record_id)
        metadata['token_budget'] = data.token_budget
        metadata['tokens_used'] = tokens_used
        metadata['dropped_for_budget'] = len(budget_dropped)
        metadata['warnings'] = [warning.to_dict() for warning in warnings]

        with transaction.atomic():
            bundle = ContextBundle.objects.create(
                organization=organization,
                project=project,
                team=team,
                agent=agent,
                session=session,
                request_id=data.request_id,
                purpose=data.purpose,
                query_text=str(query_result.value),
                authorization_scope=self._authorization_scope(scope),
                token_budget=data.token_budget,
                selected_count=len(kept),
                metadata=metadata,
                retrieval_latency_ms=retrieval_latency_ms,
            )
            persisted_matches = self._create_items(bundle, kept)
            bundle.rendered_text = self._render_context(persisted_matches, warnings, data.purpose)
            bundle.selected_count = len(persisted_matches)
            bundle.status = ContextBundleStatus.INJECTED if persisted_matches else ContextBundleStatus.SKIPPED
            bundle.save(update_fields=['rendered_text', 'selected_count', 'status', 'updated_at'])
            self._audit_retrieval(
                bundle,
                persisted_matches,
                scope,
                data,
                has_semantic,
                embedding_result,
                retrieval_strategy,
            )

        bundle = ContextBundle.objects.prefetch_related(
            'items__retrieval_document__memory',
            'items__retrieval_document__memory_version',
        ).get(id=bundle.id)

        return self._result_from_bundle(bundle)

    def _existing_bundle(
        self,
        organization: Organization,
        project: Project,
        request_id: str,
    ) -> ContextBundle | None:
        return (
            ContextBundle.objects.prefetch_related(
                'items__retrieval_document__memory',
                'items__retrieval_document__memory_version',
            )
            .filter(organization=organization, project=project, request_id=request_id)
            .first()
        )

    def _result_from_bundle(self, bundle: ContextBundle) -> ContextBundleResult:
        matches = []
        for item in bundle.items.select_related(
            'retrieval_document__memory',
            'retrieval_document__memory_version',
        ).order_by('rank'):
            matches.append(
                RetrievalMatch(
                    document=item.retrieval_document,
                    score=int(item.metadata.get('score', 0)),
                    matched_terms=tuple(item.metadata.get('matched_terms', [])),
                    inclusion_reason=item.inclusion_reason,
                ),
            )

        return ContextBundleResult(bundle=bundle, matches=tuple(matches))

    def _bundle_authorized_for_scope(self, bundle: ContextBundle, scope: EffectiveScope) -> bool:
        allowed_team_ids = set(scope.team_ids)
        if bundle.team_id is not None and bundle.team_id not in allowed_team_ids:
            return False

        for item in bundle.items.select_related('retrieval_document'):
            document = item.retrieval_document
            if document.visibility_scope == VisibilityScope.PROJECT:
                continue
            if document.visibility_scope == VisibilityScope.TEAM and document.team_id in allowed_team_ids:
                continue

            return False

        return True

    def _resolve_team(
        self,
        organization: Organization,
        team_id: uuid.UUID | None,
        scope: EffectiveScope,
    ) -> Team | None:
        selected_team_id = team_id
        if selected_team_id is None and len(scope.team_ids) == 1:
            selected_team_id = scope.team_ids[0]
        if selected_team_id is None:
            return None

        return Team.objects.get(organization=organization, id=selected_team_id)

    def _get_or_create_agent(self, organization: Organization, data: ContextBundleInput) -> Agent:
        external_id = data.agent_external_id or f'{data.agent_runtime}:default'
        agent, _created = Agent.objects.get_or_create(
            organization=organization,
            runtime=data.agent_runtime,
            external_id=external_id,
            defaults={'version': data.agent_version, 'display_name': external_id},
        )
        if data.agent_version and agent.version != data.agent_version:
            agent.version = data.agent_version
            agent.save(update_fields=['version', 'updated_at'])

        return agent

    def _get_or_create_session(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        agent: Agent,
        data: ContextBundleInput,
    ) -> AgentSession:
        session, _created = AgentSession.objects.get_or_create(
            organization=organization,
            project=project,
            external_session_id=data.session_id,
            defaults={
                'team': team,
                'agent': agent,
                'runtime': data.agent_runtime,
                'platform_source': data.agent_runtime,
                'repository_url': data.repository_url,
                'repository_root': data.repository_root,
                'branch': data.branch,
                'cwd': data.cwd,
                'started_at': timezone.now(),
            },
        )
        update_fields = []
        for field, value in (
            ('team', team),
            ('agent', agent),
            ('runtime', data.agent_runtime),
            ('platform_source', data.agent_runtime),
            ('repository_url', data.repository_url),
            ('repository_root', data.repository_root),
            ('branch', data.branch),
            ('cwd', data.cwd),
        ):
            if getattr(session, field) != value:
                setattr(session, field, value)
                update_fields.append(field)
        if update_fields:
            update_fields.append('updated_at')
            session.save(update_fields=update_fields)

        return session

    def _authorized_documents(
        self,
        organization: Organization,
        project: Project,
        scope: EffectiveScope,
        kinds: tuple[str, ...] = (),
    ) -> tuple[RetrievalDocument, ...]:
        documents = authorized_retrieval_documents(organization, project, scope, kinds)
        if resolve_require_provenance_enabled(organization):
            documents = tuple(
                document for document in documents if document.memory_version.source_observation_id is not None
            )

        return documents

    def _rank_matches(
        self,
        documents: tuple[RetrievalDocument, ...],
        data: ContextBundleInput,
        organization: Organization,
        project: Project,
        team: Team | None,
    ) -> tuple[tuple[RetrievalMatch, ...], bool, EmbeddingCallResult | None, bool]:
        has_request_terms = request_has_terms(data.query, data.file_paths, data.symbols)
        exact_matches: list[RetrievalMatch] = []
        for document in documents:
            match = self._score_document(document, data, has_request_terms)
            if match is not None:
                exact_matches.append(match)
        exact_matches.sort(
            key=lambda match: (
                -match.score,
                -float(match.document.memory.confidence or 0),
                -match.document.updated_at.timestamp(),
                match.document.memory.title.casefold(),
                str(match.document.id),
            ),
        )
        if not has_request_terms:
            exact_matches = self._cap_filter_only_digests(exact_matches)
        if len(exact_matches) >= data.limit:
            return tuple(exact_matches[: data.limit]), False, None, False

        org_settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
        if not org_settings.hybrid_retrieval_enabled:
            return tuple(exact_matches), False, None, False

        embedding_result = self._resolve_query_embedding(data, organization, project, team)
        if embedding_result is None:
            semantic_unavailable = semantic_retrieval_gap(has_request_terms, exact_matches)
            return tuple(exact_matches), False, None, semantic_unavailable

        query_vector = list(embedding_result.embedding)
        semantic_matches = self._semantic_matches(documents, exact_matches, query_vector)
        if org_settings.lexical_recall_enabled:
            already_matched_ids = {match.document.id for match in exact_matches} | {
                match.document.id for match in semantic_matches
            }
            lexical_matches = lexical_recall_matches(documents, already_matched_ids, data.query)
            tail = fuse_retrieval_legs(semantic_matches, lexical_matches)
        elif org_settings.lexical_fusion_enabled:
            tail = lexical_fusion_matches(semantic_matches, data.query)
        else:
            tail = semantic_matches

        return (
            tuple((exact_matches + list(tail))[: data.limit]),
            bool(tail),
            embedding_result,
            False,
        )

    def _semantic_matches(
        self,
        documents: tuple[RetrievalDocument, ...],
        exact_matches: list[RetrievalMatch],
        query_vector: list[float],
    ) -> list[RetrievalMatch]:
        return semantic_retrieval_matches(documents, exact_matches, query_vector)

    def _cap_filter_only_digests(self, matches: list[RetrievalMatch]) -> list[RetrievalMatch]:
        capped: list[RetrievalMatch] = []
        digest_kept = False
        for match in matches:
            if match.document.memory.kind == 'digest':
                if digest_kept:
                    continue
                digest_kept = True
            capped.append(match)

        return capped

    def _resolve_query_embedding(
        self,
        data: ContextBundleInput,
        organization: Organization,
        project: Project,
        team: Team | None,
    ) -> EmbeddingCallResult | None:
        return resolve_query_embedding(
            data.query,
            data.file_paths,
            data.symbols,
            organization,
            project,
            team,
            data.request_id,
            data.trace_id or data.request_id,
        )

    def _score_document(
        self,
        document: RetrievalDocument,
        data: ContextBundleInput,
        has_request_terms: bool,
    ) -> RetrievalMatch | None:
        return score_retrieval_document(document, data.query, data.file_paths, data.symbols, has_request_terms)

    def _create_items(
        self,
        bundle: ContextBundle,
        matches: tuple[RetrievalMatch, ...],
    ) -> tuple[RetrievalMatch, ...]:
        persisted = []
        for index, match in enumerate(matches, start=1):
            citation = f'M{index}'
            ContextBundleItem.objects.create(
                bundle=bundle,
                organization=bundle.organization,
                project=bundle.project,
                memory=match.document.memory,
                retrieval_document=match.document,
                rank=index,
                citation=citation,
                inclusion_reason=redact_text(match.inclusion_reason),
                scope_evidence=scope_evidence(match.document),
                metadata={
                    'score': match.score,
                    'matched_terms': [redact_text(term) for term in match.matched_terms],
                },
            )
            persisted.append(match)

        return tuple(persisted)

    def _render_context(
        self,
        matches: tuple[RetrievalMatch, ...],
        warnings: list[RetrievalWarning],
        purpose: str,
    ) -> str:
        if not matches:
            if purpose == 'user_prompt_submit':
                base = ''
            else:
                base = '# Engram context\n\nNo approved memory matched this request.'
        else:
            lines = ['# Engram context', '']
            for index, match in enumerate(matches, start=1):
                lines.append(_render_block(match.document.memory, index))
            base = '\n'.join(lines)

        if not warnings:
            return base

        warning_lines = ['> Warnings:']
        warning_lines.extend(f'> - {warning.message}' for warning in warnings)
        warning_block = '\n'.join(warning_lines)
        if not base:
            return warning_block

        return base + '\n\n' + warning_block

    def _authorization_scope(self, scope: EffectiveScope) -> dict[str, object]:
        return {
            'capability': 'memories:read',
            'actor_type': scope.actor_type,
            'actor_id': scope.actor_id,
            'organization_id': str(scope.organization_id),
            'project_ids': [str(project_id) for project_id in scope.project_ids],
            'team_ids': [str(team_id) for team_id in scope.team_ids],
        }

    def _audit_retrieval(
        self,
        bundle: ContextBundle,
        matches: tuple[RetrievalMatch, ...],
        scope: EffectiveScope,
        data: ContextBundleInput,
        has_semantic: bool,
        embedding_result: EmbeddingCallResult | None,
        retrieval_strategy: str,
    ) -> None:
        metadata = {
            'selected_count': len(matches),
            'retrieval_strategy': retrieval_strategy,
            'scope_filters': {
                'organization_id': str(scope.organization_id),
                'project_ids': [str(project_id) for project_id in scope.project_ids],
                'team_ids': [str(team_id) for team_id in scope.team_ids],
            },
            'memory_ids': [str(match.document.memory_id) for match in matches],
            'retrieval_document_ids': [str(match.document.id) for match in matches],
        }
        if has_semantic and embedding_result is not None:
            metadata['semantic_provider_call_id'] = str(embedding_result.call_record_id)
            metadata['semantic_document_ids'] = [str(match.document.id) for match in matches if match.score == 30]

        AuditEvent.objects.create(
            organization=bundle.organization,
            project=bundle.project,
            team=bundle.team,
            event_type='MemoryRetrieved',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='context_bundle',
            target_id=str(bundle.id),
            capability='memories:read',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata=metadata,
        )


def request_query_terms(query: str) -> tuple[str, ...]:
    query_value = query.strip()
    if not query_value:
        return ()
    terms = [query_value]
    terms.extend(token for token in query_value.replace('/', ' ').split() if len(token.strip()) >= 2)

    return normalize_lookup_values(terms)


def contains_match_query_terms(query: str) -> tuple[str, ...]:
    query_value = query.strip()
    if not query_value:
        return ()
    terms = [query_value]
    terms.extend(
        token
        for token in query_value.replace('/', ' ').split()
        if len(token.strip()) >= CONTAINS_MATCH_MIN_TOKEN_LENGTH
    )

    return normalize_lookup_values(terms)


def first_path_match(request_paths: tuple[str, ...], document_paths: tuple[str, ...]) -> str:
    for request_path in request_paths:
        request_value = normalize_lookup_value(request_path)
        for document_path in document_paths:
            document_value = normalize_lookup_value(document_path)
            if (
                request_value == document_value
                or document_value.endswith(request_value)
                or request_value.endswith(
                    document_value,
                )
            ):
                return request_path

    return ''


def first_exact_match(request_values: tuple[str, ...], document_values: tuple[str, ...]) -> str:
    normalized_document_values = set(normalize_lookup_values(document_values))
    for request_value in request_values:
        if normalize_lookup_value(request_value) in normalized_document_values:
            return request_value

    return ''


def first_contains_match(request_values: tuple[str, ...], document_values: tuple[str, ...]) -> str:
    normalized_document_values = normalize_lookup_values(document_values)
    for request_value in request_values:
        normalized_request = normalize_lookup_value(request_value)
        for document_value in normalized_document_values:
            if (
                normalized_request == document_value
                or normalized_request in document_value
                or document_value in normalized_request
            ):
                return document_value

    return ''


def first_full_text_match(request_values: tuple[str, ...], full_text: str) -> str:
    normalized_full_text = normalize_lookup_value(full_text)
    for request_value in request_values:
        normalized_request = normalize_lookup_value(request_value)
        if normalized_request and normalized_request in normalized_full_text:
            return request_value

    return ''


def scope_evidence(document: RetrievalDocument) -> dict[str, str]:
    return {
        'visibility_scope': document.visibility_scope,
        'project_id': str(document.project_id),
        'team_id': str(document.team_id) if document.team_id else '',
    }
