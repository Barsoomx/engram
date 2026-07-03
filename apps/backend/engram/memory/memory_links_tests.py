from __future__ import annotations

import uuid

import pytest
import structlog
from rest_framework.test import APIClient

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
from engram.context.context_api_tests import (
    RAW_KEY,
    auth_headers,
    create_approved_memory_document,
    create_project_scope,
)
from engram.core.models import AuditEvent, MemoryLink, Organization, Project, Team, VisibilityScope

AGENT_RAW_KEY = 'egk_test_memory_link_agent_0123456789abcdefghijklmnopqrstuvwxyz'
AGENT_CAPS = ('memories:review', 'memories:read', 'projects:agent')


def create_org_agent_key(organization: Organization) -> None:
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})
        RoleCapability.objects.get_or_create(role=role, capability=capability)
    identity = Identity.objects.create(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='memory-link-agent',
        display_name='Memory link agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role, active=True)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=identity,
        name='memory link agent key',
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
    listed_body = listed.json()
    assert listed_body['count'] == 1
    items = listed_body['items']
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
def test_delete_memory_link_removes_and_audits() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(project.id), 'link_id': str(link.id), 'request_id': 'request-link-del-1'},
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body['link_id'] == str(link.id)
    assert body['deleted'] is True
    assert MemoryLink.objects.filter(id=link.id).count() == 0
    audit = AuditEvent.objects.get(event_type='MemoryLinkRemoved', target_id=str(link.id))
    assert audit.capability == 'memories:review'
    assert audit.metadata['memory_id'] == str(memory.id)
    assert RAW_KEY not in str(body)


@pytest.mark.django_db
def test_delete_memory_link_returns_not_found_for_missing_link() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(project.id), 'link_id': str(uuid.uuid4()), 'request_id': 'request-link-del-missing'},
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'link_not_found'


@pytest.mark.django_db
def test_delete_memory_link_requires_review_capability() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(project.id), 'link_id': str(link.id), 'request_id': 'request-link-del-403'},
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'missing_capability'
    assert MemoryLink.objects.filter(id=link.id).count() == 1


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


@pytest.mark.django_db
def test_create_memory_link_returns_not_found_for_other_project_memory() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    other_project = Project.objects.create(organization=organization, name='Other', slug='other-project-link-404')
    memory, _version, _document = create_approved_memory_document(organization, team, other_project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, request_id='request-link-missing-memory'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'memory_not_found'
    assert MemoryLink.objects.count() == 0


@pytest.mark.django_db
def test_create_memory_link_logs_memory_link_recorded() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    with structlog.testing.capture_logs() as captured_logs:
        response = client.post(
            f'/v1/memories/{memory.id}/links',
            link_payload(project, request_id='request-link-logged'),
            format='json',
            **auth_headers(),
        )

    assert response.status_code == 201
    body = response.json()
    link_events = [entry for entry in captured_logs if entry['event'] == 'memory_link_recorded']
    assert len(link_events) == 1
    assert link_events[0]['memory_id'] == str(memory.id)
    assert link_events[0]['item_id'] == body['link_id']
    assert link_events[0]['link_type'] == 'file'
    assert link_events[0]['created'] is True


@pytest.mark.django_db
def test_create_memory_link_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, project_id=None, repository_url=project.repository_url),
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 201, response.json()
    assert response.json()['created'] is True


@pytest.mark.django_db
def test_create_memory_link_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, project_id=None, repository_url='https://github.com/acme/never-created-link'),
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_create_memory_link_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-link')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-link',
        repository_url='git@github.com:acme/foreign-link.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, project_id=None, repository_url='https://github.com/acme/foreign-link'),
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_create_memory_link_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-link-inorg',
        repository_url='git@github.com:acme/foreign-in-org-link.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, foreign_project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, project_id=None, repository_url='https://github.com/acme/foreign-in-org-link'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_create_memory_link_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, project_id=None),
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_create_memory_link_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-link',
        repository_url='git@github.com:acme/decoy-link.git',
    )
    client = APIClient()

    response = client.post(
        f'/v1/memories/{memory.id}/links',
        link_payload(project, repository_url='https://github.com/acme/decoy-link'),
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 201, response.json()
    assert response.json()['created'] is True


@pytest.mark.django_db
def test_list_memory_links_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'repository_url': project.repository_url},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    assert response.json()['count'] == 1


