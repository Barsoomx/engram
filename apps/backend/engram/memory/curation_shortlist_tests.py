from __future__ import annotations

from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    CandidateStatus,
    CurationDecision,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryStatus,
    MemoryTransition,
    MemoryVersion,
    Team,
    VisibilityScope,
)
from engram.memory.deterministic_gates import EffectiveCandidateScope
from engram.memory.projections import write_exact_memory_projection
from engram.memory.transitions_test_support import (
    candidate_in_scope,
    open_single_conflict,
    provenanced_candidate,
    provenanced_candidate_in_scope,
    transition_request,
    transitions_module,
)


def _shortlist_module() -> object:
    from engram.memory import curation_shortlist

    return curation_shortlist


def _input(
    organization_id: UUID,
    project_id: UUID,
    scope: EffectiveCandidateScope,
    *,
    embedding: tuple[float, ...] | None = None,
    terms: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    title: str = 'candidate title',
    body: str = 'candidate body',
) -> object:
    module = _shortlist_module()
    return module.BuildCurationShortlistInput(
        organization_id=organization_id,
        project_id=project_id,
        effective_scope=scope,
        title=title,
        body=body,
        query_embedding=embedding,
        exact_terms=terms,
        symbols=symbols,
    )


def _semantic_snapshot(project_id: UUID) -> dict[str, tuple[object, ...]]:
    return {
        'decisions': tuple(
            CurationDecision.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'outcome', 'reason_code', 'target_memory_version_id')
        ),
        'transitions': tuple(
            MemoryTransition.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'transition_type', 'memory_id', 'from_version_id', 'to_version_id')
        ),
        'conflicts': tuple(
            MemoryConflict.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list(
                'id',
                'memory_id',
                'candidate_id',
                'resolved_transition_id',
                'resolution',
                'resolved_at',
            )
        ),
        'candidates': tuple(
            MemoryCandidate.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'status', 'promoted_memory_id')
        ),
        'memories': tuple(
            Memory.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'status', 'stale', 'refuted', 'current_transition_id', 'current_version')
        ),
        'versions': tuple(
            MemoryVersion.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'memory_id', 'version', 'content_hash')
        ),
    }


def _write_projection(memory: object, version: object, transition_id: UUID, *, full_text: str) -> object:
    version.source_metadata = {
        'exact_terms': ['term'],
        'symbols': ['Symbol.method'],
        'full_text': full_text,
    }
    version.save(update_fields=['source_metadata', 'updated_at'])
    with transaction.atomic():
        return write_exact_memory_projection(
            memory=memory,
            version=version,
            transition_id=transition_id,
            sources=list(version.provenance_sources.all()),
        )


def _promote(suffix: str) -> tuple[object, object, object, object, object, object]:
    candidate, source, scope = provenanced_candidate(suffix)
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    document = result.retrieval_document
    document = _write_projection(
        result.memory,
        result.memory_version,
        result.transition.id,
        full_text=f'{document.memory.title}\n\n{document.memory.body}',
    )
    return scope, result.memory, result.memory_version, document, candidate, source


def _embedding(seed: int) -> tuple[float, ...]:
    values = [0.0] * 1536
    values[seed % len(values)] = 1.0
    return tuple(values)


def _set_embedding(document: object, seed: int) -> None:
    vector = _embedding(seed)
    document.embedding_vector = list(vector)
    if hasattr(document, 'embedding_pgvector'):
        document.embedding_pgvector = list(vector)
    document.embedding_reference = f'test://embedding/{seed}'
    document.embedding_projection_hash = document.exact_projection_hash
    document.projection_contract_version = 1
    document.embedding_projected_at = timezone.now()
    update_fields = [
        'embedding_vector',
        'embedding_reference',
        'embedding_projection_hash',
        'exact_projection_hash',
        'projection_contract_version',
        'embedding_projected_at',
        'updated_at',
    ]
    if hasattr(document, 'embedding_pgvector'):
        update_fields.append('embedding_pgvector')
    document.save(update_fields=update_fields)


