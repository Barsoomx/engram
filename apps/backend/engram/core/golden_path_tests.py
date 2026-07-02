from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import Organization, Project, ProjectTeam, Team
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope

RAW_KEY = 'egk_test_golden_path_0123456789abcdefghijklmnopqrstuvwxyz'


def bootstrap_golden_path() -> dict[str, object]:
    stdout = io.StringIO()

    call_command('engram_bootstrap_golden_path', '--api-key', RAW_KEY, '--json', stdout=stdout)

    return json.loads(stdout.getvalue())


@pytest.mark.django_db
def test_bootstrap_golden_path_creates_scoped_project_key_without_raw_secret() -> None:
    body = bootstrap_golden_path()

    organization = Organization.objects.get(slug='engram-e2e')
    team = Team.objects.get(organization=organization, slug='platform')
    project = Project.objects.get(organization=organization, slug='backend')
    identity = Identity.objects.get(
        organization=organization,
        identity_type=IdentityType.SERVICE_ACCOUNT,
        external_id='golden-path-agent',
    )
    api_key = ApiKey.objects.get(organization=organization, owner_identity=identity)
    secret = ProviderSecret.objects.get(organization=organization, team=team, provider='openai')
    policy = ModelPolicy.objects.get(organization=organization, team=team, project=project, task_type='generation')
    embedding_policy = ModelPolicy.objects.get(
        organization=organization,
        team=team,
        project=project,
        task_type='embedding',
    )

    organization_generation_policy = ModelPolicy.objects.get(
        organization=organization,
        team=None,
        project=None,
        scope='organization',
        task_type='generation',
    )
    organization_embedding_policy = ModelPolicy.objects.get(
        organization=organization,
        team=None,
        project=None,
        scope='organization',
        task_type='embedding',
    )
    assert body == {
        'organization_id': str(organization.id),
        'team_id': str(team.id),
        'project_id': str(project.id),
        'identity_id': str(identity.id),
        'api_key_id': str(api_key.id),
        'api_key_fingerprint': api_key_fingerprint(RAW_KEY),
        'capabilities': ['memories:read', 'observations:write'],
        'provider_secret_id': str(secret.id),
        'generation_policy_id': str(policy.id),
        'embedding_policy_id': str(embedding_policy.id),
        'organization_generation_policy_id': str(organization_generation_policy.id),
        'organization_embedding_policy_id': str(organization_embedding_policy.id),
    }
    assert ProjectTeam.objects.filter(organization=organization, team=team, project=project).exists()
    assert OrganizationMembership.objects.filter(
        organization=organization,
        identity=identity,
        role=Role.objects.get(code='developer'),
        active=True,
    ).exists()
    assert ProjectGrant.objects.filter(
        organization=organization,
        project=project,
        identity=identity,
        role=Role.objects.get(code='developer'),
        active=True,
    ).exists()
    assert api_key.team_id == team.id
    assert api_key.project_id == project.id
    assert api_key.key_prefix == api_key_prefix(RAW_KEY)
    assert api_key.key_hash == hash_api_key(RAW_KEY)
    assert api_key.key_fingerprint == api_key_fingerprint(RAW_KEY)
    assert set(ApiKeyCapability.objects.filter(api_key=api_key).values_list('capability__code', flat=True)) == {
        'memories:read',
        'observations:write',
    }
    assert secret.active is True
    assert secret.secret_fingerprint
    assert ProviderSecretEnvelope.objects.filter(secret=secret, active=True).count() == 1
    assert policy.provider == 'openai'
    assert policy.model == 'gpt-4.1-mini'
    assert policy.secret_id == secret.id
    assert embedding_policy.provider == 'openai'
    assert embedding_policy.model == 'text-embedding-3-small'
    assert embedding_policy.secret_id == secret.id
    assert RAW_KEY not in str(body)
    assert RAW_KEY not in str(api_key.__dict__)
    assert RAW_KEY not in str(secret.__dict__)
    assert RAW_KEY not in str(ProviderSecretEnvelope.objects.filter(secret=secret).values())


@pytest.mark.django_db
def test_bootstrap_golden_path_is_idempotent() -> None:
    first = bootstrap_golden_path()
    second = bootstrap_golden_path()

    assert second == first
    assert Organization.objects.count() == 1
    assert Team.objects.count() == 1
    assert Project.objects.count() == 1
    assert ProjectTeam.objects.count() == 1
    assert Identity.objects.count() == 1
    assert ApiKey.objects.count() == 1
    assert ApiKeyCapability.objects.count() == 2
    assert ProviderSecret.objects.count() == 1
    assert ProviderSecretEnvelope.objects.count() == 1
    assert ModelPolicy.objects.count() == 6