@pytest.mark.django_db
def test_list_memory_links_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'repository_url': 'https://github.com/acme/never-created-link-list'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_list_memory_links_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-link-list')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-link-list',
        repository_url='git@github.com:acme/foreign-link-list.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'repository_url': 'https://github.com/acme/foreign-link-list'},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'


@pytest.mark.django_db
def test_list_memory_links_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-link-list-inorg',
        repository_url='git@github.com:acme/foreign-in-org-link-list.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, foreign_project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'repository_url': 'https://github.com/acme/foreign-in-org-link-list'},
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'


@pytest.mark.django_db
def test_list_memory_links_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {},
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'


@pytest.mark.django_db
def test_list_memory_links_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-link-list',
        repository_url='git@github.com:acme/decoy-link-list.git',
    )
    client = APIClient()

    response = client.get(
        f'/v1/memories/{memory.id}/links',
        {'project_id': str(project.id), 'repository_url': 'https://github.com/acme/decoy-link-list'},
        **auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()['count'] == 1


@pytest.mark.django_db
def test_delete_memory_link_routes_by_repository_url_with_org_agent_key() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {
            'repository_url': project.repository_url,
            'link_id': str(link.id),
            'request_id': 'request-link-del-repo-url',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 200, response.json()
    assert response.json()['deleted'] is True
    assert MemoryLink.objects.filter(id=link.id).count() == 0


@pytest.mark.django_db
def test_delete_memory_link_unknown_repository_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {
            'repository_url': 'https://github.com/acme/never-created-link-del',
            'link_id': str(link.id),
            'request_id': 'request-link-del-unknown-repo',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'
    assert MemoryLink.objects.filter(id=link.id).count() == 1


@pytest.mark.django_db
def test_delete_memory_link_cross_org_repository_url_returns_404() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    other_organization = Organization.objects.create(name='Globex', slug='globex-link-del')
    Project.objects.create(
        organization=other_organization,
        name='Foreign',
        slug='foreign-link-del',
        repository_url='git@github.com:acme/foreign-link-del.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {
            'repository_url': 'https://github.com/acme/foreign-link-del',
            'link_id': str(link.id),
            'request_id': 'request-link-del-cross-org',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 404
    assert response.json()['code'] == 'project_not_found'
    assert MemoryLink.objects.filter(id=link.id).count() == 1


@pytest.mark.django_db
def test_delete_memory_link_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-link-del-inorg',
        repository_url='git@github.com:acme/foreign-in-org-link-del.git',
    )
    memory, _version, _document = create_approved_memory_document(organization, team, foreign_project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=foreign_project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {
            'repository_url': 'https://github.com/acme/foreign-in-org-link-del',
            'link_id': str(link.id),
            'request_id': 'request-link-del-foreign-inorg',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 403
    assert response.json()['code'] == 'project_scope_denied'
    assert MemoryLink.objects.filter(id=link.id).count() == 1


@pytest.mark.django_db
def test_delete_memory_link_missing_project_and_repository_url_returns_400() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_org_agent_key(organization)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {'link_id': str(link.id), 'request_id': 'request-link-del-missing-both'},
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {AGENT_RAW_KEY}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'project_or_repository_required'
    assert MemoryLink.objects.filter(id=link.id).count() == 1


@pytest.mark.django_db
def test_delete_memory_link_project_id_wins_over_repository_url() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    grant_review_capability(RAW_KEY)
    memory, _version, _document = create_approved_memory_document(organization, team, project)
    link = MemoryLink.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        link_type='file',
        target='apps/backend/engram/memory/services.py',
        label='versioning service',
    )
    Project.objects.create(
        organization=organization,
        name='Decoy',
        slug='decoy-link-del',
        repository_url='git@github.com:acme/decoy-link-del.git',
    )
    client = APIClient()

    response = client.delete(
        f'/v1/memories/{memory.id}/links',
        {
            'project_id': str(project.id),
            'repository_url': 'https://github.com/acme/decoy-link-del',
            'link_id': str(link.id),
            'request_id': 'request-link-del-project-wins',
        },
        format='json',
        **auth_headers(),
    )

    assert response.status_code == 200, response.json()
    assert response.json()['deleted'] is True
