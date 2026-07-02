from __future__ import annotations

import pytest

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
from engram.access.services import (
    AccessDeniedError,
    ResolveApiKeyScope,
    api_key_fingerprint,
    api_key_prefix,
    hash_api_key,
)
from engram.core.models import Organization, Project

PLAINTEXT = 'egk_agent_scope_test_0123456789abcdefghij'


def _capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def _org_with_owner(capability_codes: tuple[str, ...]) -> tuple[Organization, Identity]:
    org = Organization.objects.create(name='Acme', slug='acme')
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in capability_codes:
        RoleCapability.objects.get_or_create(role=role, capability=_capability(code))
    identity = Identity.objects.create(
        organization=org,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='acme-owner',
        display_name='Owner',
        active=True,
    )
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role, active=True)

    return org, identity


def _org_scoped_key(
    org: Organization,
    identity: Identity,
    capability_codes: tuple[str, ...],
) -> ApiKey:
    key = ApiKey.objects.create(
        organization=org,
        owner_identity=identity,
        name='agent',
        key_prefix=api_key_prefix(PLAINTEXT),
        key_hash=hash_api_key(PLAINTEXT),
        key_fingerprint=api_key_fingerprint(PLAINTEXT),
        active=True,
    )
    for code in capability_codes:
        ApiKeyCapability.objects.get_or_create(api_key=key, capability=_capability(code))

    return key


def _resolve(project_id: object) -> object:
    return ResolveApiKeyScope().execute(
        raw_key=PLAINTEXT,
        required_capability='observations:write',
        requested_project_id=project_id,
        requested_team_id=None,
        request_id='req-1',
        correlation_id='corr-1',
        target_type='hook_event',
        target_id='evt-1',
    )


@pytest.mark.django_db
def test_agent_key_resolves_any_project_in_its_org() -> None:
    caps = ('observations:write', 'projects:agent')
    org, identity = _org_with_owner(caps)
    _org_scoped_key(org, identity, caps)
    project = Project.objects.create(organization=org, name='p', slug='p', repository_url='')

    scope = _resolve(project.id)

    assert project.id in scope.project_ids


@pytest.mark.django_db
def test_non_agent_org_scoped_key_is_denied_for_project() -> None:
    caps = ('observations:write',)
    org, identity = _org_with_owner(caps)
    _org_scoped_key(org, identity, caps)
    project = Project.objects.create(organization=org, name='p', slug='p', repository_url='')

    with pytest.raises(AccessDeniedError) as excinfo:
        _resolve(project.id)

    assert excinfo.value.code == 'project_scope_denied'
