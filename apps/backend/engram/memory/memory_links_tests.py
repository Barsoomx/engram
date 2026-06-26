from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKey, ApiKeyCapability, Capability, Role, RoleCapability
from engram.access.services import hash_api_key
from engram.context.context_api_tests import (
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
)
from engram.core.models import AuditEvent, MemoryLink, Project, Team, VisibilityScope


def grant_review_capability(raw_key: str) -> None:
    developer = Role.objects.get(code='developer')
    RoleCapability.objects.get_or_create(
        role=developer,
        capability=Capability.objects.get(code='memories:review'),
    )
    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    ApiKeyCapability.objects.get_or_create(
        api_key=api_key,
        capability=Capability.objects.get(code='memories:review'),
    )


def link_payload(project: Project, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        'project_id': str(project.id),
        'link_type': 'file',
        'target': 'apps/backend/engram/memory/services.py',
        'label': 'versioning service',
        'request_id': 'request-link-1',
    }
    payload.update(overrides)

    return payload


@pytest.mark.django_db
def test_create_and_list_memory_link() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    created = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project),
        format='json',
        **auth_headers(),
    )
    assert created.status_code == 201
    body = created.json()
    assert body['memory_id'] == str(memory.id)
    assert body['link_type'] == 'file'
    assert body['target'] == 'apps/backend/engram/memory/services.py'
    assert body['created'] is True
    assert RAW_KEY not in str(body)

    listed = client.get(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(project.id)},
        **auth_headers(),
    )
    assert listed.status_code == 200
    items = listed.json()['items']
    assert len(items) == 1
    assert items[0]['link_id'] == body['link_id']
    assert items[0]['target'] == 'apps/backend/engram/memory/services.py'

    audit = AuditEvent.objects.get(event_type='MemoryLinkRecorded', target_id=str(body['link_id']))
    assert audit.capability == 'memories:review'
    assert audit.metadata['memory_id'] == str(memory.id)


@pytest.mark.django_db
def test_create_memory_link_is_idempotent_for_same_target() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    first = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, request_id='request-link-a'),
        format='json',
        **auth_headers(),
    )
    second = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, request_id='request-link-b'),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()['link_id'] == second.json()['link_id']
    assert first.json()['created'] is True
    assert second.json()['created'] is False
    assert MemoryLink.objects.filter(memory=memory).count() == 1


@pytest.mark.django_db
def test_create_memory_link_requires_review_capability() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
    assert MemoryLink.objects.count() == 0


@pytest.mark.django_db
def test_create_memory_link_denies_other_team_visible_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    other_team = Team.objects.create(organization=organization, name='Other', slug='other-team-links')
    memory, _version, _document = create_approved_memory_document(
        organization,
        other_team,
        project,
        visibility_scope=VisibilityScope.TEAM,
        title='Other team private memory linked',
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, team_id=str(team.id)),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'team_scope_denied'
    assert MemoryLink.objects.count() == 0


@pytest.mark.django_db
def test_create_memory_link_rejects_oversized_target() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, target='a' * 1025),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert MemoryLink.objects.count() == 0


@pytest.mark.django_db
def test_list_memory_links_denies_other_project() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-project-links')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(other_project.id)},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
