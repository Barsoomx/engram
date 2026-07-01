from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from engram.context.context_api_tests import create_project_scope
from engram.core.models import Observation
from engram.observations.filters import ObservationFilterSet
from engram.observations.observations_api_tests import create_observation, create_raw_event


@pytest.mark.django_db
def test_filterset_filters_by_team_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    matching = create_observation(organization, team, project, content_hash='team-match')

    other_team_observation = create_observation(organization, None, project, content_hash='team-other')

    queryset = Observation.objects.filter(organization=organization, project=project)

    filtered = ObservationFilterSet(data={'team_id': str(team.id)}, queryset=queryset).qs

    ids = {obs.id for obs in filtered}

    assert matching.id in ids

    assert other_team_observation.id not in ids


@pytest.mark.django_db
def test_filterset_filters_by_observation_type() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    matching = create_observation(organization, team, project, content_hash='type-match')

    queryset = Observation.objects.filter(organization=organization, project=project)

    filtered = ObservationFilterSet(data={'observation_type': 'tool_use'}, queryset=queryset).qs

    assert matching.id in {obs.id for obs in filtered}

    filtered_none = ObservationFilterSet(data={'observation_type': 'other_type'}, queryset=queryset).qs

    assert filtered_none.count() == 0


@pytest.mark.django_db
def test_filterset_filters_by_correlation_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    raw_event = create_raw_event(
        organization,
        team,
        project,
        correlation_id='corr-filterset-match',
        client_event_id='client-filterset-1',
    )

    matching = create_observation(organization, team, project, content_hash='corr-match', raw_event=raw_event)

    unrelated = create_observation(organization, team, project, content_hash='corr-unrelated')

    queryset = Observation.objects.filter(organization=organization, project=project)

    filtered = ObservationFilterSet(data={'correlation_id': 'corr-filterset-match'}, queryset=queryset).qs

    ids = {obs.id for obs in filtered}

    assert matching.id in ids

    assert unrelated.id not in ids


@pytest.mark.django_db
def test_filterset_filters_by_since_until_range() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    now = timezone.now()

    old_obs = create_observation(organization, team, project, content_hash='since-old')

    old_obs.created_at = now - timedelta(days=10)

    old_obs.save(update_fields=['created_at'])

    new_obs = create_observation(organization, team, project, content_hash='since-new')

    new_obs.created_at = now - timedelta(days=1)

    new_obs.save(update_fields=['created_at'])

    queryset = Observation.objects.filter(organization=organization, project=project)

    filtered = ObservationFilterSet(
        data={'since': (now - timedelta(days=5)).isoformat(), 'until': now.isoformat()},
        queryset=queryset,
    ).qs

    ids = {obs.id for obs in filtered}

    assert new_obs.id in ids

    assert old_obs.id not in ids
