from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q

from engram.access.services import EffectiveScope
from engram.core.models import (
    CandidateStatus,
    LinkType,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    Organization,
    Project,
    RetrievalDocument,
)
from engram.memory.conflict_links import CONFLICT_CANDIDATE_TARGET_PREFIX

if TYPE_CHECKING:
    from engram.context.services import RetrievalMatch


@dataclass(frozen=True)
class RetrievalWarning:
    code: str
    message: str
    memory_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {'code': self.code, 'message': self.message, 'memory_id': self.memory_id}


STALE_REFUTED_WARNING_CAP = 3
STALE_REFUTED_MIN_SCORE = 60
CONFLICTING_MEMORY_WARNING_CAP = 5


def budget_dropped_warning(dropped_count: int) -> RetrievalWarning | None:
    if dropped_count <= 0:
        return None

    return RetrievalWarning(
        code='budget_dropped',
        message=f'{dropped_count} matching memories dropped for token budget',
    )


def semantic_unavailable_warning(unavailable: bool) -> RetrievalWarning | None:
    if not unavailable:
        return None

    return RetrievalWarning(
        code='semantic_unavailable',
        message='semantic retrieval unavailable: embedding could not be resolved',
    )


def semantic_retrieval_gap(has_request_terms: bool, exact_matches: list[RetrievalMatch]) -> bool:
    return has_request_terms and not exact_matches


def stale_and_refuted_warnings(
    organization: Organization,
    project: Project,
    scope: EffectiveScope,
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    has_request_terms: bool,
    kinds: tuple[str, ...] = (),
) -> list[RetrievalWarning]:
    from engram.context.services import filter_documents_by_team_visibility, redact_text, score_retrieval_document

    if not has_request_terms:
        return []

    documents = RetrievalDocument.objects.filter(organization=organization, project=project).filter(
        Q(memory__stale=True) | Q(memory__refuted=True) | Q(memory__status=MemoryStatus.REFUTED),
    )
    if kinds:
        documents = documents.filter(memory__kind__in=kinds)
    documents = documents.select_related('memory')[:200]
    authorized_documents = filter_documents_by_team_visibility(documents, scope)

    warnings: list[RetrievalWarning] = []
    seen_memory_ids: set[uuid.UUID] = set()
    for document in authorized_documents:
        if len(warnings) >= STALE_REFUTED_WARNING_CAP:
            break
        if document.memory_id in seen_memory_ids:
            continue

        match = score_retrieval_document(document, query, file_paths, symbols, has_request_terms)
        if match is None or match.score < STALE_REFUTED_MIN_SCORE:
            continue

        memory = document.memory
        seen_memory_ids.add(memory.id)
        if memory.refuted or memory.status == MemoryStatus.REFUTED:
            warnings.append(
                RetrievalWarning(
                    code='refuted_match',
                    message=f'refuted memory matched: "{redact_text(memory.title)}"',
                    memory_id=str(memory.id),
                ),
            )
        else:
            warnings.append(
                RetrievalWarning(
                    code='stale_match',
                    message=f'stale memory matched: "{redact_text(memory.title)}"',
                    memory_id=str(memory.id),
                ),
            )

    return warnings


def _conflict_candidate_id(target: str) -> uuid.UUID | None:
    if not target.startswith(CONFLICT_CANDIDATE_TARGET_PREFIX):
        return None

    raw_id = target[len(CONFLICT_CANDIDATE_TARGET_PREFIX) :]
    try:
        return uuid.UUID(raw_id)
    except ValueError:
        return None


def conflicting_memory_warnings(included_matches: tuple[RetrievalMatch, ...]) -> list[RetrievalWarning]:
    memory_ids = [match.document.memory_id for match in included_matches]
    if not memory_ids:
        return []

    candidate_id_by_memory_id: dict[uuid.UUID, uuid.UUID] = {}
    for link in MemoryLink.objects.filter(memory_id__in=memory_ids, link_type=LinkType.CONFLICTS_WITH):
        candidate_id = _conflict_candidate_id(link.target)
        if candidate_id is not None:
            candidate_id_by_memory_id.setdefault(link.memory_id, candidate_id)
    if not candidate_id_by_memory_id:
        return []

    proposed_candidate_ids = set(
        MemoryCandidate.objects.filter(
            id__in=candidate_id_by_memory_id.values(),
            status=CandidateStatus.PROPOSED,
        ).values_list('id', flat=True),
    )
    if not proposed_candidate_ids:
        return []

    warnings: list[RetrievalWarning] = []
    seen_memory_ids: set[uuid.UUID] = set()
    for memory_id in memory_ids:
        if len(warnings) >= CONFLICTING_MEMORY_WARNING_CAP:
            break
        if memory_id in seen_memory_ids:
            continue

        candidate_id = candidate_id_by_memory_id.get(memory_id)
        if candidate_id is None or candidate_id not in proposed_candidate_ids:
            continue

        seen_memory_ids.add(memory_id)
        warnings.append(
            RetrievalWarning(
                code='conflicting_memory',
                message='memory has an unresolved contradiction claim',
                memory_id=str(memory_id),
            ),
        )

    return warnings


def compute_retrieval_warnings(
    *,
    organization: Organization,
    project: Project,
    scope: EffectiveScope,
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    has_request_terms: bool,
    included_matches: tuple[RetrievalMatch, ...],
    semantic_unavailable: bool,
    dropped_for_budget: int = 0,
    kinds: tuple[str, ...] = (),
) -> list[RetrievalWarning]:
    warnings: list[RetrievalWarning] = []
    budget_warning = budget_dropped_warning(dropped_for_budget)
    if budget_warning is not None:
        warnings.append(budget_warning)

    semantic_warning = semantic_unavailable_warning(semantic_unavailable)
    if semantic_warning is not None:
        warnings.append(semantic_warning)

    warnings.extend(
        stale_and_refuted_warnings(
            organization,
            project,
            scope,
            query,
            file_paths,
            symbols,
            has_request_terms,
            kinds,
        ),
    )
    warnings.extend(conflicting_memory_warnings(included_matches))

    return warnings
