from __future__ import annotations

import uuid

import pytest

from engram.context.context_api_tests import RAW_KEY, create_embedding_policy, create_project_scope
from engram.core.models import (
    Memory,
    MemoryConflict,
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
from engram.memory.transitions import (
    OpenMemoryConflict,
    OpenMemoryConflictInput,
    PromoteMemoryCandidate,
    build_memory_fence,
)
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    candidate_in_scope,
    provenanced_candidate_in_scope,
    transition_request,
    transition_request_for,
)
from engram.search.search_api_tests import grant_search_capability
from engram.search.services import SearchInput, SearchMemories

pytestmark_pgvector = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def _search_input(project: Project, **overrides: object) -> SearchInput:
    defaults: dict[str, object] = {
        'raw_key': RAW_KEY,
        'project_id': project.id,
        'team_id': None,
        'query': 'authorization',
        'file_paths': (),
        'symbols': (),
        'limit': 5,
        'request_id': f'search-test-{uuid.uuid4()}',
        'correlation_id': 'search-test-correlation',
    }
    defaults.update(overrides)

    return SearchInput(**defaults)


def _seed_lexical_recall_document(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    title: str,
    body: str,
    exact_terms: list[str],
    sequence: int,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'search-recall-hash-{sequence}',
    )

    return RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        source_observation_ids=[],
        file_paths=[],
        symbols=[],
        exact_terms=exact_terms,
        full_text=f'{title}\n\n{body}',
    )


@pytest.mark.django_db
def test_search_flag_off_lexical_recall_is_byte_identical() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    anchor = _seed_lexical_recall_document(
        organization,
        team,
        project,
        title='Authorization anchor',
        body='Authorization anchor',
        exact_terms=['authorization'],
        sequence=1,
    )
    fuzzy = _seed_lexical_recall_document(
        organization,
        team,
        project,
        title='authorisation',
        body='authorisation',
        exact_terms=[],
        sequence=2,
    )

    result = SearchMemories().execute(_search_input(project, query='authorization'))

    assert [match.document.id for match in result.matches] == [anchor.id]
    assert result.matches[0].inclusion_reason == 'exact match: authorization'
    assert fuzzy.id not in {match.document.id for match in result.matches}


@pytestmark_pgvector
@pytest.mark.django_db
def test_search_lexical_recall_surfaces_lexical_only_match_when_enabled() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    create_embedding_policy(organization, team, project)
    OrganizationSettings.objects.create(organization=organization, lexical_recall_enabled=True)
    anchor = _seed_lexical_recall_document(
        organization,
        team,
        project,
        title='Authorization anchor',
        body='Authorization anchor',
        exact_terms=['authorization'],
        sequence=1,
    )
    fuzzy = _seed_lexical_recall_document(
        organization,
        team,
        project,
        title='authorisation',
        body='authorisation',
        exact_terms=[],
        sequence=2,
    )

    result = SearchMemories().execute(_search_input(project, query='authorization'))

    matched_ids = {match.document.id for match in result.matches}
    assert anchor.id in matched_ids
    assert fuzzy.id in matched_ids
    fuzzy_match = next(match for match in result.matches if match.document.id == fuzzy.id)
    assert fuzzy_match.inclusion_reason.startswith('lexical match:')


# C5.4 unresolved-conflict search exclusion (RED) -----------------------------


def _seed_conflicted_and_clean_memories(
    organization: Organization,
    team: Team,
    project: Project,
) -> tuple[Memory, Memory]:
    clean_candidate, _clean_source, _clean_session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='search-clean',
        title='authorization',
        body='authorization ranking clean claim body',
    )
    clean_memory = PromoteMemoryCandidate().execute(transition_request(clean_candidate)).memory

    base, base_source, _base_session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix='search-conflicted',
        title='authorization',
        body='authorization ranking conflicted claim body',
    )
    conflicted_memory = PromoteMemoryCandidate().execute(transition_request(base)).memory

    opponent, _opponent_source = candidate_in_scope(
        base,
        base_source,
        title='authorization opponent claim',
        body='authorization opponent claim body',
    )
    OpenMemoryConflict().execute(
        OpenMemoryConflictInput(
            request=transition_request_for(
                opponent,
                key=f'request:{uuid.uuid4()}:conflict-open:{opponent.id}:v1',
            ),
            candidate_fence=candidate_fence_for(opponent),
            memory_fence=build_memory_fence(conflicted_memory),
            evidence_hash='e' * 64,
            redacted_reason='search conflict evidence',
        )
    )

    return clean_memory, conflicted_memory


@pytest.mark.django_db
def test_search_excludes_memory_with_unresolved_conflict() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_search_capability(RAW_KEY)
    clean_memory, conflicted_memory = _seed_conflicted_and_clean_memories(organization, team, project)

    assert MemoryConflict.objects.filter(memory=conflicted_memory, resolved_transition__isnull=True).exists()

    result = SearchMemories().execute(_search_input(project, query='authorization'))

    matched_memory_ids = {match.document.memory_id for match in result.matches}
    assert clean_memory.id in matched_memory_ids
    assert conflicted_memory.id not in matched_memory_ids
