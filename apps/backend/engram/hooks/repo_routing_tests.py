from __future__ import annotations

import pytest

from engram.access.access_scope_tests import (
    create_owner,
    create_scoped_api_key,
    grant_project_access,
)
from engram.access.access_scope_tests import (
    create_project_scope as create_bound_project_scope,
)
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
from engram.access.services import AccessDeniedError, api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import AuditEvent, AuditResult, Organization, Project
from engram.core.repository import ProjectNotFoundError
from engram.hooks.services import HookEventInput, IngestHookEvent

PLAINTEXT = 'egk_repo_routing_test_0123456789abcdef'
AGENT_CAPS = ('observations:write', 'memories:read', 'search:query', 'projects:agent')


def _capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


@pytest.fixture
def f_agent_org() -> Organization:
    org = Organization.objects.create(name='Acme', slug='acme')
    role, _ = Role.objects.get_or_create(code='organization_owner', defaults={'name': 'owner'})
    for code in AGENT_CAPS:
        RoleCapability.objects.get_or_create(role=role, capability=_capability(code))
    identity = Identity.objects.create(
        organization=org,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='acme-agent',
        display_name='Agent',
        active=True,
    )
    OrganizationMembership.objects.create(organization=org, identity=identity, role=role, active=True)
    key = ApiKey.objects.create(
        organization=org,
        owner_identity=identity,
        name='agent',
        key_prefix=api_key_prefix(PLAINTEXT),
        key_hash=hash_api_key(PLAINTEXT),
        key_fingerprint=api_key_fingerprint(PLAINTEXT),
        active=True,
    )
    for code in AGENT_CAPS:
        ApiKeyCapability.objects.get_or_create(api_key=key, capability=_capability(code))

    return org


def _hook_input(**overrides: object) -> HookEventInput:
    base: dict[str, object] = {
        'raw_key': PLAINTEXT,
        'project_id': None,
        'team_id': None,
        'agent_runtime': 'claude_code',
        'agent_version': '',
        'agent_external_id': 'agent-1',
        'session_id': 'sess-1',
        'event_id': 'evt-1',
        'idempotency_key': 'evt-1',
        'event_type': 'post_tool_use',
        'payload_schema_version': 'v1',
        'sequence_number': None,
        'occurred_at': None,
        'content_hash': 'hash-1',
        'request_id': 'req-1',
        'correlation_id': '',
        'trace_id': '',
        'repository_url': 'https://github.com/Barsoomx/Engram.git',
        'repository_root': '/home/dev/engram',
        'branch': 'master',
        'cwd': '/home/dev/engram',
        'payload': {'tool': 'edit'},
        'observation': {},
    }
    base.update(overrides)

    return HookEventInput(**base)


@pytest.mark.django_db
def test_hook_without_project_id_routes_to_autocreated_project(f_agent_org: Organization) -> None:
    result = IngestHookEvent().execute(_hook_input())

    project = Project.objects.get(
        organization=f_agent_org,
        repository_url='git@github.com:barsoomx/engram.git',
    )
    assert result.raw_event.project_id == project.id
    assert Project.objects.filter(organization=f_agent_org).count() == 1


@pytest.mark.django_db
def test_hook_reuses_existing_project_for_same_repo(f_agent_org: Organization) -> None:
    existing = Project.objects.create(
        organization=f_agent_org,
        name='barsoomx/engram',
        slug='barsoomx-engram',
        repository_url='git@github.com:barsoomx/engram.git',
    )

    result = IngestHookEvent().execute(_hook_input(repository_url='git@github.com:barsoomx/engram.git'))

    assert result.raw_event.project_id == existing.id
    assert Project.objects.filter(organization=f_agent_org).count() == 1


BOUND_RAW_KEY = 'egk_repo_routing_bound_test_0123456789abcdefghij'


def _bound_hook_input(**overrides: object) -> HookEventInput:
    base: dict[str, object] = {'raw_key': BOUND_RAW_KEY}
    base.update(overrides)

    return _hook_input(**base)


@pytest.mark.django_db
def test_hook_project_scoped_key_denies_foreign_in_org_repository_url() -> None:
    organization, team, project = create_bound_project_scope()
    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-hook',
        repository_url='git@github.com:acme/foreign-hook.git',
    )
    owner = create_owner(organization, role_code='organization_admin')
    grant_project_access(organization, project, owner)
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=BOUND_RAW_KEY,
        capabilities=('observations:write',),
    )

    with pytest.raises(AccessDeniedError) as excinfo:
        IngestHookEvent().execute(
            _bound_hook_input(project_id=None, repository_url='https://github.com/acme/foreign-hook'),
        )

    assert excinfo.value.code == 'project_scope_denied'
    audit = AuditEvent.objects.get(event_type='AccessScopeResolved', project_id=foreign_project.id)
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['resolved_project_id'] == str(foreign_project.id)


@pytest.mark.django_db
def test_hook_bound_key_with_agent_capability_denied_for_foreign_project() -> None:
    organization, team, project = create_bound_project_scope()
    Project.objects.create(
        organization=organization,
        name='Foreign',
        slug='foreign-hook-footgun',
        repository_url='git@github.com:acme/foreign-hook-footgun.git',
    )
    owner = create_owner(organization, role_code='organization_admin')
    grant_project_access(organization, project, owner)
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=BOUND_RAW_KEY,
        capabilities=('observations:write', 'projects:agent'),
    )

    with pytest.raises(AccessDeniedError) as excinfo:
        IngestHookEvent().execute(
            _bound_hook_input(project_id=None, repository_url='https://github.com/acme/foreign-hook-footgun'),
        )

    assert excinfo.value.code == 'project_scope_denied'


@pytest.mark.django_db
def test_hook_bound_key_with_agent_capability_allowed_for_own_project() -> None:
    organization, team, project = create_bound_project_scope()
    project.repository_url = 'git@github.com:acme/own-hook-project.git'
    project.save(update_fields=['repository_url'])
    owner = create_owner(organization, role_code='organization_admin')
    grant_project_access(organization, project, owner)
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=BOUND_RAW_KEY,
        capabilities=('observations:write', 'projects:agent'),
    )

    result = IngestHookEvent().execute(
        _bound_hook_input(project_id=None, repository_url='https://github.com/acme/own-hook-project'),
    )

    assert result.raw_event.project_id == project.id


@pytest.mark.django_db
def test_hook_bound_key_unknown_repository_url_no_longer_auto_creates() -> None:
    organization, team, project = create_bound_project_scope()
    owner = create_owner(organization, role_code='organization_admin')
    grant_project_access(organization, project, owner)
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        raw_key=BOUND_RAW_KEY,
        capabilities=('observations:write',),
    )
    project_count_before = Project.objects.filter(organization=organization).count()

    with pytest.raises(ProjectNotFoundError):
        IngestHookEvent().execute(
            _bound_hook_input(project_id=None, repository_url='https://github.com/acme/never-created-hook'),
        )

    assert Project.objects.filter(organization=organization).count() == project_count_before
