from __future__ import annotations

import io

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
)
from engram.access.services import api_key_prefix, hash_api_key
from engram.core.models import Organization, Project, ProjectTeam, Team

ADMIN_CAPABILITY_CODES = (
    'organizations:admin',
    'teams:admin',
    'projects:admin',
    'members:admin',
    'api_keys:issue',
    'api_keys:revoke',
    'api_keys:read',
    'roles:read',
    'memories:read',
    'memories:propose',
    'memories:review',
    'memories:admin',
    'observations:read',
    'observations:write',
    'search:query',
    'policy:admin',
)


def _run(stdout: io.StringIO, **options: object) -> None:
    call_command('engram_bootstrap_admin', stdout=stdout, **options)


@pytest.mark.django_db
def test_bootstrap_creates_owner_membership_with_owner_role() -> None:
    stdout = io.StringIO()

    _run(stdout, api_key='engram-test-admin-key-1234567890')

    organization = Organization.objects.get(slug='default')
    user = User.objects.get(username='admin')

    assert user.is_superuser is True

    assert user.is_staff is True

    identity = Identity.objects.get(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=f'django-user:{user.id}',
    )

    membership = OrganizationMembership.objects.get(
        organization=organization,
        identity=identity,
    )

    assert membership.role.code == 'organization_owner'

    assert membership.active is True


@pytest.mark.django_db
def test_bootstrap_creates_default_team_project_and_link() -> None:
    stdout = io.StringIO()

    _run(stdout, api_key='engram-test-admin-key-1234567890')

    organization = Organization.objects.get(slug='default')
    team = Team.objects.get(organization=organization, slug='default')
    project = Project.objects.get(organization=organization, slug='default')

    assert ProjectTeam.objects.filter(
        organization=organization,
        team=team,
        project=project,
    ).exists()


@pytest.mark.django_db
def test_bootstrap_api_key_has_admin_capabilities() -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    stdout = io.StringIO()

    _run(stdout, api_key=raw_key)

    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    granted = set(ApiKeyCapability.objects.filter(api_key=api_key).values_list('capability__code', flat=True))

    for code in ADMIN_CAPABILITY_CODES:
        assert code in granted, f'missing capability {code}'


@pytest.mark.django_db
def test_bootstrap_api_key_owner_is_admin_identity() -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    stdout = io.StringIO()

    _run(stdout, api_key=raw_key)

    api_key = ApiKey.objects.get(key_hash=hash_api_key(raw_key))
    organization = Organization.objects.get(slug='default')
    user = User.objects.get(username='admin')

    assert api_key.owner_identity.identity_type == IdentityType.USER

    assert api_key.owner_identity.external_id == f'django-user:{user.id}'

    assert api_key.organization_id == organization.id

    assert api_key.key_prefix == api_key_prefix(raw_key)


@pytest.mark.django_db
def test_bootstrap_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', 'fixed-strong-secret-42')

    stdout_first = io.StringIO()
    _run(stdout_first, api_key=raw_key)

    stdout_second = io.StringIO()
    _run(stdout_second, api_key=raw_key)

    assert Organization.objects.count() == 1

    assert Team.objects.count() == 1

    assert Project.objects.count() == 1

    assert User.objects.count() == 1

    assert Identity.objects.count() == 1

    assert OrganizationMembership.objects.count() == 1

    assert ApiKey.objects.count() == 1


@pytest.mark.django_db
def test_bootstrap_rerun_does_not_reset_password(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', 'fixed-strong-secret-42')

    stdout_first = io.StringIO()
    _run(stdout_first, api_key=raw_key)

    user = User.objects.get(username='admin')
    first_password = user.password

    stdout_second = io.StringIO()
    _run(stdout_second, api_key=raw_key)

    user.refresh_from_db()

    assert user.password == first_password


@pytest.mark.django_db
def test_bootstrap_generates_password_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    monkeypatch.delenv('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', raising=False)

    stdout = io.StringIO()
    _run(stdout, api_key=raw_key)

    output = stdout.getvalue()
    assert 'Generated admin password:' in output

    user = User.objects.get(username='admin')

    assert user.check_password(_extract_generated_password(output)) is True


@pytest.mark.django_db
def test_bootstrap_prints_raw_api_key_once(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', 'fixed-strong-secret-42')

    stdout = io.StringIO()
    _run(stdout, api_key=raw_key)

    output = stdout.getvalue()
    assert raw_key in output

    assert 'bootstrap complete' in output.lower()


@pytest.mark.django_db
def test_bootstrap_uses_env_slugs(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_key = 'engram-test-admin-key-1234567890'
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ORG_SLUG', 'acme')
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_TEAM_SLUG', 'platform')
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_PROJECT_SLUG', 'backend')
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ADMIN_USERNAME', 'root')
    monkeypatch.setenv('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', 'fixed-strong-secret-42')

    stdout = io.StringIO()
    _run(stdout, api_key=raw_key)

    assert Organization.objects.filter(slug='acme').exists()

    organization = Organization.objects.get(slug='acme')
    assert Team.objects.filter(organization=organization, slug='platform').exists()

    assert Project.objects.filter(organization=organization, slug='backend').exists()

    assert User.objects.filter(username='root').exists()


@pytest.mark.django_db
def test_bootstrap_all_admin_capabilities_exist_in_seed() -> None:
    missing = [
        code
        for code in ADMIN_CAPABILITY_CODES
        if not Capability.objects.filter(code=code).exists()
    ]

    assert missing == []


def _extract_generated_password(output: str) -> str:
    marker = 'Generated admin password:'

    for line in output.splitlines():
        if marker in line:

            return line.split(marker, 1)[1].strip()

    raise AssertionError('generated password not found in output')
