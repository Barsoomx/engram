from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from engram.access.services import AccessDeniedError, EffectiveScope
from engram.console import search_debug_service
from engram.console.search_debug_service import ReplaySearchDebug
from engram.context.context_api_tests import create_embedding_policy
from engram.core.models import (
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
from engram.memory.digest_visibility_tests import build_legacy_digest, build_proven_weekly_digest
from engram.model_policy.services import EmbeddingCallResult

pytestmark_pgvector = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def _make_scope(
    organization: Organization,
    project: Project,
    *,
    capabilities: tuple[str, ...] = ('memories:read',),
    team_ids: tuple[uuid.UUID, ...] = (),
) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(project.id,),
        team_ids=team_ids,
        capabilities=capabilities,
        actor_type='user',
        actor_id='debug-tester',
        project_bound=False,
    )


def _make_org_project_team() -> tuple[Organization, Project, Team]:
    organization = Organization.objects.create(name='Debug Org', slug='debug-org-search')
    project = Project.objects.create(organization=organization, name='Main', slug='main-search-debug')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform-search-debug')

    return organization, project, team


def _seed_lexical_recall_document(
    organization: Organization,
    project: Project,
    team: Team,
    *,
    title: str,
    body: str,
    exact_terms: list[str],
    sequence: int,
    kind: str = '',
    confidence: Decimal | None = None,
    visibility_scope: str = VisibilityScope.PROJECT,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=visibility_scope,
        metadata={'kind': kind} if kind else {},
        confidence=confidence,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'debug-recall-hash-{sequence}',
    )

    return RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope=visibility_scope,
        source_observation_ids=[],
        file_paths=[],
        symbols=[],
        exact_terms=exact_terms,
        full_text=f'{title}\n\n{body}',
    )


@pytest.mark.django_db
def test_replay_flag_off_lexical_stage_disabled() -> None:
    organization, project, team = _make_org_project_team()
    _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='Authorization anchor',
        body='Authorization anchor',
        exact_terms=['authorization'],
        sequence=1,
    )
    fuzzy = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='authorisation',
        body='authorisation',
        exact_terms=[],
        sequence=2,
    )

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=_make_scope(organization, project),
        query='authorization',
        team_id=None,
        file_paths=(),
        symbols=(),
    )

    assert result.lexical_enabled is False
    assert result.lexical_candidates == []
    assert fuzzy.memory_id not in {m.memory_id for m in result.exact_matches}


@pytestmark_pgvector
@pytest.mark.django_db
def test_replay_lexical_stage_surfaces_lexical_only_match_when_enabled() -> None:
    organization, project, team = _make_org_project_team()
    create_embedding_policy(organization, team, project)
    OrganizationSettings.objects.create(organization=organization, lexical_recall_enabled=True)
    anchor = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='Authorization anchor',
        body='Authorization anchor',
        exact_terms=['authorization'],
        sequence=1,
        kind='decision',
        confidence=Decimal('0.700'),
    )
    fuzzy = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='authorisation',
        body='authorisation',
        exact_terms=[],
        sequence=2,
        kind='gotcha',
        confidence=Decimal('0.910'),
    )

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=_make_scope(organization, project),
        query='authorization',
        team_id=None,
        file_paths=(),
        symbols=(),
    )

    assert result.lexical_enabled is True
    matched_ids = {c.memory_id for c in result.lexical_candidates}
    assert fuzzy.memory_id in matched_ids
    fuzzy_candidate = next(c for c in result.lexical_candidates if c.memory_id == fuzzy.memory_id)
    assert fuzzy_candidate.matched_on.startswith('lexical match:')
    assert fuzzy_candidate.kind == 'gotcha'
    assert fuzzy_candidate.confidence == '0.910'

    anchor_match = next(m for m in result.exact_matches if m.memory_id == anchor.memory_id)
    assert anchor_match.kind == 'decision'
    assert anchor_match.confidence == '0.700'

    packed_by_id = {p.memory_id: p for p in result.packed_context}
    assert packed_by_id[anchor.memory_id].kind == 'decision'
    assert packed_by_id[anchor.memory_id].confidence == '0.700'
    assert packed_by_id[fuzzy.memory_id].kind == 'gotcha'
    assert packed_by_id[fuzzy.memory_id].confidence == '0.910'


@pytestmark_pgvector
@pytest.mark.django_db
def test_replay_semantic_stage_surfaces_kind_and_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, project, team = _make_org_project_team()
    OrganizationSettings.objects.create(organization=organization, hybrid_retrieval_enabled=True)
    semantic_doc = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='semantic only memory',
        body='semantic only memory',
        exact_terms=[],
        sequence=1,
        kind='architecture',
        confidence=Decimal('0.640'),
    )
    semantic_doc.embedding_vector = [1.0, 0.0, 0.0]
    semantic_doc.save(update_fields=['embedding_vector'])

    def m_resolve_query_embedding(*args: object, **kwargs: object) -> EmbeddingCallResult:
        return EmbeddingCallResult(
            provider='fake',
            model='fake-embed',
            call_record_id=uuid.uuid4(),
            redaction_state='none',
            embedding=(1.0, 0.0, 0.0),
        )

    monkeypatch.setattr(search_debug_service, 'resolve_query_embedding', m_resolve_query_embedding)

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=_make_scope(organization, project),
        query='unrelated text',
        team_id=None,
        file_paths=(),
        symbols=(),
    )

    assert result.semantic_enabled is True
    semantic_candidate = next(c for c in result.semantic_candidates if c.memory_id == semantic_doc.memory_id)
    assert semantic_candidate.kind == 'architecture'
    assert semantic_candidate.confidence == '0.640'

    packed_by_id = {p.memory_id: p for p in result.packed_context}
    assert packed_by_id[semantic_doc.memory_id].kind == 'architecture'
    assert packed_by_id[semantic_doc.memory_id].confidence == '0.640'


