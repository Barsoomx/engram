from __future__ import annotations

import pytest

from engram.console.filters import MemoryReviewCandidateFilterSet, MemoryReviewMemoryFilterSet
from engram.console.views.memory_review_tests import _make_candidate, _make_memory, _make_observation
from engram.core.models import Memory, MemoryCandidate, ObservationSource, Organization, Project, VisibilityScope


def _create_org_and_project() -> tuple[Organization, Project]:
    organization = Organization.objects.create(name='FiltersOrg', slug='filters-org')

    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, project


@pytest.mark.django_db
def test_memory_filterset_filters_by_visibility_scope_and_confidence_range() -> None:
    organization, project = _create_org_and_project()

    team_memory = _make_memory(organization, project, visibility_scope=VisibilityScope.TEAM, confidence='0.500')

    project_memory = _make_memory(organization, project, visibility_scope=VisibilityScope.PROJECT, confidence='0.950')

    queryset = Memory.objects.filter(organization=organization)

    by_scope = MemoryReviewMemoryFilterSet(data={'visibility_scope': 'team'}, queryset=queryset).qs

    assert {m.id for m in by_scope} == {team_memory.id}

    by_confidence = MemoryReviewMemoryFilterSet(
        data={'confidence__gte': '0.300', 'confidence__lte': '0.700'},
        queryset=queryset,
    ).qs

    assert {m.id for m in by_confidence} == {team_memory.id}

    assert project_memory.id not in {m.id for m in by_confidence}


@pytest.mark.django_db
def test_memory_filterset_filters_by_search() -> None:
    organization, project = _create_org_and_project()

    target = _make_memory(organization, project, title='Authentication flow notes', body='login handshake')

    _make_memory(organization, project, title='Billing notes', body='unrelated billing details')

    queryset = Memory.objects.filter(organization=organization)

    filtered = MemoryReviewMemoryFilterSet(data={'search': 'authentication'}, queryset=queryset).qs

    assert {m.id for m in filtered} == {target.id}


@pytest.mark.django_db
def test_memory_filterset_rejects_invalid_team_id() -> None:
    organization, project = _create_org_and_project()

    _make_memory(organization, project)

    queryset = Memory.objects.filter(organization=organization)

    filterset = MemoryReviewMemoryFilterSet(data={'team_id': 'not-a-uuid'}, queryset=queryset)

    assert filterset.is_valid() is False


@pytest.mark.django_db
def test_candidate_filterset_filters_by_source_type() -> None:
    organization, project = _create_org_and_project()

    file_observation = _make_observation(organization, project)

    ObservationSource.objects.create(
        organization=organization,
        project=project,
        observation=file_observation,
        source_type='file',
        source_id='src/app.py',
    )

    web_observation = _make_observation(organization, project)

    ObservationSource.objects.create(
        organization=organization,
        project=project,
        observation=web_observation,
        source_type='web',
        source_id='https://example.com',
    )

    file_candidate = _make_candidate(organization, project, source_observation=file_observation)

    _make_candidate(organization, project, source_observation=web_observation)

    queryset = MemoryCandidate.objects.filter(organization=organization)

    filtered = MemoryReviewCandidateFilterSet(data={'source_type': 'file'}, queryset=queryset).qs

    assert {c.id for c in filtered} == {file_candidate.id}
