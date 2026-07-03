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
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.context.context_api_tests import RAW_KEY, auth_headers, create_project_scope
from engram.core.models import Agent, AgentSession, Observation, Organization, Project, RawEventEnvelope, Runtime

AGENT_RAW_KEY = 'egk_test_observations_agent_0123456789abcdefghijklmnopqrstuv'
AGENT_CAPS = ('observations:read', 'projects:agent')


def create_org_agent_key(organization: Organization) -> None:
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='observations-agent',
        display_name='Observations agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='observations agent key',
        key_prefix=api_key_prefix(AGENT_RAW_KEY),
        key_hash=hash_api_key(AGENT_RAW_KEY),
        key_fingerprint=api_key_fingerprint(AGENT_RAW_KEY),
        active=True,
    )
    for code in AGENT_CAPS:
        ApiKeyCapability.objects.get_or_create(
            api_key=api_key,
            capability=Capability.objects.get(code=code),
        )


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
    subtitle: str = '',
    facts: list[Any] | None = None,
    narrative: str = '',
    concepts: list[Any] | None = None,
    raw_event: RawEventEnvelope | None = None,
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
        raw_event=raw_event,
        observation_type='tool_use',
        title=title,
        subtitle=subtitle,
        body=body,
        facts=facts or [],
        narrative=narrative,
        concepts=concepts or [],
        content_hash=content_hash,
        observed_at=timezone.now(),
    )


def create_raw_event(
    organization: Any,
    team: Any,
    project: Any,
    *,
    correlation_id: str,
    client_event_id: str,
) -> RawEventEnvelope:
    agent = Agent.objects.create(
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'codex-raw-{client_event_id}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-raw-{client_event_id}',
        runtime=Runtime.CODEX,
    )

    return RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        client_event_id=client_event_id,
        idempotency_key=f'{client_event_id}-key',
        content_hash=f'raw-hash-{client_event_id}',
        runtime=Runtime.CODEX,
        payload={'tool_name': 'bash'},
        correlation_id=correlation_id,
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


@pytest.mark.django_db
def test_list_observations_includes_facts_narrative_concepts_subtitle() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    create_observation(
        organization,
        team,
        project,
        title='Full fields obs',
        subtitle='Subtitle text',
        facts=['fact-one', 'fact-two'],
        narrative='Narrative text',
        concepts=['concept-one'],
        content_hash='h-full-fields',
    )
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['subtitle'] == 'Subtitle text'
    assert item['facts'] == ['fact-one', 'fact-two']
    assert item['narrative'] == 'Narrative text'
    assert item['concepts'] == ['concept-one']


@pytest.mark.django_db
def test_observation_detail_includes_facts_narrative_concepts_subtitle() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    obs = create_observation(
        organization,
        team,
        project,
        title='Detail full fields obs',
        subtitle='Detail subtitle',
        facts=['detail-fact'],
        narrative='Detail narrative',
        concepts=['detail-concept'],
        content_hash='h-detail-full-fields',
    )
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['subtitle'] == 'Detail subtitle'
    assert body['facts'] == ['detail-fact']
    assert body['narrative'] == 'Detail narrative'
    assert body['concepts'] == ['detail-concept']


@pytest.mark.django_db
def test_list_observations_filters_by_correlation_id() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    matching_raw_event = create_raw_event(
        organization,
        team,
        project,
        correlation_id='corr-match-1',
        client_event_id='event-match-1',
    )
    other_raw_event = create_raw_event(
        organization,
        team,
        project,
        correlation_id='corr-other-1',
        client_event_id='event-other-1',
    )
    matching_obs = create_observation(
        organization,
        team,
        project,
        title='Matching correlation obs',
        content_hash='h-corr-match',
        raw_event=matching_raw_event,
    )
    create_observation(
        organization,
        team,
        project,
        title='Other correlation obs',
        content_hash='h-corr-other',
        raw_event=other_raw_event,
    )
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'project_id': str(project.id), 'correlation_id': 'corr-match-1', 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['observation_id'] == str(matching_obs.id)


