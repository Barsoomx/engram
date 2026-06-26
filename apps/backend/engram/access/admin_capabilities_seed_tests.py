from __future__ import annotations

import pytest
from django.core.management import call_command

from engram.access.models import Capability, Role, RoleCapability


def _role_caps(role_code: str) -> set[str]:
    return set(
        RoleCapability.objects.filter(role__code=role_code).values_list(
            'capability__code', flat=True
        )
    )


@pytest.mark.django_db
def test_owner_has_all_admin_capabilities() -> None:
    caps = _role_caps('organization_owner')

    required = {
        'organizations:read',
        'organizations:admin',
        'teams:read',
        'teams:admin',
        'projects:read',
        'projects:admin',
        'members:read',
        'members:admin',
        'roles:read',
        'api_keys:read',
        'api_keys:issue',
        'api_keys:revoke',
    }

    missing = required - caps

    assert not missing, f'missing owner capabilities: {sorted(missing)}'


@pytest.mark.django_db
def test_organization_admin_capabilities() -> None:
    caps = _role_caps('organization_admin')

    required = {
        'teams:admin',
        'projects:admin',
        'members:admin',
        'api_keys:read',
        'api_keys:issue',
        'api_keys:revoke',
        'roles:read',
        'organizations:read',
        'teams:read',
        'projects:read',
        'members:read',
    }

    missing = required - caps

    assert not missing, f'missing admin capabilities: {sorted(missing)}'


@pytest.mark.django_db
def test_developer_capabilities() -> None:
    caps = _role_caps('developer')

    required = {'projects:read', 'teams:read', 'api_keys:read'}

    missing = required - caps

    assert not missing, f'missing developer capabilities: {sorted(missing)}'


@pytest.mark.django_db
def test_auditor_has_read_only_admin_capabilities() -> None:
    caps = _role_caps('auditor')

    read_codes = {
        'organizations:read',
        'teams:read',
        'projects:read',
        'members:read',
        'roles:read',
        'api_keys:read',
        'audit:read',
    }

    missing = read_codes - caps

    assert not missing, f'missing auditor read capabilities: {sorted(missing)}'

    write_codes = {c for c in caps if c.endswith(':admin')}
    write_codes |= {c for c in caps if c in {'api_keys:issue', 'api_keys:revoke'}}

    assert not write_codes, f'auditor must not have write capabilities: {sorted(write_codes)}'


@pytest.mark.django_db
def test_wildcard_capabilities_exist() -> None:
    codes = set(Capability.objects.values_list('code', flat=True))

    required = {'api_keys:*', 'members:*', 'teams:*', 'projects:*'}

    missing = required - codes

    assert not missing, f'missing wildcard capabilities: {sorted(missing)}'


@pytest.mark.django_db
def test_migration_is_idempotent() -> None:
    call_command('migrate', 'access', run_syncdb=False, verbosity=0)
    call_command('migrate', 'access', run_syncdb=False, verbosity=0)

    link_count = RoleCapability.objects.filter(
        role__code='organization_owner',
        capability__code='teams:admin',
    ).count()

    assert link_count == 1

    cap_count = Capability.objects.filter(code='teams:admin').count()

    assert cap_count == 1

    role_count = Role.objects.filter(code='organization_owner').count()

    assert role_count == 1