@pytest.mark.django_db
def test_pgvector_shortlist_authorizes_before_distance_ordering() -> None:
    scope, memory, _version, document, base_candidate, base_source = _promote('shortlist-authorized')
    _set_embedding(document, 0)
    foreign_team = Team.objects.create(
        organization_id=scope[0].id,
        name='Foreign shortlist team',
        slug='foreign-shortlist-team',
    )
    scope[1].team_links.create(organization_id=scope[0].id, team=foreign_team)
    foreign_candidate, foreign_source, _foreign_session = provenanced_candidate_in_scope(
        scope[0],
        scope[1],
        foreign_team,
        suffix='shortlist-foreign-closer',
        title='Foreign closer candidate',
        body='Foreign closer body',
        visibility_scope=VisibilityScope.TEAM,
    )
    foreign_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(foreign_candidate))
    foreign_document = foreign_result.retrieval_document
    foreign_document = _write_projection(
        foreign_result.memory,
        foreign_result.memory_version,
        foreign_result.transition.id,
        full_text='Foreign closer body',
    )
    _set_embedding(foreign_document, 0)
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(
            scope[0].id,
            scope[1].id,
            EffectiveCandidateScope(VisibilityScope.PROJECT, None),
            embedding=_embedding(0),
        ),
    )

    assert [entry.memory_id for entry in result.entries] == [memory.id]
    assert foreign_result.memory.id not in {entry.memory_id for entry in result.entries}
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_shortlist_enforces_three_bounded_legs_union_cap_and_tie_order() -> None:
    scope, base_memory, _version, _document, base_candidate, base_source = _promote('shortlist-bounds-0')
    _set_embedding(_document, 1)
    for index in range(1, 14):
        candidate, source = candidate_in_scope(
            base_candidate,
            base_source,
            title=f'Bounded candidate {index}',
            body=f'Bounded body {index}',
        )
        result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
        document = result.retrieval_document
        document = _write_projection(
            result.memory,
            result.memory_version,
            result.transition.id,
            full_text=f'term Symbol.method bounded body {index}',
        )
        _set_embedding(document, 1)

    before = _semantic_snapshot(scope[1].id)
    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(
            scope[0].id,
            scope[1].id,
            EffectiveCandidateScope(VisibilityScope.PROJECT, None),
            embedding=_embedding(1),
            terms=('term',),
            symbols=('Symbol.method',),
        ),
    )

    assert len([entry for entry in result.entries if entry.vector_distance is not None]) <= 8
    assert len([entry for entry in result.entries if entry.lexical_rank is not None]) <= 4
    assert len([entry for entry in result.entries if entry.exact_overlap]) <= 4
    assert len(result.entries) <= 12
    assert len({entry.memory_version_id for entry in result.entries}) == len(result.entries)
    expected_order = sorted(
        result.entries,
        key=lambda entry: (
            -entry.exact_overlap,
            entry.vector_distance is None,
            entry.vector_distance if entry.vector_distance is not None else 0.0,
            -(entry.lexical_rank or 0.0),
            str(entry.memory_version_id),
        ),
    )
    assert list(result.entries) == expected_order
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('visibility', 'team_case', 'authorized'),
    [
        (VisibilityScope.PROJECT, None, True),
        (VisibilityScope.TEAM, 'same', True),
        (VisibilityScope.TEAM, 'other', False),
        (VisibilityScope.SESSION, None, False),
        (VisibilityScope.ORGANIZATION, None, False),
    ],
)
def test_shortlist_applies_effective_visibility_and_excludes_inactive_rows(
    visibility: str,
    team_case: str | None,
    authorized: bool,
) -> None:
    scope, memory, version, document, _candidate, _source = _promote('shortlist-visibility')
    memory.visibility_scope = visibility
    effective_scope = EffectiveCandidateScope(VisibilityScope.PROJECT, None)
    if visibility == VisibilityScope.TEAM:
        effective_scope = EffectiveCandidateScope(VisibilityScope.TEAM, scope[2].team_id)
        if team_case == 'other':
            other_team = Team.objects.create(
                organization_id=scope[0].id,
                name='Other visibility team',
                slug='other-visibility-team',
            )
            scope[1].team_links.create(organization_id=scope[0].id, team=other_team)
            memory.team_id = other_team.id
            document.team_id = other_team.id
        document.visibility_scope = VisibilityScope.TEAM
    else:
        document.visibility_scope = visibility
    memory.save(update_fields=['visibility_scope', 'team_id', 'updated_at'])
    document = _write_projection(memory, version, memory.current_transition_id, full_text=document.full_text)
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(
            scope[0].id,
            scope[1].id,
            effective_scope,
            embedding=_embedding(2),
        ),
    )

    assert (memory.id in {entry.memory_id for entry in result.entries}) is authorized
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
@pytest.mark.parametrize('field', ['stale', 'refuted'])
def test_shortlist_excludes_stale_and_refuted_current_memories(field: str) -> None:
    scope, memory, version, document, _candidate, _source = _promote(f'shortlist-{field}')
    setattr(memory, field, True)
    setattr(document, field, True)
    memory.save(update_fields=[field, 'updated_at'])
    document = _write_projection(memory, version, memory.current_transition_id, full_text=document.full_text)
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
    )

    assert result.entries == ()
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_shortlist_rejects_missing_or_incoherent_cp4_projection_without_semantic_writes() -> None:
    scope, memory, _version, document, _candidate, _source = _promote('shortlist-projection-fence')
    document.projection_contract_version = 0
    document.exact_projection_hash = ''
    document.save(update_fields=['projection_contract_version', 'exact_projection_hash', 'updated_at'])
    before = _semantic_snapshot(scope[1].id)

    with pytest.raises(ValueError) as error:
        _shortlist_module().BuildCurationShortlist.execute(
            _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
        )

    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    assert Memory.objects.get(id=memory.id).current_transition_id is not None
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_document_memory_scope_mismatch_is_operationally_fenced_without_writes() -> None:
    scope, memory, _version, document, _candidate, _source = _promote('shortlist-scope-mismatch')
    other_team = Team.objects.create(
        organization_id=scope[0].id,
        name='Mismatch team',
        slug='mismatch-team',
    )
    scope[1].team_links.create(organization_id=scope[0].id, team=other_team)
    document.team_id = other_team.id
    document.save(update_fields=['team_id', 'updated_at'])
    before = _semantic_snapshot(scope[1].id)

    with pytest.raises(ValueError) as error:
        _shortlist_module().BuildCurationShortlist.execute(
            _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
        )

    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    memory.refresh_from_db()
    assert memory.team_id == scope[2].team_id
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_shortlist_manifest_hash_is_replay_stable_and_fences_transition_changes() -> None:
    scope, memory, version, _document, candidate, source = _promote('shortlist-manifest')
    data = _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None))
    first = _shortlist_module().BuildCurationShortlist.execute(data)
    replay = _shortlist_module().BuildCurationShortlist.execute(data)

    assert first.manifest_hash == replay.manifest_hash
    assert first.entries == replay.entries
    transitions = transitions_module()
    revised = transitions.ReviseMemory().execute(
        transitions.ReviseMemoryInput(
            request=transition_request(candidate, key=f'revise:{memory.id}:v2').request,
            memory_fence=transitions.build_memory_fence(memory),
            title='new generation',
            body='new generation body',
        ),
    )
    _write_projection(
        revised.memory,
        revised.memory_version,
        revised.transition.id,
        full_text='new generation\n\nnew generation body',
    )
    assert version.id in {entry.memory_version_id for entry in first.entries}
    changed = _shortlist_module().BuildCurationShortlist.execute(data)
    assert changed.entries[0].memory_version_id == revised.memory_version.id
    assert changed.entries[0].current_transition_id != first.entries[0].current_transition_id
    assert changed.manifest_hash != first.manifest_hash
    assert _semantic_snapshot(scope[1].id)['decisions'] == ()


