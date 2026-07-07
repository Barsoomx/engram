from __future__ import annotations

import uuid
from dataclasses import dataclass

from engram.access.auth_services import PROJECT_ADMIN_CAPABILITIES
from engram.access.services import AccessDeniedError, EffectiveScope
from engram.context.services import (
    SEMANTIC_MIN_SIMILARITY,
    cosine_similarity,
    lexical_recall_matches,
    resolve_query_embedding,
    score_retrieval_document,
)
from engram.core.models import (
    Memory,
    MemoryStatus,
    Organization,
    OrganizationSettings,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)

DEFAULT_PACK_LIMIT = 20


def _is_full_org_admin(scope: EffectiveScope) -> bool:
    return bool(PROJECT_ADMIN_CAPABILITIES & set(scope.capabilities))


def _confidence_str(memory: Memory) -> str | None:
    if memory.confidence is None:
        return None

    return str(memory.confidence)


@dataclass(frozen=True)
class DebugMatch:
    memory_id: uuid.UUID
    title: str
    score: int
    matched_on: str
    kind: str
    confidence: str | None


@dataclass(frozen=True)
class DebugSemanticCandidate:
    memory_id: uuid.UUID
    title: str
    score: float
    kind: str
    confidence: str | None


@dataclass(frozen=True)
class DebugPackedItem:
    memory_id: uuid.UUID
    title: str
    kind: str
    confidence: str | None


@dataclass(frozen=True)
class DebugExcluded:
    memory_id: uuid.UUID
    title: str
    reason: str


@dataclass(frozen=True)
class SearchDebugResult:
    scope_filters: dict[str, object]
    candidate_universe_count: int
    exact_matches: list[DebugMatch]
    semantic_enabled: bool
    semantic_candidates: list[DebugSemanticCandidate]
    lexical_enabled: bool
    lexical_candidates: list[DebugMatch]
    packed_context: list[DebugPackedItem]
    excluded: list[DebugExcluded]


