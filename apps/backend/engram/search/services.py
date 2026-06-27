from __future__ import annotations

import uuid
from dataclasses import dataclass

from engram.access.services import EffectiveScope, ResolveApiKeyScope
from engram.context.services import (
    RetrievalMatch,
    authorized_retrieval_documents,
    redact_text,
    resolve_query_embedding,
    score_retrieval_document,
    semantic_retrieval_matches,
)
from engram.core.models import Organization, Project, Team


@dataclass(frozen=True)
class SearchInput:
    raw_key: str
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    query: str
    file_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    limit: int
    request_id: str
    correlation_id: str


@dataclass(frozen=True)
class SearchResult:
    matches: tuple[RetrievalMatch, ...]

    def to_response(self) -> dict[str, object]:
        items = [self._item_response(match, f'M{index}') for index, match in enumerate(self.matches, start=1)]

        return {
            'items': items,
            'warnings': [],
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
        project = Project.objects.get(organization=organization, id=data.project_id)
        documents = authorized_retrieval_documents(organization, project, scope)
        has_request_terms = bool(data.query.strip() or data.file_paths or data.symbols)
        matches: list[RetrievalMatch] = []
        for document in documents:
            match = score_retrieval_document(document, data.query, data.file_paths, data.symbols, has_request_terms)
            if match is not None:
                matches.append(match)
        matches.sort(
            key=lambda match: (
                -match.score,
                -match.document.updated_at.timestamp(),
                match.document.memory.title.casefold(),
                str(match.document.id),
            ),
        )
        if len(matches) >= data.limit:
            return SearchResult(matches=tuple(matches[: data.limit]))

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
        if embedding_result is not None:
            query_vector = list(embedding_result.embedding)
            matches = list(matches) + semantic_retrieval_matches(documents, matches, query_vector)

        return SearchResult(matches=tuple(matches[: data.limit]))

    def _resolve_team(self, data: SearchInput, scope: EffectiveScope) -> Team | None:
        selected_team_id = data.team_id
        if selected_team_id is None and len(scope.team_ids) == 1:
            selected_team_id = scope.team_ids[0]
        if selected_team_id is None:
            return None

        return Team.objects.get(organization_id=scope.organization_id, id=selected_team_id)