@pytest.mark.django_db
def test_missing_embedding_on_nonempty_scope_retries_without_publication() -> None:
    scope, _memory, _version, document, _candidate, _source = _promote('shortlist-no-embedding')
    document.embedding_vector = []
    document.embedding_reference = ''
    document.embedding_projection_hash = ''
    document.embedding_projected_at = None
    document.save(
        update_fields=[
            'embedding_vector',
            'embedding_reference',
            'embedding_projection_hash',
            'embedding_projected_at',
            'updated_at',
        ],
    )
    before = _semantic_snapshot(scope[1].id)

    with pytest.raises(ValueError) as error:
        _shortlist_module().BuildCurationShortlist.execute(
            _input(
                scope[0].id,
                scope[1].id,
                EffectiveCandidateScope(VisibilityScope.PROJECT, None),
                title='',
                body='',
            ),
        )

    assert getattr(error.value, 'code', None) == 'embedding_unavailable'
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
@pytest.mark.parametrize('status', [MemoryStatus.ARCHIVED, MemoryStatus.REFUTED])
def test_shortlist_excludes_archived_and_status_refuted_memories(status: str) -> None:
    scope, memory, version, document, _candidate, _source = _promote(f'shortlist-status-{status}')
    memory.status = status
    memory.save(update_fields=['status', 'updated_at'])
    _write_projection(memory, version, memory.current_transition_id, full_text=document.full_text)
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
    )

    assert result.entries == ()
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_query_embedding_with_unembedded_corpus_retries_without_blind_completion() -> None:
    scope, _memory, _version, _document, _candidate, _source = _promote('shortlist-corpus-no-embedding')
    before = _semantic_snapshot(scope[1].id)

    with pytest.raises(ValueError) as error:
        _shortlist_module().BuildCurationShortlist.execute(
            _input(
                scope[0].id,
                scope[1].id,
                EffectiveCandidateScope(VisibilityScope.PROJECT, None),
                embedding=_embedding(7),
                title='',
                body='',
            ),
        )

    assert getattr(error.value, 'code', None) == 'embedding_unavailable'
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_zero_authorized_corpus_returns_empty_complete_manifest_without_embedding() -> None:
    candidate, _source, scope = provenanced_candidate('shortlist-zero-corpus')
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
    )

    assert result.entries == ()
    assert result.authorized_corpus_count == 0
    assert result.comparison_complete is True
    assert candidate.status == CandidateStatus.PROPOSED
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_project_visible_row_with_retained_team_provenance_remains_authorized() -> None:
    scope, memory, version, document, _candidate, _source = _promote('shortlist-project-retained-team')
    memory.visibility_scope = VisibilityScope.PROJECT
    document.visibility_scope = VisibilityScope.PROJECT
    document.team_id = scope[2].team_id
    memory.save(update_fields=['visibility_scope', 'updated_at'])
    _write_projection(memory, version, memory.current_transition_id, full_text=document.full_text)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(scope[0].id, scope[1].id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
    )

    assert [entry.memory_id for entry in result.entries] == [memory.id]
    assert result.entries[0].team_id == scope[2].team_id


