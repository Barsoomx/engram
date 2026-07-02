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
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import Organization, Project
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
