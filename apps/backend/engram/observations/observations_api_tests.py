from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.access.services import hash_api_key
from engram.context.context_api_tests import RAW_KEY, auth_headers, create_project_scope
from engram.core.models import Agent, AgentSession, Observation, Organization, Project, Runtime


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


@pytest.mark.django_db
def test_observation_list_offset_paginates() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    for i in range(4):
        create_observation(organization, team, project, title=f'Obs {i}', content_hash=f'h-page-{i}')
    client = APIClient()

    page_one = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'limit': 2, 'offset': 0},
        **auth_headers(),
    )
    page_two = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'limit': 2, 'offset': 2},
        **auth_headers(),
    )

    assert page_one.status_code == 200
    assert len(page_one.json()['items']) == 2
    assert page_two.status_code == 200
    assert len(page_two.json()['items']) == 2
    ids_one = {i['observation_id'] for i in page_one.json()['items']}
    ids_two = {i['observation_id'] for i in page_two.json()['items']}
    assert ids_one.isdisjoint(ids_two)


@pytest.mark.django_db
def test_observation_list_filter_by_observation_type() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    tool_obs = create_observation(organization, team, project, title='Tool obs', content_hash='h-tool-type')
    agent = tool_obs.agent
    session = tool_obs.session
    Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='code_review',
        title='Code review obs',
        body='Body.',
        content_hash='h-code-type',
        observed_at=timezone.now(),
    )
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'observation_type': 'tool_use', 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['observation_id'] == str(tool_obs.id)


@pytest.mark.django_db
def test_observation_list_filter_by_session_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    obs_a = create_observation(organization, team, project, title='Session A obs', content_hash='h-sess-a')
    obs_b = create_observation(organization, team, project, title='Session B obs', content_hash='h-sess-b')
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'session_id': str(obs_a.session_id), 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    ids = {i['observation_id'] for i in items}
    assert str(obs_a.id) in ids
    assert str(obs_b.id) not in ids


@pytest.mark.django_db
def test_observation_list_filter_by_since_until() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    now = timezone.now()
    old_obs = create_observation(organization, team, project, title='Old obs', content_hash='h-since-old')
    old_obs.created_at = now - timedelta(days=10)
    old_obs.save(update_fields=['created_at'])
    new_obs = create_observation(organization, team, project, title='New obs', content_hash='h-since-new')
    new_obs.created_at = now - timedelta(days=1)
    new_obs.save(update_fields=['created_at'])
    since_str = (now - timedelta(days=5)).isoformat()
    until_str = now.isoformat()
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'since': since_str, 'until': until_str, 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    ids = {i['observation_id'] for i in response.json()['items']}
    assert str(new_obs.id) in ids
    assert str(old_obs.id) not in ids


@pytest.mark.django_db
def test_observation_detail_returns_observation() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    obs = create_observation(
        organization,
        team,
        project,
        title=f'Detail obs {RAW_KEY}',
        content_hash='h-detail-obs',
    )
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['observation_id'] == str(obs.id)
    assert body['title'] == 'Detail obs [REDACTED]'
    assert RAW_KEY not in str(body)
    assert 'request_id' in body


@pytest.mark.django_db
def test_observation_detail_cross_project_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    other_project = Project.objects.create(organization=organization, name='Other proj', slug='other-obs-detail')
    obs = create_observation(organization, team, other_project, title='Other proj obs', content_hash='h-cross-proj')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'observation_not_found'


@pytest.mark.django_db
def test_observation_detail_missing_capability_returns_403() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    obs = create_observation(organization, team, project, title='No cap obs', content_hash='h-no-cap-obs')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 403


def _make_session_admin(organization: Organization) -> str:
    user = User.objects.create_user('session-obs-admin', password='admin-obs-pass-123')  # noqa: S106
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': 'Session obs admin'},
    )
    role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role)

    return Token.objects.get_or_create(user=user)[0].key


def _session_headers(token: str, organization: Organization) -> dict[str, str]:
    return {
        'HTTP_AUTHORIZATION': f'Token {token}',
        'HTTP_X_ENGRAM_ORGANIZATION': str(organization.id),
    }


@pytest.mark.django_db
def test_session_admin_can_list_observations() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    token = _make_session_admin(organization)
    obs = create_observation(organization, team, project, content_hash='h-sess-list')
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'limit': 10},
        **_session_headers(token, organization),
    )

    assert response.status_code == 200
    body = response.json()
    assert 'request_id' in body
    ids = [item['observation_id'] for item in body['items']]
    assert str(obs.id) in ids


@pytest.mark.django_db
def test_session_admin_can_get_observation_detail() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    token = _make_session_admin(organization)
    obs = create_observation(
        organization,
        team,
        project,
        title=f'Session detail {RAW_KEY}',
        content_hash='h-sess-detail',
    )
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id)},
        **_session_headers(token, organization),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['observation_id'] == str(obs.id)
    assert body['title'] == 'Session detail [REDACTED]'
    assert 'request_id' in body
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_session_user_lacking_observations_read_gets_403() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    user = User.objects.create_user('limited-obs-sess', password='pass-limited-456')  # noqa: S106
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': 'Limited session'},
    )
    limited_role = Role.objects.create(code='limited-obs-sess-role', name='Limited obs test')
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=limited_role)
    token = Token.objects.get_or_create(user=user)[0].key
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id)},
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(organization.id),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