@pytest.mark.django_db
def test_shortlist_tags_open_conflicts_without_authorizing_destruction() -> None:
    candidate, conflict = open_single_conflict('shortlist-conflict-tag')
    memory = conflict.memory
    before = _semantic_snapshot(candidate.project_id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(candidate.organization_id, candidate.project_id, EffectiveCandidateScope(VisibilityScope.PROJECT, None)),
    )

    tagged = [entry for entry in result.entries if entry.memory_id == memory.id]
    assert tagged and tagged[0].has_open_conflict is True
    assert _semantic_snapshot(candidate.project_id) == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('terms', 'symbols'),
    [((), ()), (('term', 'TERM', ' term '), ()), ((), ('Symbol.method', 'symbol.method'))],
)
def test_empty_and_duplicate_term_symbol_inputs_are_bounded_before_query(
    terms: tuple[str, ...],
    symbols: tuple[str, ...],
) -> None:
    scope, _memory, _version, _document, _candidate, _source = _promote('shortlist-inputs')
    before = _semantic_snapshot(scope[1].id)

    result = _shortlist_module().BuildCurationShortlist.execute(
        _input(
            scope[0].id,
            scope[1].id,
            EffectiveCandidateScope(VisibilityScope.PROJECT, None),
            terms=terms,
            symbols=symbols,
        ),
    )

    if not terms and not symbols:
        assert result.entries
        assert all(entry.exact_overlap == 0 for entry in result.entries)
    else:
        assert result.entries
        assert result.entries[0].exact_overlap > 0 or result.entries[0].lexical_rank is not None
    assert len(result.entries) <= 12
    if not terms and not symbols:
        assert result.comparison_complete is False
    assert _semantic_snapshot(scope[1].id) == before


@pytest.mark.django_db
def test_revalidation_detects_selected_vector_rank_and_membership_change() -> None:
    module = _shortlist_module()
    scope, memory, _version, document, _candidate, _source = _promote('revalidate-vector-rank')
    _set_embedding(document, 0)
    data = _input(
        scope[0].id,
        scope[1].id,
        EffectiveCandidateScope(VisibilityScope.PROJECT, None),
        embedding=_embedding(0),
        title='',
        body='',
    )
    frozen = module.BuildCurationShortlist.execute(data)
    frozen_transition_id = memory.current_transition_id
    assert [entry.memory_id for entry in frozen.entries] == [memory.id]

    _set_embedding(document, 1)
    rebuilt = module.BuildCurationShortlist.execute(data)
    memory.refresh_from_db()

    assert rebuilt.entries == ()
    assert rebuilt.manifest_hash != frozen.manifest_hash
    assert memory.current_transition_id == frozen_transition_id
    assert module.revalidate_curation_shortlist(data, frozen) is False


