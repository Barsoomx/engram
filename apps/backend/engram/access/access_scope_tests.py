from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
    TeamMembership,
)
from engram.access.services import (
    AccessDeniedError,
    ResolveApiKeyScope,
    api_key_fingerprint,
    api_key_prefix,
    hash_api_key,
)
from engram.core.models import AuditEvent, AuditResult, Organization, Project, ProjectTeam, Team

RAW_KEY = 'egk_test_auth_scope_0123456789abcdefghijklmnopqrstuvwxyz'


def deactivate_key(api_key: ApiKey, _owner: Identity) -> None:
    api_key.active = False


def revoke_key(api_key: ApiKey, _owner: Identity) -> None:
    api_key.revoked_at = timezone.now()


def expire_key(api_key: ApiKey, _owner: Identity) -> None:
    api_key.expires_at = timezone.now() - timedelta(seconds=1)


def deactivate_owner(_api_key: ApiKey, owner: Identity) -> None:
    owner.active = False


def create_project_scope() -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    ProjectTeam.objects.create(organization=organization, team=team, project=project)

    return organization, team, project


def create_owner(
    organization: Organization,
    *,
    external_id: str = 'svc-hooks',
    role_code: str = 'developer',
) -> Identity:
    identity = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id=external_id,
        display_name='Hook service account',
    )
    role = Role.objects.get(code=role_code)
    OrganizationMembership.objects.create(organization=organization, identity=identity, role=role)

    return identity


def grant_project_access(
    organization: Organization,
    project: Project,
    owner: Identity,
    *,
    role_code: str = 'developer',
) -> ProjectGrant:
    return ProjectGrant.objects.create(
        organization=organization,
        project=project,
        identity=owner,
        role=Role.objects.get(code=role_code),
    )


def create_scoped_api_key(
    organization: Organization,
    team: Team | None,
    project: Project | None,
    owner: Identity,
    *,
    raw_key: str = RAW_KEY,
    capabilities: tuple[str, ...] = ('observations:write', 'memories:read'),
) -> ApiKey:
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name='Hook key',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        team=team,
        project=project,
    )
    for capability_code in capabilities:
        ApiKeyCapability.objects.create(
            api_key=api_key,
            capability=Capability.objects.get(code=capability_code),
        )

    return api_key


@pytest.mark.django_db
def test_default_capabilities_and_roles_are_seeded() -> None:
    capability_codes = set(Capability.objects.values_list('code', flat=True))
    role_codes = set(Role.objects.values_list('code', flat=True))

    assert {
        'members:*',
        'teams:*',
        'projects:*',
        'api_keys:*',
        'observations:write',
        'memories:read',
        'memories:propose',
        'search:query',
        'audit:read',
        'policy:admin',
    }.issubset(capability_codes)
    assert {'organization_owner', 'organization_admin', 'developer', 'auditor'}.issubset(role_codes)

    developer_capabilities = set(
        RoleCapability.objects.filter(role__code='developer').values_list('capability__code', flat=True),
    )

    assert {'observations:write', 'memories:read', 'memories:propose', 'search:query'}.issubset(
        developer_capabilities,
    )


@pytest.mark.django_db
def test_api_key_stores_hash_and_fingerprint_without_raw_secret() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization)

    api_key = create_scoped_api_key(organization, team, project, owner)

    assert api_key.key_prefix == 'egk_test_aut'
    assert api_key.key_hash != RAW_KEY
    assert api_key.key_fingerprint != RAW_KEY
    assert RAW_KEY not in str(api_key.__dict__)


@pytest.mark.django_db
def test_api_key_hash_is_unique() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization)

    create_scoped_api_key(organization, team, project, owner)

    with pytest.raises(IntegrityError), transaction.atomic():
        create_scoped_api_key(
            organization,
            team,
            project,
            owner,
            capabilities=('observations:write',),
        )


@pytest.mark.django_db
def test_api_key_prefix_collision_does_not_block_distinct_keys_or_resolution() -> None:
    first_raw_key = 'egk_test_same_prefix_first'
    second_raw_key = 'egk_test_same_prefix_second'
    organization, team, project = create_project_scope()
    first_owner = create_owner(organization, external_id='svc-hooks-1')
    second_owner = create_owner(organization, external_id='svc-hooks-2')
    grant_project_access(organization, project, first_owner)
    grant_project_access(organization, project, second_owner)
    first_key = create_scoped_api_key(organization, team, project, first_owner, raw_key=first_raw_key)
    second_key = create_scoped_api_key(organization, team, project, second_owner, raw_key=second_raw_key)

    scope = ResolveApiKeyScope().execute(
        raw_key=second_raw_key,
        required_capability='observations:write',
        requested_project_id=project.id,
        request_id='request-prefix-collision-1',
    )

    assert first_key.key_prefix == second_key.key_prefix
    assert first_key.key_hash != second_key.key_hash
    assert scope.api_key_id == second_key.id
    assert scope.identity_id == second_owner.id


