from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from engram.access.models import ApiKey, ApiKeyCapability, Capability
from engram.access.services import hash_api_key
from engram.context.context_api_tests import RAW_KEY, auth_headers, create_project_scope
from engram.core.models import Agent, AgentSession, Observation, Project, Runtime


def grant_observations_read(raw_key: str) -> None:
    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='observations:read'),
    )


def create_observation(
    organization: Any,
    team: Any,
    project: Any,
    *,
    title: str = 'Test observation',
    body: str = 'Observation body content.',
    content_hash: str = 'obs-hash-1',
) -> Observation:
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'codex-obs-{content_hash}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-obs-{content_hash}',
        runtime=Runtime.CODEX,
    )

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='tool_use',
        title=title,
        body=body,
        content_hash=content_hash,
        observed_at=timezone.now(),
    )


@pytest.mark.django_db
def test_list_observations_returns_authorized_observations() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    observation = create_observation(organization, team, project)
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['request_id']
    items = body['items']
    assert len(items) == 1
    assert items[0]['observation_id'] == str(observation.id)
    assert items[0]['title'] == 'Test observation'
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_list_observations_requires_observations_read_capability() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_observation(organization, team, project)
    client = APIClient()

    response = client.get('/v1/observations/', {'project_id': str(project.id)}, **auth_headers())

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_list_observations_denies_wrong_project() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-obs')
    client = APIClient()

    response = client.get('/v1/observations/', {'project_id': str(other_project.id)}, **auth_headers())

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_list_observations_filters_by_team() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    create_observation(organization, team, project, title='In team', content_hash='h-in')
    from engram.core.models import Team

    other_team = Team.objects.create(organization=organization, name='Other', slug='other-obs-team')
    create_observation(organization, other_team, project, title='Other team', content_hash='h-out')
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'team_id': str(team.id), 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['title'] == 'In team'