class ReplaySearchDebug:
    def execute(
        self,
        organization: Organization,
        project: Project,
        scope: EffectiveScope,
        query: str,
        team_id: uuid.UUID | None,
        file_paths: tuple[str, ...],
        symbols: tuple[str, ...],
    ) -> SearchDebugResult:
        self._authorize_project(scope, project)
        allowed_team_ids = self._allowed_team_ids(scope, team_id)
        team = self._resolve_team(organization, team_id)

        all_documents = list(
            RetrievalDocument.objects.select_related('memory', 'team').filter(
                organization=organization,
                project=project,
            )
        )

        scope_filters: dict[str, object] = {
            'organization_id': str(organization.id),
            'project_id': str(project.id),
            'team_ids': [str(tid) for tid in sorted(allowed_team_ids)],
        }

        authorized, excluded = self._classify_documents(all_documents, allowed_team_ids)

        has_request_terms = bool(query.strip() or file_paths or symbols)
        scored, scoring_excluded = self._score_authorized(authorized, query, file_paths, symbols, has_request_terms)
        excluded.extend(scoring_excluded)

        org_settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)

        semantic_enabled, semantic_candidates, semantic_matched_ids = self._build_semantic_candidates(
            authorized, scored, query, file_paths, symbols, organization, project, team, org_settings
        )

        lexical_enabled, lexical_candidates = self._build_lexical_candidates(
            authorized, scored, semantic_matched_ids, query, org_settings
        )

        combined: list[tuple[uuid.UUID, str, str, str | None]] = (
            [(doc.memory_id, doc.memory.title, doc.memory.kind, _confidence_str(doc.memory)) for doc, _, _ in scored]
            + [(c.memory_id, c.title, c.kind, c.confidence) for c in semantic_candidates]
            + [(c.memory_id, c.title, c.kind, c.confidence) for c in lexical_candidates]
        )

        packed_context, budget_excluded = self._pack(combined)
        excluded.extend(budget_excluded)

        exact_matches = [
            DebugMatch(
                memory_id=doc.memory_id,
                title=doc.memory.title,
                score=score,
                matched_on=inclusion_reason,
                kind=doc.memory.kind,
                confidence=_confidence_str(doc.memory),
            )
            for doc, score, inclusion_reason in scored
        ]

        return SearchDebugResult(
            scope_filters=scope_filters,
            candidate_universe_count=len(all_documents),
            exact_matches=exact_matches,
            semantic_enabled=semantic_enabled,
            semantic_candidates=semantic_candidates,
            lexical_enabled=lexical_enabled,
            lexical_candidates=lexical_candidates,
            packed_context=packed_context,
            excluded=excluded,
        )

    def _classify_documents(
        self,
        documents: list[RetrievalDocument],
        allowed_team_ids: set[uuid.UUID],
    ) -> tuple[list[RetrievalDocument], list[DebugExcluded]]:
        authorized: list[RetrievalDocument] = []
        excluded: list[DebugExcluded] = []

        for doc in documents:
            reason = self._exclusion_reason(doc, allowed_team_ids)
            if reason:
                excluded.append(
                    DebugExcluded(
                        memory_id=doc.memory_id,
                        title=doc.memory.title,
                        reason=reason,
                    )
                )
            else:
                authorized.append(doc)

        return authorized, excluded

    def _score_authorized(
        self,
        authorized: list[RetrievalDocument],
        query: str,
        file_paths: tuple[str, ...],
        symbols: tuple[str, ...],
        has_request_terms: bool,
    ) -> tuple[list[tuple[RetrievalDocument, int, str]], list[DebugExcluded]]:
        scored: list[tuple[RetrievalDocument, int, str]] = []
        excluded: list[DebugExcluded] = []

        for doc in authorized:
            match = score_retrieval_document(doc, query, file_paths, symbols, has_request_terms)
            if match is not None:
                scored.append((doc, match.score, match.inclusion_reason))
            else:
                excluded.append(
                    DebugExcluded(
                        memory_id=doc.memory_id,
                        title=doc.memory.title,
                        reason='below_relevance',
                    )
                )

        scored.sort(
            key=lambda item: (
                -item[1],
                -item[0].updated_at.timestamp(),
                item[0].memory.title.casefold(),
                str(item[0].id),
            ),
        )

        return scored, excluded

    def _build_semantic_candidates(
        self,
        authorized: list[RetrievalDocument],
        scored: list[tuple[RetrievalDocument, int, str]],
        query: str,
        file_paths: tuple[str, ...],
        symbols: tuple[str, ...],
        organization: Organization,
        project: Project,
        team: Team | None,
        org_settings: OrganizationSettings,
    ) -> tuple[bool, list[DebugSemanticCandidate], set[uuid.UUID]]:
        if not org_settings.hybrid_retrieval_enabled:
            return False, [], set()

        embedding_result = resolve_query_embedding(
            query,
            file_paths,
            symbols,
            organization,
            project,
            team,
            request_id='admin:search-debug',
            trace_id='admin:search-debug',
        )

        if embedding_result is None:
            return False, [], set()

        query_vector = list(embedding_result.embedding)
        already_matched_ids = {doc.id for doc, _, _ in scored}
        semantic_scored: list[tuple[float, RetrievalDocument]] = []

        for doc in authorized:
            if doc.id in already_matched_ids or not doc.embedding_vector:
                continue
            similarity = cosine_similarity(query_vector, list(doc.embedding_vector))
            if similarity >= SEMANTIC_MIN_SIMILARITY:
                semantic_scored.append((similarity, doc))

        semantic_scored.sort(key=lambda item: -item[0])
        candidates = [
            DebugSemanticCandidate(
                memory_id=doc.memory_id,
                title=doc.memory.title,
                score=round(similarity, 4),
                kind=doc.memory.kind,
                confidence=_confidence_str(doc.memory),
            )
            for similarity, doc in semantic_scored
        ]
        semantic_matched_ids = {doc.id for _similarity, doc in semantic_scored}

        return True, candidates, semantic_matched_ids

    def _build_lexical_candidates(
        self,
        authorized: list[RetrievalDocument],
        scored: list[tuple[RetrievalDocument, int, str]],
        semantic_matched_ids: set[uuid.UUID],
        query: str,
        org_settings: OrganizationSettings,
    ) -> tuple[bool, list[DebugMatch]]:
        if not org_settings.lexical_recall_enabled:
            return False, []

        already_matched_ids = {doc.id for doc, _, _ in scored} | semantic_matched_ids
        matches = lexical_recall_matches(tuple(authorized), already_matched_ids, query)
        candidates = [
            DebugMatch(
                memory_id=match.document.memory_id,
                title=match.document.memory.title,
                score=match.score,
                matched_on=match.inclusion_reason,
                kind=match.document.memory.kind,
                confidence=_confidence_str(match.document.memory),
            )
            for match in matches
        ]

        return True, candidates

    def _pack(
        self,
        combined: list[tuple[uuid.UUID, str, str, str | None]],
    ) -> tuple[list[DebugPackedItem], list[DebugExcluded]]:
        packed: list[DebugPackedItem] = []
        excluded: list[DebugExcluded] = []

        for i, (memory_id, title, kind, confidence) in enumerate(combined):
            if i < DEFAULT_PACK_LIMIT:
                packed.append(DebugPackedItem(memory_id=memory_id, title=title, kind=kind, confidence=confidence))
            else:
                excluded.append(
                    DebugExcluded(
                        memory_id=memory_id,
                        title=title,
                        reason='token_budget',
                    )
                )

        return packed, excluded

    def _authorize_project(self, scope: EffectiveScope, project: Project) -> None:
        if _is_full_org_admin(scope):
            return

        if project.id in scope.project_ids:
            return

        raise AccessDeniedError('project_scope_denied', 'Scope cannot access requested project')

    def _allowed_team_ids(
        self,
        scope: EffectiveScope,
        team_id: uuid.UUID | None,
    ) -> set[uuid.UUID]:
        allowed: set[uuid.UUID] = set(scope.team_ids)
        if team_id is None:
            return allowed

        if _is_full_org_admin(scope):
            return {team_id}

        if team_id not in allowed:
            raise AccessDeniedError('team_scope_denied', 'Scope cannot access requested team')

        return {team_id}

    def _resolve_team(
        self,
        organization: Organization,
        team_id: uuid.UUID | None,
    ) -> Team | None:
        if team_id is None:
            return None

        return Team.objects.filter(organization=organization, id=team_id).first()

    def _exclusion_reason(
        self,
        doc: RetrievalDocument,
        allowed_team_ids: set[uuid.UUID],
    ) -> str:
        memory = doc.memory

        if memory.status != MemoryStatus.APPROVED:
            return 'not_approved'

        if memory.stale or doc.stale:
            return 'stale'

        if memory.refuted or doc.refuted:
            return 'refuted'

        if doc.visibility_scope == VisibilityScope.PROJECT:
            return ''

        if doc.visibility_scope == VisibilityScope.TEAM:
            if doc.team_id in allowed_team_ids:
                return ''

            return 'team_not_in_scope'

        return 'visibility_not_injectable'