@pytest.mark.django_db
def test_project_scoped_api_key_resolves_effective_scope_without_audit_row() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization)
    grant_project_access(organization, project, owner)
    api_key = create_scoped_api_key(organization, team, project, owner)

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='observations:write',
        requested_project_id=project.id,
        request_id='request-allow-1',
        target_type='hook_event',
        target_id='event-1',
    )

    api_key.refresh_from_db()

    assert scope.organization_id == organization.id
    assert scope.identity_id == owner.id
    assert scope.api_key_id == api_key.id
    assert scope.project_ids == (project.id,)
    assert scope.team_ids == (team.id,)
    assert 'observations:write' in scope.capabilities
    assert scope.project_bound is True
    assert api_key.last_used_at is not None
    assert not AuditEvent.objects.filter(request_id='request-allow-1').exists()


@pytest.mark.django_db
def test_api_key_capabilities_cannot_expand_owner_capabilities() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization, role_code='auditor')
    grant_project_access(organization, project, owner, role_code='auditor')
    create_scoped_api_key(organization, team, project, owner, capabilities=('observations:write',))

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='observations:write',
            requested_project_id=project.id,
            request_id='request-missing-capability-1',
        )

    audit = AuditEvent.objects.get(request_id='request-missing-capability-1')

    assert exc_info.value.code == 'missing_capability'
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['missing_capability'] == 'observations:write'
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_project_scoped_api_key_denies_another_project_in_same_organization() -> None:
    organization, team, project = create_project_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    owner = create_owner(organization)
    grant_project_access(organization, project, owner)
    create_scoped_api_key(organization, team, project, owner)

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='observations:write',
            requested_project_id=other_project.id,
            request_id='request-cross-project-1',
        )

    audit = AuditEvent.objects.get(request_id='request-cross-project-1')

    assert exc_info.value.code == 'project_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.project_id == project.id
    assert audit.metadata['requested_project_id'] == str(other_project.id)
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_project_scoped_api_key_denies_cross_organization_project_without_target_fk() -> None:
    organization, team, project = create_project_scope()
    other_organization = Organization.objects.create(name='Other', slug='other')
    other_project = Project.objects.create(organization=other_organization, name='Backend', slug='backend')
    owner = create_owner(organization)
    grant_project_access(organization, project, owner)
    create_scoped_api_key(organization, team, project, owner)

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='observations:write',
            requested_project_id=other_project.id,
            request_id='request-cross-org-project-1',
        )

    audit = AuditEvent.objects.get(request_id='request-cross-org-project-1')

    assert exc_info.value.code == 'project_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.project_id == project.id
    assert audit.metadata['requested_project_id'] == str(other_project.id)
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.parametrize(
    ('mutate_key', 'expected_code'),
    [
        (deactivate_key, 'inactive_key'),
        (revoke_key, 'revoked_key'),
        (expire_key, 'expired_key'),
        (deactivate_owner, 'inactive_owner'),
    ],
)
@pytest.mark.django_db
def test_unusable_api_key_states_are_denied(
    mutate_key: Callable[[ApiKey, Identity], None],
    expected_code: str,
) -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization)
    grant_project_access(organization, project, owner)
    api_key = create_scoped_api_key(organization, team, project, owner)
    mutate_key(api_key, owner)
    owner.save()
    api_key.save()

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='observations:write',
            requested_project_id=project.id,
            request_id=f'request-{expected_code}-1',
        )

    audit = AuditEvent.objects.get(request_id=f'request-{expected_code}-1')

    assert exc_info.value.code == expected_code
    assert audit.result == AuditResult.DENIED
    assert RAW_KEY not in str(audit.metadata)


@pytest.mark.django_db
def test_api_key_project_binding_cannot_expand_owner_project_access() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization)
    create_scoped_api_key(organization, team, project, owner)

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='observations:write',
            requested_project_id=project.id,
            request_id='request-no-owner-project-access-1',
        )

    audit = AuditEvent.objects.get(request_id='request-no-owner-project-access-1')

    assert exc_info.value.code == 'project_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['requested_project_id'] == str(project.id)


