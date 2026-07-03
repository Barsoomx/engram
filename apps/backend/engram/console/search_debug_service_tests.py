from __future__ import annotations

import uuid

import pytest

from engram.access.services import EffectiveScope
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

pytestmark_pgvector = pytest.mark.skipif(VectorField is None, reason='pgvector not installed')


def _make_scope(organization: Organization) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(),
        team_ids=(),
        capabilities=(),
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
        content_hash=f'debug-recall-hash-{sequence}',
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
        scope=_make_scope(organization),
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
        scope=_make_scope(organization),
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