@pytest.mark.django_db
def test_replay_denies_project_not_in_scope() -> None:
    organization, project, _team = _make_org_project_team()
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-search-debug')
    scope = _make_scope(organization, other_project)

    with pytest.raises(AccessDeniedError) as exc:
        ReplaySearchDebug().execute(
            organization=organization,
            project=project,
            scope=scope,
            query='authorization',
            team_id=None,
            file_paths=(),
            symbols=(),
        )

    assert exc.value.error_code == 'project_scope_denied'
    assert exc.value.status_code == 403


@pytest.mark.django_db
def test_replay_allows_full_org_admin_for_project_not_in_scope() -> None:
    organization, project, _team = _make_org_project_team()
    scope = EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(),
        capabilities=('projects:*',),
        actor_type='user',
        actor_id='debug-admin',
        project_bound=False,
    )

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=scope,
        query='',
        team_id=None,
        file_paths=(),
        symbols=(),
    )

    assert result.scope_filters['project_id'] == str(project.id)


@pytest.mark.django_db
def test_replay_denies_foreign_team_id_for_non_admin() -> None:
    organization, project, team = _make_org_project_team()
    foreign_team = Team.objects.create(organization=organization, name='Foreign', slug='foreign-search-debug')
    scope = _make_scope(organization, project, team_ids=(team.id,))

    with pytest.raises(AccessDeniedError) as exc:
        ReplaySearchDebug().execute(
            organization=organization,
            project=project,
            scope=scope,
            query='authorization',
            team_id=foreign_team.id,
            file_paths=(),
            symbols=(),
        )

    assert exc.value.error_code == 'team_scope_denied'
    assert exc.value.status_code == 403


@pytest.mark.django_db
def test_replay_member_team_id_narrows_within_scope() -> None:
    organization, project, team = _make_org_project_team()
    second_team = Team.objects.create(organization=organization, name='Second', slug='second-search-debug')
    in_scope_doc = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='team a anchor',
        body='team a anchor',
        exact_terms=['authorization'],
        sequence=1,
        visibility_scope=VisibilityScope.TEAM,
    )
    other_doc = _seed_lexical_recall_document(
        organization,
        project,
        second_team,
        title='team b anchor',
        body='team b anchor',
        exact_terms=['authorization'],
        sequence=2,
        visibility_scope=VisibilityScope.TEAM,
    )
    scope = _make_scope(organization, project, team_ids=(team.id, second_team.id))

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=scope,
        query='authorization',
        team_id=team.id,
        file_paths=(),
        symbols=(),
    )

    assert in_scope_doc.memory_id in {m.memory_id for m in result.exact_matches}
    excluded_reasons = {e.memory_id: e.reason for e in result.excluded}
    assert excluded_reasons.get(other_doc.memory_id) == 'team_not_in_scope'
    assert result.scope_filters['team_ids'] == [str(team.id)]


@pytest.mark.django_db
def test_replay_admin_narrows_to_requested_team_and_authorizes_team_document() -> None:
    organization, project, team = _make_org_project_team()
    team_doc = _seed_lexical_recall_document(
        organization,
        project,
        team,
        title='team scoped anchor',
        body='team scoped anchor',
        exact_terms=['authorization'],
        sequence=1,
        visibility_scope=VisibilityScope.TEAM,
    )
    scope = EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(),
        capabilities=('projects:*',),
        actor_type='user',
        actor_id='debug-admin',
        project_bound=False,
    )

    result = ReplaySearchDebug().execute(
        organization=organization,
        project=project,
        scope=scope,
        query='authorization',
        team_id=team.id,
        file_paths=(),
        symbols=(),
    )

    assert team_doc.memory_id in {m.memory_id for m in result.exact_matches}
    assert team_doc.memory_id not in {e.memory_id for e in result.excluded}
    assert result.scope_filters['team_ids'] == [str(team.id)]


# digest visibility quarantine — search debug candidates and pack


@pytest.mark.django_db
def test_search_debug_excludes_unproven_digest_and_admin_does_not_bypass() -> None:
    organization, project, _team = _make_org_project_team()
    proven = build_proven_weekly_digest(organization, project)
    legacy = build_legacy_digest(organization, project)
    scope = _make_scope(organization, project, capabilities=('memories:read', 'memories:admin'))

    result = ReplaySearchDebug().execute(
        organization,
        project,
        scope,
        query='',
        team_id=None,
        file_paths=(),
        symbols=(),
    )

    exact_ids = {match.memory_id for match in result.exact_matches}
    semantic_ids = {candidate.memory_id for candidate in result.semantic_candidates}
    lexical_ids = {candidate.memory_id for candidate in result.lexical_candidates}
    packed_ids = {item.memory_id for item in result.packed_context}

    assert proven.id in packed_ids
    assert legacy.id not in exact_ids
    assert legacy.id not in semantic_ids
    assert legacy.id not in lexical_ids
    assert legacy.id not in packed_ids