@pytest.mark.django_db
def test_revalidation_detects_selected_projection_becoming_incoherent() -> None:
    module = _shortlist_module()
    scope, memory, _version, document, _candidate, _source = _promote('revalidate-projection')
    data = _input(
        scope[0].id,
        scope[1].id,
        EffectiveCandidateScope(VisibilityScope.PROJECT, None),
        terms=('term',),
        title='',
        body='',
    )
    frozen = module.BuildCurationShortlist.execute(data)
    frozen_transition_id = memory.current_transition_id
    assert [entry.memory_id for entry in frozen.entries] == [memory.id]

    document.projection_contract_version = 0
    document.save(update_fields=['projection_contract_version', 'updated_at'])

    with pytest.raises(ValueError) as error:
        module.BuildCurationShortlist.execute(data)

    memory.refresh_from_db()
    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    assert memory.current_transition_id == frozen_transition_id
    assert module.revalidate_curation_shortlist(data, frozen) is False


@pytest.mark.django_db
def test_revalidation_detects_selected_open_conflict_tag_change() -> None:
    from engram.memory.transitions_test_support import candidate_fence_for, transition_request_for

    module = _shortlist_module()
    scope, memory, _version, _document, base_candidate, base_source = _promote('revalidate-conflict')
    data = _input(
        scope[0].id,
        scope[1].id,
        EffectiveCandidateScope(VisibilityScope.PROJECT, None),
        terms=('term',),
        title='',
        body='',
    )
    frozen = module.BuildCurationShortlist.execute(data)
    frozen_transition_id = memory.current_transition_id
    assert frozen.entries[0].has_open_conflict is False

    competing, _source = candidate_in_scope(
        base_candidate,
        base_source,
        title='Competing supported claim',
        body='A competing claim now requires conflict handling.',
    )
    transitions = transitions_module()
    transitions.OpenMemoryConflict().execute(
        transitions.OpenMemoryConflictInput(
            request=transition_request_for(
                competing,
                key=f'revalidate-conflict:{competing.id}:v1',
            ),
            candidate_fence=candidate_fence_for(competing),
            memory_fence=transitions.build_memory_fence(memory),
            evidence_hash='e' * 64,
            redacted_reason='concurrent conflict',
        )
    )

    rebuilt = module.BuildCurationShortlist.execute(data)
    memory.refresh_from_db()
    assert rebuilt.entries[0].has_open_conflict is True
    assert rebuilt.manifest_hash != frozen.manifest_hash
    assert memory.current_transition_id == frozen_transition_id
    assert module.revalidate_curation_shortlist(data, frozen) is False


@pytest.mark.django_db
def test_revalidation_detects_transition_of_previously_unselected_memory() -> None:
    module = _shortlist_module()
    scope, selected, _version, _document, base_candidate, base_source = _promote('revalidate-unselected')
    other_candidate, _other_source = candidate_in_scope(
        base_candidate,
        base_source,
        title='Dormant comparison',
        body='This comparison is initially outside the exact leg.',
    )
    transitions = transitions_module()
    other_result = transitions.PromoteMemoryCandidate().execute(transition_request(other_candidate))
    other_version = other_result.memory_version
    other_version.source_metadata = {
        'exact_terms': ['other'],
        'symbols': [],
        'full_text': 'dormant comparison',
    }
    other_version.save(update_fields=['source_metadata', 'updated_at'])
    with transaction.atomic():
        write_exact_memory_projection(
            memory=other_result.memory,
            version=other_version,
            transition_id=other_result.memory.current_transition_id,
            sources=list(other_version.provenance_sources.all()),
        )

    data = _input(
        scope[0].id,
        scope[1].id,
        EffectiveCandidateScope(VisibilityScope.PROJECT, None),
        terms=('term',),
        title='',
        body='',
    )
    frozen = module.BuildCurationShortlist.execute(data)
    selected_transition_id = selected.current_transition_id
    assert [entry.memory_id for entry in frozen.entries] == [selected.id]
    assert frozen.authorized_corpus_count == 2

    revised = transitions.ReviseMemory().execute(
        transitions.ReviseMemoryInput(
            request=transition_request(
                other_candidate,
                key=f'revalidate-unselected:{other_result.memory.id}:v2',
            ).request,
            memory_fence=transitions.build_memory_fence(other_result.memory),
            title='Newly relevant comparison',
            body='The current comparison now matches the exact shortlist leg.',
        )
    )
    _write_projection(
        revised.memory,
        revised.memory_version,
        revised.transition.id,
        full_text='newly relevant comparison',
    )

    rebuilt = module.BuildCurationShortlist.execute(data)
    selected.refresh_from_db()
    assert other_result.memory.id in {entry.memory_id for entry in rebuilt.entries}
    assert rebuilt.manifest_hash != frozen.manifest_hash
    assert rebuilt.authorized_corpus_count == frozen.authorized_corpus_count
    assert selected.current_transition_id == selected_transition_id
    assert module.revalidate_curation_shortlist(data, frozen) is False
