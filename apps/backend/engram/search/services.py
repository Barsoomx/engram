from __future__ import annotations

import uuid
from dataclasses import dataclass

from engram.access.services import EffectiveScope, ResolveApiKeyScope
from engram.context.retrieval_warnings import RetrievalWarning, compute_retrieval_warnings, semantic_retrieval_gap
from engram.context.services import (
    RetrievalMatch,
    authorized_retrieval_documents,
    fuse_retrieval_legs,
    lexical_fusion_matches,
    lexical_recall_matches,
    redact_text,
    request_has_terms,
    resolve_query_embedding,
    score_retrieval_document,
    semantic_retrieval_matches,
)
from engram.core.models import Organization, OrganizationSettings, Project, Team
from engram.core.repository import resolve_project_for_scope


@dataclass(frozen=True)
class SearchInput:
    raw_key: str
    project_id: uuid.UUID | None
    team_id: uuid.UUID | None
    query: str
    file_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    limit: int
    request_id: str
    correlation_id: str
    repository_url: str = ''
    repository_root: str = ''


@dataclass(frozen=True)
class SearchResult:
    matches: tuple[RetrievalMatch, ...]
    warnings: tuple[RetrievalWarning, ...] = ()

    def to_response(self) -> dict[str, object]:
        items = [self._item_response(match, f'M{index}') for index, match in enumerate(self.matches, start=1)]

        return {
            'items': items,
            'warnings': [warning.to_dict() for warning in self.warnings],
        }

    def _item_response(self, match: RetrievalMatch, citation: str) -> dict[str, object]:
        document = match.document
        memory = document.memory

        return {
            'citation': citation,
            'memory_id': str(memory.id),
            'memory_version_id': str(document.memory_version_id),
            'retrieval_document_id': str(document.id),
            'title': redact_text(memory.title),
            'body': redact_text(memory.body),
            'confidence': str(memory.confidence) if memory.confidence is not None else None,
            'kind': memory.kind,
            'inclusion_reason': match.inclusion_reason,
            'scope_evidence': {
                'visibility_scope': document.visibility_scope,
                'project_id': str(document.project_id),
                'team_id': str(document.team_id) if document.team_id else '',
            },
            'matched_terms': list(match.matched_terms),
        }


class SearchMemories:
    def execute(self, data: SearchInput) -> SearchResult:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='search:query',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='memory_search',
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
        documents = authorized_retrieval_documents(organization, project, scope)
        has_request_terms = request_has_terms(data.query, data.file_paths, data.symbols)
        exact_matches: list[RetrievalMatch] = []
        for document in documents:
            match = score_retrieval_document(document, data.query, data.file_paths, data.symbols, has_request_terms)
            if match is not None:
                exact_matches.append(match)
        exact_matches.sort(
            key=lambda match: (
                -match.score,
                -match.document.updated_at.timestamp(),
                match.document.memory.title.casefold(),
                str(match.document.id),
            ),
        )
        if len(exact_matches) >= data.limit:
            return self._result(organization, project, scope, data, tuple(exact_matches[: data.limit]), False)

        org_settings, _ = OrganizationSettings.objects.get_or_create(organization=organization)
        if not org_settings.hybrid_retrieval_enabled:
            return self._result(organization, project, scope, data, tuple(exact_matches), False)

        embedding_result = resolve_query_embedding(
            data.query,
            data.file_paths,
            data.symbols,
            organization,
            project,
            self._resolve_team(data, scope),
            data.request_id,
            data.request_id,
        )
        if embedding_result is None:
            semantic_unavailable = semantic_retrieval_gap(has_request_terms, exact_matches)
            return self._result(organization, project, scope, data, tuple(exact_matches), semantic_unavailable)

        query_vector = list(embedding_result.embedding)
        semantic_matches = semantic_retrieval_matches(documents, exact_matches, query_vector)
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

        matches = tuple((exact_matches + list(tail))[: data.limit])

        return self._result(organization, project, scope, data, matches, False)

    def _result(
        self,
        organization: Organization,
        project: Project,
        scope: EffectiveScope,
        data: SearchInput,
        matches: tuple[RetrievalMatch, ...],
        semantic_unavailable: bool,
    ) -> SearchResult:
        warnings = compute_retrieval_warnings(
            organization=organization,
            project=project,
            scope=scope,
            query=data.query,
            file_paths=data.file_paths,
            symbols=data.symbols,
            has_request_terms=request_has_terms(data.query, data.file_paths, data.symbols),
            included_matches=matches,
            semantic_unavailable=semantic_unavailable,
        )

        return SearchResult(matches=matches, warnings=tuple(warnings))

    def _resolve_team(self, data: SearchInput, scope: EffectiveScope) -> Team | None:
        selected_team_id = data.team_id
        if selected_team_id is None and len(scope.team_ids) == 1:
            selected_team_id = scope.team_ids[0]
        if selected_team_id is None:
            return None

        return Team.objects.get(organization_id=scope.organization_id, id=selected_team_id)