@pytest.mark.django_db
def test_unbound_api_key_needs_explicit_project_capability_to_resolve_project_scope() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team=None,
        project=None,
        owner=owner,
        capabilities=('memories:read',),
    )

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='memories:read',
            requested_project_id=project.id,
            request_id='request-unbound-missing-project-capability-1',
        )

    audit = AuditEvent.objects.get(request_id='request-unbound-missing-project-capability-1')

    assert exc_info.value.code == 'project_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.project_id is None
    assert audit.metadata['requested_project_id'] == str(project.id)


@pytest.mark.django_db
def test_unbound_api_key_with_project_capability_resolves_scope_without_audit_row() -> None:
    organization, _team, project = create_project_scope()
    owner = create_owner(organization, role_code='organization_admin')
    api_key = create_scoped_api_key(
        organization,
        team=None,
        project=None,
        owner=owner,
        capabilities=('memories:read', 'projects:*'),
    )

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='memories:read',
        requested_project_id=project.id,
        request_id='request-unbound-project-allow-1',
    )

    assert scope.api_key_id == api_key.id
    assert scope.project_ids == (project.id,)
    assert scope.team_ids == ()
    assert scope.project_bound is False
    assert not AuditEvent.objects.filter(request_id='request-unbound-project-allow-1').exists()


@pytest.mark.django_db
def test_unbound_api_key_denies_unlinked_team_hint() -> None:
    organization, _team, project = create_project_scope()
    unlinked_team = Team.objects.create(organization=organization, name='Unlinked', slug='unlinked')
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team=None,
        project=None,
        owner=owner,
        capabilities=('memories:read', 'projects:*', 'teams:*'),
    )

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='memories:read',
            requested_project_id=project.id,
            requested_team_id=unlinked_team.id,
            request_id='request-unbound-unlinked-team-1',
        )

    audit = AuditEvent.objects.get(request_id='request-unbound-unlinked-team-1')

    assert exc_info.value.code == 'team_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['requested_team_id'] == str(unlinked_team.id)


@pytest.mark.django_db
def test_unbound_api_key_denies_cross_organization_team_hint() -> None:
    organization, _team, project = create_project_scope()
    other_organization = Organization.objects.create(name='Other', slug='other')
    other_team = Team.objects.create(organization=other_organization, name='Other', slug='other')
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team=None,
        project=None,
        owner=owner,
        capabilities=('memories:read', 'projects:*', 'teams:*'),
    )

    with pytest.raises(AccessDeniedError) as exc_info:
        ResolveApiKeyScope().execute(
            raw_key=RAW_KEY,
            required_capability='memories:read',
            requested_project_id=project.id,
            requested_team_id=other_team.id,
            request_id='request-unbound-cross-org-team-1',
        )

    audit = AuditEvent.objects.get(request_id='request-unbound-cross-org-team-1')

    assert exc_info.value.code == 'team_scope_denied'
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['requested_team_id'] == str(other_team.id)


@pytest.mark.django_db
def test_access_models_reject_cross_scope_foreign_keys_on_create() -> None:
    organization, team, project = create_project_scope()
    other_organization = Organization.objects.create(name='Other', slug='other')
    other_team = Team.objects.create(organization=other_organization, name='Other', slug='other')
    other_project = Project.objects.create(organization=other_organization, name='Other Backend', slug='backend')
    owner = create_owner(organization)
    role = Role.objects.get(code='developer')

    with pytest.raises(ValidationError):
        TeamMembership.objects.create(
            organization=organization,
            team=other_team,
            identity=owner,
            role=role,
        )

    with pytest.raises(ValidationError):
        ProjectGrant.objects.create(
            organization=organization,
            project=other_project,
            identity=owner,
            role=role,
        )

    with pytest.raises(ValidationError):
        ApiKey.objects.create(
            organization=organization,
            owner_identity=owner,
            name='Cross-scope key',
            key_prefix=api_key_prefix('egk_test_cross_scope'),
            key_hash=hash_api_key('egk_test_cross_scope'),
            key_fingerprint=api_key_fingerprint('egk_test_cross_scope'),
            team=team,
            project=other_project,
        )

    unlinked_team = Team.objects.create(organization=organization, name='Unlinked', slug='unlinked')

    with pytest.raises(ValidationError):
        ApiKey.objects.create(
            organization=organization,
            owner_identity=owner,
            name='Unlinked team key',
            key_prefix=api_key_prefix('egk_test_unlinked_team'),
            key_hash=hash_api_key('egk_test_unlinked_team'),
            key_fingerprint=api_key_fingerprint('egk_test_unlinked_team'),
            team=unlinked_team,
            project=project,
        )
