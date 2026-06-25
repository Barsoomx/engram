from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from engram.access.services import EffectiveScope, ResolveApiKeyScope
from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    AuditResult,
    ContextBundle,
    ContextBundleItem,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.core.redaction import redact_value


class ContextIndexError(Exception):
    pass


@dataclass(frozen=True)
class IndexMemoryVersionInput:
    memory_version_id: uuid.UUID


@dataclass(frozen=True)
class IndexMemoryVersionResult:
    retrieval_document: RetrievalDocument
    created: bool


@dataclass(frozen=True)
class ContextBundleInput:
    raw_key: str
    project_id: uuid.UUID
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

        return {
            'status': self.bundle.status,
            'request_id': self.bundle.request_id,
            'context_bundle_id': str(self.bundle.id),
            'purpose': self.bundle.purpose,
            'rendered_context': rendered_context,
            'hook_specific_output': hook_specific_output,
            'items': [self._item_response(match) for match in self.matches],
            'warnings': [],
        }

    def _item_response(self, match: RetrievalMatch) -> dict[str, object]:
        document = match.document
        memory = document.memory

        return {
            'citation': self._citation_for(document),
            'memory_id': str(memory.id),
            'memory_version_id': str(document.memory_version_id),
            'retrieval_document_id': str(document.id),
            'title': memory.title,
            'body': memory.body,
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


class IndexMemoryVersion:
    def execute(self, data: IndexMemoryVersionInput) -> IndexMemoryVersionResult:
        version = MemoryVersion.objects.select_related(
            'memory',
            'source_observation',
            'organization',
            'project',
        ).get(id=data.memory_version_id)
        memory = version.memory
        if memory.status != MemoryStatus.APPROVED:
            raise ContextIndexError('Only approved memory can be indexed')

        observation = version.source_observation
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        file_paths = unique_text_values(
            metadata.get('file_paths', []),
            observation.files_read if observation is not None else [],
            observation.files_modified if observation is not None else [],
        )
        symbols = unique_text_values(metadata.get('symbols', []))
        exact_terms = list(
            normalize_lookup_values(
                [
                    *metadata.get('exact_terms', []),
                    memory.title,
                ],
            ),
        )
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

        return IndexMemoryVersionResult(retrieval_document=retrieval_document, created=created)


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
        project = Project.objects.get(organization=organization, id=data.project_id)
        existing_bundle = self._existing_bundle(organization, project, data.request_id)
        if existing_bundle is not None:
            return self._result_from_bundle(existing_bundle)

        team = self._resolve_team(organization, data.team_id, scope)
        agent = self._get_or_create_agent(organization, data)
        session = self._get_or_create_session(organization, project, team, agent, data)
        matches = self._rank_matches(
            self._authorized_documents(organization, project, scope),
            data,
        )
        query_result = redact_value(data.query)
        metadata = {'retrieval_strategy': 'exact'}
        if query_result.redacted:
            metadata['redaction'] = {'query_text': True}

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
                selected_count=len(matches),
                metadata=metadata,
            )
            persisted_matches = self._create_items(bundle, matches)
            bundle.rendered_text = self._render_context(persisted_matches)
            bundle.selected_count = len(persisted_matches)
            bundle.save(update_fields=['rendered_text', 'selected_count', 'updated_at'])
            self._audit_retrieval(bundle, persisted_matches, scope, data)

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
        authorized = []
        allowed_team_ids = set(scope.team_ids)
        for document in documents:
            if document.visibility_scope == VisibilityScope.PROJECT:
                authorized.append(document)
            elif document.visibility_scope == VisibilityScope.TEAM and document.team_id in allowed_team_ids:
                authorized.append(document)

        return tuple(authorized)

    def _rank_matches(
        self,
        documents: tuple[RetrievalDocument, ...],
        data: ContextBundleInput,
    ) -> tuple[RetrievalMatch, ...]:
        matches = []
        has_request_terms = bool(data.query.strip() or data.file_paths or data.symbols)
        for document in documents:
            match = self._score_document(document, data, has_request_terms)
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

        return tuple(matches[: data.limit])

    def _score_document(
        self,
        document: RetrievalDocument,
        data: ContextBundleInput,
        has_request_terms: bool,
    ) -> RetrievalMatch | None:
        document_file_paths = tuple(str(value) for value in document.file_paths)
        file_match = first_path_match(data.file_paths, document_file_paths)
        if file_match:
            return RetrievalMatch(
                document=document,
                score=100,
                matched_terms=(file_match,),
                inclusion_reason=f'exact match: {file_match}',
            )

        document_symbols = tuple(str(value) for value in document.symbols)
        symbol_match = first_exact_match(data.symbols, document_symbols)
        if symbol_match:
            return RetrievalMatch(
                document=document,
                score=80,
                matched_terms=(symbol_match,),
                inclusion_reason=f'exact match: {symbol_match}',
            )

        query_terms = request_query_terms(data.query)
        exact_match = first_contains_match(query_terms, tuple(str(value) for value in document.exact_terms))
        if exact_match:
            return RetrievalMatch(
                document=document,
                score=60,
                matched_terms=(exact_match,),
                inclusion_reason=f'exact match: {exact_match}',
            )

        full_text_match = first_full_text_match(query_terms, document.full_text)
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
                inclusion_reason=match.inclusion_reason,
                scope_evidence=scope_evidence(match.document),
                metadata={
                    'score': match.score,
                    'matched_terms': list(match.matched_terms),
                },
            )
            persisted.append(match)

        return tuple(persisted)

    def _render_context(self, matches: tuple[RetrievalMatch, ...]) -> str:
        if not matches:
            return '# Engram context\n\nNo approved memory matched this request.'

        lines = ['# Engram context', '']
        for index, match in enumerate(matches, start=1):
            memory = match.document.memory
            lines.append(f'- [M{index}] {memory.title}')
            lines.append(f'  {memory.body}')

        return '\n'.join(lines)

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
    ) -> None:
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
            metadata={
                'selected_count': len(matches),
                'retrieval_strategy': 'exact',
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
                'memory_ids': [str(match.document.memory_id) for match in matches],
                'retrieval_document_ids': [str(match.document.id) for match in matches],
            },
        )


def request_query_terms(query: str) -> tuple[str, ...]:
    query_value = query.strip()
    if not query_value:
        return ()
    terms = [query_value]
    terms.extend(token for token in query_value.replace('/', ' ').split() if len(token.strip()) >= 2)

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
