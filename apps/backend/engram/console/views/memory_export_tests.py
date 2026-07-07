from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
    RoleCapability,
)
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    Organization,
    Project,
    Team,
    VisibilityScope,
)

LEAKED_TOKEN = 'egk_view_export_secret_0123456789abcdefghijklmnopqrstuvwxyz'


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def _role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
    for cap_code in capability_codes:
        RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(cap_code))

    return role


def _client_for_org(username: str, org: Organization, capabilities: tuple[str, ...]) -> APIClient:
    user = _make_user(username)
    identity = _make_identity(user, org)
    role = _role_with_capabilities(f'role_{username}', capabilities)
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)
    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


def _make_memory(
    organization: Organization,
    project: Project,
    team: Team | None,
    *,
    title: str,
    body: str = 'Body text.',
    status: str = MemoryStatus.APPROVED,
) -> Memory:
    return Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=body,
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
    )


def _read_stream(response: object) -> dict:
    body = b''.join(response.streaming_content).decode('utf-8')

    return json.loads(body)


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='ExportViewOrg', slug='export-view-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


@pytest.fixture
def f_reader_client(f_org: Organization) -> APIClient:
    return _client_for_org('export-reader', f_org, ('memories:read',))


@pytest.fixture
def f_admin_client(f_org: Organization) -> APIClient:
    return _client_for_org('export-admin', f_org, ('memories:read', 'memories:admin'))


@pytest.mark.django_db
def test_export_approved_returns_attachment_stream(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory')

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    assert response['Content-Type'].startswith('application/json')
    disposition = response['Content-Disposition']
    assert disposition.startswith('attachment;')
    assert 'engram-memories-export-view-org-backend-' in disposition
    assert disposition.rstrip('"').endswith('.json')

    payload = _read_stream(response)
    assert payload['project_id'] == str(f_project.id)
    assert payload['memory_count'] == 1
    assert [entry['title'] for entry in payload['memories']] == ['Approved memory']


@pytest.mark.django_db
def test_export_is_streaming_response(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='One')
    _make_memory(f_org, f_project, None, title='Two')

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    assert response.streaming is True
    payload = _read_stream(response)
    assert payload['memory_count'] == 2


@pytest.mark.django_db
def test_export_approved_only_excludes_non_approved(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory')
    _make_memory(f_org, f_project, None, title='Archived memory', status=MemoryStatus.ARCHIVED)

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    payload = _read_stream(response)
    assert [entry['title'] for entry in payload['memories']] == ['Approved memory']


@pytest.mark.django_db
def test_export_all_statuses_requires_admin(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory')

    response = f_reader_client.get(
        '/v1/admin/memories/export',
        {'project_id': str(f_project.id), 'all_statuses': 'true'},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_export_all_statuses_with_admin_includes_non_approved(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory')
    _make_memory(f_org, f_project, None, title='Archived memory', status=MemoryStatus.ARCHIVED)

    response = f_admin_client.get(
        '/v1/admin/memories/export',
        {'project_id': str(f_project.id), 'all_statuses': 'true'},
    )

    assert response.status_code == 200
    payload = _read_stream(response)
    assert sorted(entry['title'] for entry in payload['memories']) == [
        'Approved memory',
        'Archived memory',
    ]


@pytest.mark.django_db
def test_export_missing_project_id_returns_400(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/memories/export')

    assert response.status_code == 400


@pytest.mark.django_db
def test_export_invalid_project_id_returns_400(f_reader_client: APIClient) -> None:
    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': 'not-a-uuid'})

    assert response.status_code == 400


@pytest.mark.django_db
def test_export_unknown_project_returns_404(f_reader_client: APIClient) -> None:
    import uuid

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(uuid.uuid4())})

    assert response.status_code == 404


@pytest.mark.django_db
def test_export_other_org_project_returns_404(f_reader_client: APIClient) -> None:
    other_org = Organization.objects.create(name='OtherExportOrg', slug='other-export-org')
    other_project = Project.objects.create(organization=other_org, name='Other', slug='other')
    _make_memory(other_org, other_project, None, title='Other org memory')

    response = f_reader_client.get(
        '/v1/admin/memories/export',
        {'project_id': str(other_project.id)},
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_export_writes_audit_event_with_counts_and_flags(
    f_admin_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory', body='secret body content')
    _make_memory(f_org, f_project, None, title='Archived memory', status=MemoryStatus.ARCHIVED)

    response = f_admin_client.get(
        '/v1/admin/memories/export',
        {'project_id': str(f_project.id), 'all_statuses': 'true'},
    )

    assert response.status_code == 200

    audit = AuditEvent.objects.get(organization=f_org, event_type='MemoryExported')
    assert audit.target_type == 'project'
    assert audit.target_id == str(f_project.id)
    assert audit.metadata['memory_count'] == 2
    assert audit.metadata['all_statuses'] is True
    assert audit.metadata['project_id'] == str(f_project.id)
    serialized_metadata = json.dumps(audit.metadata)
    assert 'Approved memory' not in serialized_metadata
    assert 'secret body content' not in serialized_metadata


@pytest.mark.django_db
def test_export_approved_audit_flag_false(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_org, f_project, None, title='Approved memory')

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    audit = AuditEvent.objects.get(organization=f_org, event_type='MemoryExported')
    assert audit.metadata['all_statuses'] is False
    assert audit.metadata['memory_count'] == 1


@pytest.mark.django_db
def test_export_redacts_token_shaped_values_in_stream(
    f_reader_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(
        f_org,
        f_project,
        None,
        title=f'Redact {LEAKED_TOKEN}',
        body=f'Body leaks {LEAKED_TOKEN}.',
    )

    response = f_reader_client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code == 200
    body = b''.join(response.streaming_content).decode('utf-8')
    assert LEAKED_TOKEN not in body
    assert '[REDACTED]' in body


@pytest.mark.django_db
def test_export_requires_authentication(f_project: Project) -> None:
    client = APIClient()

    response = client.get('/v1/admin/memories/export', {'project_id': str(f_project.id)})

    assert response.status_code in {401, 403}
