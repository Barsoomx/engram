from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

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
    VisibilityScope,
)
from engram.memory.services import WeeklyDigestResult


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='test-pass-123')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(
        code=code,
        defaults={'description': code},
    )

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for cap_code in capability_codes:
        capability = _ensure_capability(cap_code)
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_client(token: str, organization: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(organization.id),
    )

    return client


def _make_digest_memory(org: Organization, project: Project, ready: bool = False) -> Memory:
    return Memory.objects.create(
        organization=org,
        project=project,
        title='Weekly Structured Digest 2026-06-23 to 2026-06-30',
        body='Structured weekly digest.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={
            'kind': 'digest',
            'digest_kind': 'weekly_structured',
            'window_start': '2026-06-23T00:00:00+00:00',
            'window_end': '2026-06-30T00:00:00+00:00',
            'window_days': 7,
            'memory_changes': {
                'refuted': [],
                'retired': [],
                'superseded': [],
                'merged': [],
                'added': [{'id': str(uuid.uuid4()), 'title': 'mem', 'at': '2026-06-25T12:00:00+00:00'}],
            },
            'counts': {'refuted': 0, 'retired': 0, 'superseded': 0, 'merged': 0, 'added': 1},
            'content_hash': 'abc123',
            'ready': ready,
            'reviewed_at': None,
        },
    )


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Digest View Org', slug='digest-view-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_org,
        name='digest-view-project',
        slug='digest-view-project',
    )


@pytest.fixture
def f_other_org() -> Organization:
    return Organization.objects.create(name='Other Digest Org', slug='other-digest-org')


@pytest.fixture
def f_other_project(f_other_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_other_org,
        name='other-digest-project',
        slug='other-digest-project',
    )


@pytest.fixture
def f_read_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-read-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_read_role', ('memories:read',))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_review_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-review-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_review_role', ('memories:read', 'memories:review'))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_no_cap_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-no-cap-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_no_cap_role', ())
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


def _fake_weekly_result(project: Project) -> WeeklyDigestResult:
    digest_memory = MagicMock(spec=Memory)
    digest_memory.id = uuid.uuid4()
    digest_memory.metadata = {
        'kind': 'digest',
        'digest_kind': 'weekly_structured',
        'window_start': '2026-06-23T00:00:00+00:00',
        'window_end': '2026-06-30T00:00:00+00:00',
        'window_days': 7,
        'ready': False,
    }

    return WeeklyDigestResult(
        digest_memory=digest_memory,
        counts={'refuted': 0, 'retired': 0, 'superseded': 0, 'merged': 0, 'added': 1},
        memory_changes={
            'refuted': [],
            'retired': [],
            'superseded': [],
            'merged': [],
            'added': [{'id': str(uuid.uuid4()), 'title': 'added-mem', 'at': '2026-06-25T12:00:00+00:00'}],
        },
        ready=False,
    )


@pytest.mark.django_db
def test_get_weekly_digest_returns_expected_structure(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    with patch('engram.console.views.digests.BuildWeeklyStructuredDigest') as m_service_cls:
        m_service_cls.return_value.execute.return_value = _fake_weekly_result(f_project)

        response = f_read_client.get(
            '/v1/admin/digests/weekly',
            {'project_id': str(f_project.id)},
        )

    assert response.status_code == 200

    data = response.data

    assert 'window_start' in data

    assert 'window_end' in data

    assert 'window_days' in data

    assert 'counts' in data

    assert 'memory_changes' in data

    assert 'changelog' in data

    assert 'ready' in data

    assert isinstance(data['changelog'], list)

    assert data['ready'] is False


@pytest.mark.django_db
def test_get_weekly_digest_changelog_flattens_buckets(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    with patch('engram.console.views.digests.BuildWeeklyStructuredDigest') as m_service_cls:
        m_service_cls.return_value.execute.return_value = _fake_weekly_result(f_project)

        response = f_read_client.get(
            '/v1/admin/digests/weekly',
            {'project_id': str(f_project.id)},
        )

    assert response.status_code == 200

    changelog = response.data['changelog']

    assert len(changelog) == 1

    entry = changelog[0]

    assert entry['bucket'] == 'added'

    assert 'id' in entry

    assert 'title' in entry

    assert 'at' in entry


@pytest.mark.django_db
def test_get_weekly_digest_requires_project_id(
    f_read_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_read_client.get('/v1/admin/digests/weekly')

    assert response.status_code == 400


@pytest.mark.django_db
def test_get_weekly_digest_requires_memories_read_capability(
    f_no_cap_client: APIClient,
    f_project: Project,
) -> None:
    response = f_no_cap_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_get_weekly_digest_requires_authentication(
    f_project: Project,
) -> None:
    client = APIClient()

    response = client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_get_weekly_digest_returns_404_for_unknown_project(
    f_read_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(uuid.uuid4())},
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_flips_ready_to_true(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_digest_memory(f_org, f_project, ready=False)

    response = f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 200

    assert response.data['memory_id'] == str(digest.id)

    assert response.data['reviewed'] is True

    assert response.data['ready'] is True

    digest.refresh_from_db()

    assert digest.metadata['ready'] is True

    assert digest.metadata['reviewed_at'] is not None


@pytest.mark.django_db
def test_post_digest_review_writes_audit_event(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_digest_memory(f_org, f_project)

    f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    event = AuditEvent.objects.filter(
        organization=f_org,
        event_type='DigestReviewed',
        target_id=str(digest.id),
    ).first()

    assert event is not None

    assert event.target_type == 'memory'


@pytest.mark.django_db
def test_post_digest_review_requires_memories_review_capability(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_digest_memory(f_org, f_project)

    response = f_read_client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 403


@pytest.mark.django_db
def test_post_digest_review_requires_authentication(
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_digest_memory(f_org, f_project)

    client = APIClient()

    response = client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 401


@pytest.mark.django_db
def test_post_digest_review_tenant_isolation(
    f_review_client: APIClient,
    f_other_org: Organization,
    f_other_project: Project,
) -> None:
    other_digest = _make_digest_memory(f_other_org, f_other_project)

    response = f_review_client.post(f'/v1/admin/digests/{other_digest.id}/review')

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_returns_404_for_nonexistent_memory(
    f_review_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_review_client.post(f'/v1/admin/digests/{uuid.uuid4()}/review')

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_returns_404_for_non_digest_memory(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    plain_memory = Memory.objects.create(
        organization=f_org,
        project=f_project,
        title='plain memory',
        body='body',
        status=MemoryStatus.APPROVED,
    )

    response = f_review_client.post(f'/v1/admin/digests/{plain_memory.id}/review')

    assert response.status_code == 404
