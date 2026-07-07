from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.contrib.auth.models import User
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
from engram.core.models import Organization, Project
from engram.imports.models import ImportJob, ImportJobStatus


@dataclass(frozen=True)
class ConsoleScope:
    organization: Organization
    project: Project
    client: APIClient


def _ensure_capability(code: str) -> Capability:
    capability, _created = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def _console_scope(slug: str, capabilities: tuple[str, ...]) -> ConsoleScope:
    user = User.objects.create_user(username=f'reader-{slug}', password='strong-secret-123')  # noqa: S106
    organization = Organization.objects.create(name=f'Org {slug}', slug=f'org-{slug}')
    project = Project.objects.create(organization=organization, name=f'Project {slug}', slug=f'project-{slug}')
    identity, _created = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )
    role, _created = Role.objects.get_or_create(code=f'reader-{slug}', defaults={'name': 'Reader'})
    for code in capabilities:
        RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(code))
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.create(user=user).key
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {token}', HTTP_X_ENGRAM_ORGANIZATION=str(organization.id))

    return ConsoleScope(organization=organization, project=project, client=client)


def _make_job(organization: Organization, project: Project, source_store_id: str) -> ImportJob:
    return ImportJob.objects.create(
        organization=organization,
        project=project,
        source_store_id=source_store_id,
        status=ImportJobStatus.SUCCEEDED,
        rows_created=5,
    )


@pytest.fixture
def f_reader() -> ConsoleScope:
    return _console_scope('reader', ('memories:read',))


@pytest.mark.django_db
def test_list_is_org_scoped(f_reader: ConsoleScope) -> None:
    _make_job(f_reader.organization, f_reader.project, 'own-store')

    other = _console_scope('other', ('memories:read',))
    _make_job(other.organization, other.project, 'foreign-store')

    response = f_reader.client.get('/v1/admin/imports/')

    assert response.status_code == 200
    stores = {entry['source_store_id'] for entry in response.data['results']}
    assert stores == {'own-store'}


@pytest.mark.django_db
def test_detail_returns_job(f_reader: ConsoleScope) -> None:
    job = _make_job(f_reader.organization, f_reader.project, 'detail-store')

    response = f_reader.client.get(f'/v1/admin/imports/{job.id}/')

    assert response.status_code == 200
    assert response.data['source_store_id'] == 'detail-store'
    assert response.data['rows_created'] == 5


@pytest.mark.django_db
def test_requires_memories_read_capability() -> None:
    scope = _console_scope('nocap', ('audit:read',))
    _make_job(scope.organization, scope.project, 'store')

    response = scope.client.get('/v1/admin/imports/')

    assert response.status_code == 403
