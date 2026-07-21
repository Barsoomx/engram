from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import MemoryCandidate, Organization, Project, ProjectTeam, Team, VisibilityScope
from engram.memory.memory_propose_service import ProposeMemoryError

_URL = '/v1/memories/propose'


def _key_material(suffix: str) -> str:
    return f'egk_test_propose_{suffix}_0123456789abcdefghijklmnopqrstuvwxyz'


def _make_key(
    organization: Organization,
    owner: Identity,
    *,
    raw_key: str,
    capabilities: tuple[str, ...],
    team: Team | None = None,
    project: Project | None = None,
) -> ApiKey:
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name=f'propose key {raw_key[-6:]}',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        team=team,
        project=project,
    )
    for code in capabilities:
        ApiKeyCapability.objects.create(api_key=api_key, capability=Capability.objects.get(code=code))

    return api_key


@pytest.fixture
def f_scope() -> tuple[Organization, Team, Project, Identity]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(
        organization=organization,
        name='Backend',
        slug='backend',
        repository_url='https://example.test/engram.git',
        repository_root='/workspace/engram',
    )
    ProjectTeam.objects.create(organization=organization, team=team, project=project)
    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id='svc-propose',
        display_name='Propose service account',
    )
    role = Role.objects.get(code='organization_admin')
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)

    return organization, team, project, owner


def _headers(raw_key: str) -> dict[str, str]:
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'title': 'Deploy requires approval',
        'body': 'The production deploy pipeline requires a manual approval step.',
        'request_id': f'req-{uuid.uuid4()}',
    }
    payload.update(overrides)

    return payload


@pytest.mark.django_db
def test_propose_happy_path_returns_202_with_four_fields(f_scope: tuple[Organization, Team, Project, Identity]) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('happy')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)
    request_id = 'req-happy-1'

    response = APIClient().post(
        _URL,
        _payload(project_id=str(project.id), request_id=request_id),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 202
    assert set(response.json()) == {'candidate_id', 'status', 'decision_work_queued', 'request_id'}
    assert response.json()['status'] == 'proposed'
    assert response.json()['decision_work_queued'] is True
    assert response.json()['request_id'] == request_id


@pytest.mark.django_db
def test_propose_without_capability_is_403(f_scope: tuple[Organization, Team, Project, Identity]) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('nocap')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:read',), project=project)

    response = APIClient().post(
        _URL,
        _payload(project_id=str(project.id)),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_propose_missing_and_blank_body_are_400(f_scope: tuple[Organization, Team, Project, Identity]) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('body')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)
    client = APIClient()

    missing = client.post(_URL, _payload(project_id=str(project.id), body=None), format='json', **_headers(raw_key))
    blank = client.post(_URL, _payload(project_id=str(project.id), body='   '), format='json', **_headers(raw_key))

    assert missing.status_code == 400
    assert blank.status_code == 400


@pytest.mark.django_db
def test_propose_oversized_request_id_and_body_are_400(
    f_scope: tuple[Organization, Team, Project, Identity],
) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('oversize')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)
    client = APIClient()

    big_request_id = client.post(
        _URL,
        _payload(project_id=str(project.id), request_id='x' * 256),
        format='json',
        **_headers(raw_key),
    )
    big_body = client.post(
        _URL,
        _payload(project_id=str(project.id), body='b' * 16001),
        format='json',
        **_headers(raw_key),
    )

    assert big_request_id.status_code == 400
    assert big_body.status_code == 400


@pytest.mark.django_db
def test_bearer_unlinked_team_is_403_team_scope_denied(
    f_scope: tuple[Organization, Team, Project, Identity],
) -> None:
    organization, team, project, owner = f_scope
    other_team = Team.objects.create(organization=organization, name='Other', slug='other')
    raw_key = _key_material('teamdenied')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), team=team, project=project)

    response = APIClient().post(
        _URL,
        _payload(project_id=str(project.id), team_id=str(other_team.id)),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'team_scope_denied'


@pytest.mark.django_db
def test_team_bound_key_omitting_team_creates_team_candidate(
    f_scope: tuple[Organization, Team, Project, Identity],
) -> None:
    organization, team, project, owner = f_scope
    raw_key = _key_material('bound')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), team=team, project=project)

    response = APIClient().post(
        _URL,
        _payload(project_id=str(project.id)),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 202
    candidate = MemoryCandidate.objects.get(id=response.json()['candidate_id'])
    assert candidate.visibility_scope == VisibilityScope.TEAM
    assert candidate.team_id == team.id


@pytest.mark.django_db
def test_nonexistent_explicit_project_is_403_not_404(
    f_scope: tuple[Organization, Team, Project, Identity],
) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('badproj')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)

    response = APIClient().post(
        _URL,
        _payload(project_id=str(uuid.uuid4())),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_unmatched_repository_url_is_404(f_scope: tuple[Organization, Team, Project, Identity]) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('repo404')
    _make_key(
        organization,
        owner,
        raw_key=raw_key,
        capabilities=('memories:propose', 'projects:agent'),
    )

    response = APIClient().post(
        _URL,
        _payload(repository_url='https://unmatched.test/nothing.git'),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_missing_project_and_repository_is_400(f_scope: tuple[Organization, Team, Project, Identity]) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('noroute')
    _make_key(
        organization,
        owner,
        raw_key=raw_key,
        capabilities=('memories:propose', 'projects:agent'),
    )

    response = APIClient().post(
        _URL,
        _payload(),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_idempotent_repost_returns_same_candidate(
    f_scope: tuple[Organization, Team, Project, Identity],
) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('idem')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)
    client = APIClient()
    payload = _payload(project_id=str(project.id))

    first = client.post(_URL, payload, format='json', **_headers(raw_key))
    second = client.post(_URL, payload, format='json', **_headers(raw_key))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()['candidate_id'] == second.json()['candidate_id']


@pytest.mark.django_db
def test_propose_blank_detail_error_returns_static_message_without_exception_text(
    f_scope: tuple[Organization, Team, Project, Identity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, _team, project, owner = f_scope
    raw_key = _key_material('blankdetail')
    _make_key(organization, owner, raw_key=raw_key, capabilities=('memories:propose',), project=project)

    def m_raise(_self: object, _data: object) -> None:
        raise ProposeMemoryError('internal_failure')

    monkeypatch.setattr('engram.memory.propose_view.ProposeMemory.execute', m_raise)

    response = APIClient().post(
        _URL,
        _payload(project_id=str(project.id)),
        format='json',
        **_headers(raw_key),
    )

    assert response.status_code == 400
    body = response.json()
    assert body['code'] == 'internal_failure'
    assert body['detail'] == 'Memory propose failed.'
    assert 'internal_failure' not in body['detail']
