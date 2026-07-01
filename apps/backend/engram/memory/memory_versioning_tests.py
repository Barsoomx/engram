from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKey, ApiKeyCapability, Capability, Role, RoleCapability
from engram.access.services import hash_api_key
from engram.context.context_api_tests import (
    OTHER_RAW_KEY,
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
    create_scoped_api_key,
)
from engram.core.models import AuditEvent, MemoryVersion, Project, RetrievalDocument, VisibilityScope

VERSION_BODY_MAX_LENGTH = 16000


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


def version_payload(project: Project, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        'project_id': str(project.id),
        'body': 'Updated memory body describes the corrected engineering fact.',
        'reason': 'corrected after review',
        'request_id': 'request-version-1',
    }
    payload.update(overrides)

    return payload


@pytest.mark.django_db
def test_update_memory_body_creates_version_and_reindexes() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, version, document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['memory_id'] == str(memory.id)
    assert body['current_version'] == 2
    new_version = MemoryVersion.objects.get(id=body['memory_version_id'])
    assert new_version.version == 2
    assert new_version.memory_id == memory.id
    assert new_version.body == 'Updated memory body describes the corrected engineering fact.'
    memory.refresh_from_db()
    assert memory.current_version == 2
    assert memory.body == new_version.body
    new_document = RetrievalDocument.objects.get(id=body['retrieval_document_id'])
    assert new_document.memory_version_id == new_version.id
    assert new_document.id != document.id
    audit = AuditEvent.objects.get(event_type='MemoryVersionCreated', target_id=str(memory.id))
    assert audit.capability == 'memories:review'
    assert audit.metadata['version'] == 2
    assert RAW_KEY not in str(body)
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_update_memory_body_requires_review_capability() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
    assert MemoryVersion.objects.filter(memory=memory).count() == 1


@pytest.mark.django_db
def test_update_memory_body_returns_not_found_for_other_project_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    from engram.core.models import Project

    other_project = Project.objects.create(organization=organization, name='Other', slug='other-project-v')
    memory2, _version2, _document2 = create_approved_memory_document(organization, team, other_project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory2.id}/version',
        version_payload(project),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'memory_not_found'
    assert MemoryVersion.objects.filter(memory=memory2).count() == 1


@pytest.mark.django_db
def test_update_memory_body_denies_other_team_visible_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    from engram.core.models import Team

    other_team = Team.objects.create(organization=organization, name='Other', slug='other-team-v')
    memory, _version, _document = create_approved_memory_document(
        organization,
        other_team,
        project,
        visibility_scope=VisibilityScope.TEAM,
        title='Other team private memory versioned',
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, team_id=str(team.id)),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'team_scope_denied'
    assert MemoryVersion.objects.filter(memory=memory).count() == 1


@pytest.mark.django_db
def test_update_memory_body_rejects_oversized_body() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, body='a' * (VERSION_BODY_MAX_LENGTH + 1)),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert MemoryVersion.objects.filter(memory=memory).count() == 1


@pytest.mark.django_db
def test_update_memory_body_supports_multiple_versions() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    first = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, body='First update', request_id='request-version-a'),
        format='json',
        **auth_headers(),
    )
    second = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, body='Second update', request_id='request-version-b'),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['current_version'] == 2
    assert second.json()['current_version'] == 3
    memory.refresh_from_db()
    assert memory.current_version == 3
    assert memory.body == 'Second update'
    assert MemoryVersion.objects.filter(memory=memory).count() == 3


@pytest.mark.django_db
def test_update_memory_body_is_idempotent_for_same_body() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    first = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, body='Same body replayed', request_id='request-version-same-a'),
        format='json',
        **auth_headers(),
    )
    second = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project, body='Same body replayed', request_id='request-version-same-b'),
        format='json',
        **auth_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['memory_version_id'] == second.json()['memory_version_id']
    assert first.json()['current_version'] == 2
    assert second.json()['current_version'] == 2
    assert MemoryVersion.objects.filter(memory=memory).count() == 2


@pytest.mark.django_db
def test_list_memory_versions_returns_history() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=2,
        body='Second revision describes the corrected engineering fact.',
        content_hash='authorization-before-ranking-v2',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/version',
        {'project_id': str(project.id)},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['count'] == 2
    items = body['items']
    assert [item['version'] for item in items] == [2, 1]
    assert items[0]['body'] == 'Second revision describes the corrected engineering fact.'
    assert items[1]['version'] == 1
    assert 'created_at' in items[0]
    assert 'source_observation_id' in items[0]
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_list_memory_versions_requires_read_capability() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    create_scoped_api_key(organization, team, project, owner, raw_key=OTHER_RAW_KEY, capabilities=())
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/version',
        {'project_id': str(project.id)},
        **auth_headers(OTHER_RAW_KEY),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'


@pytest.mark.django_db
def test_list_memory_versions_denies_other_project() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-project-vlist')
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/version',
        {'project_id': str(other_project.id)},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
@pytest.mark.parametrize('stale_field', ['stale', 'refuted'])
def test_update_memory_body_rejects_stale_or_refuted_memory(stale_field: str) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    setattr(memory, stale_field, True)
    memory.save(update_fields=[stale_field, 'updated_at'])
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/version',
        version_payload(project),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'memory_not_editable'
    assert MemoryVersion.objects.filter(memory=memory).count() == 1