@pytest.mark.django_db
def test_list_observations_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/observations-demo.git'
    project.save(update_fields=['repository_url'])
    create_org_agent_key(organization)
    observation = create_observation(organization, team, project, content_hash='h-repo-url-list')
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'repository_url': 'https://github.com/acme/observations-demo', 'limit': 10},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['observation_id'] == str(observation.id)


@pytest.mark.django_db
def test_list_observations_unknown_repository_returns_404() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'repository_url': 'https://github.com/acme/never-created', 'limit': 10},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_list_observations_cross_org_repository_url_returns_404() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    other_organization = Organization.objects.create(name='Globex', slug='globex-obs-list')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-obs-list',
        repository_url='git@github.com:acme/foreign-org-list.git',
    )
    create_org_agent_key(organization)
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'repository_url': 'https://github.com/acme/foreign-org-list', 'limit': 10},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_list_observations_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-obs-list-inorg',
        repository_url='git@github.com:acme/foreign-in-org-list.git',
    )
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'repository_url': 'https://github.com/acme/foreign-in-org-list', 'limit': 10},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_list_observations_missing_project_and_repository_url_returns_400() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {'limit': 10},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_list_observations_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    observation = create_observation(organization, team, project, content_hash='h-repo-url-wins')
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-obs-list',
        repository_url='git@github.com:acme/decoy-list.git',
    )
    client = APIClient()

    response = client.get(
        '/v1/observations/',
        {
            'project_id': str(project.id),
            'repository_url': 'https://github.com/acme/decoy-list',
            'limit': 10,
        },
        **auth_headers(),
    )

    assert response.status_code == 200
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['observation_id'] == str(observation.id)


@pytest.mark.django_db
def test_observation_detail_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    project.repository_url = 'git@github.com:acme/observations-detail-demo.git'
    project.save(update_fields=['repository_url'])
    create_org_agent_key(organization)
    obs = create_observation(organization, team, project, content_hash='h-repo-url-detail')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'repository_url': 'https://github.com/acme/observations-detail-demo'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    assert response.json()['observation_id'] == str(obs.id)


@pytest.mark.django_db
def test_observation_detail_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    obs = create_observation(organization, team, project, content_hash='h-detail-unknown-repo')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'repository_url': 'https://github.com/acme/never-created-detail'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_observation_detail_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-obs-detail')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-detail',
        repository_url='git@github.com:acme/foreign-detail.git',
    )
    obs = create_observation(organization, team, project, content_hash='h-detail-cross-org')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'repository_url': 'https://github.com/acme/foreign-detail'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_observation_detail_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-obs-detail-inorg',
        repository_url='git@github.com:acme/foreign-in-org-detail.git',
    )
    obs = create_observation(organization, team, project, content_hash='h-detail-foreign-in-org')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'repository_url': 'https://github.com/acme/foreign-in-org-detail'},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_observation_detail_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    obs = create_observation(organization, team, project, content_hash='h-detail-missing-both')
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_observation_detail_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_observations_read(RAW_KEY)
    obs = create_observation(organization, team, project, content_hash='h-detail-project-wins')
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-obs-detail',
        repository_url='git@github.com:acme/decoy-detail.git',
    )
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'project_id': str(project.id), 'repository_url': 'https://github.com/acme/decoy-detail'},
        **auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()['observation_id'] == str(obs.id)


@pytest.mark.django_db
def test_observation_detail_repository_url_resolving_elsewhere_never_leaks_object_from_another_project() -> None:
    organization, team, project_a, _owner, _api_key = create_project_scope()
    Project.objects.create(
        organization=organization,
        name='Project B',
        slug='project-b-obs-leak-probe',
        repository_url='git@github.com:acme/project-b-obs.git',
    )
    create_org_agent_key(organization)
    obs = create_observation(
        organization,
        team,
        project_a,
        title='Project A secret observation',
        content_hash='h-leak-probe-detail',
    )
    client = APIClient()

    response = client.get(
        f'/v1/observations/{obs.id}',
        {'repository_url': 'https://github.com/acme/project-b-obs'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'observation_not_found'
    assert 'Project A secret observation' not in str(response.json())
