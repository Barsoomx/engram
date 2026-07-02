from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import Organization, OrganizationSettings, Project, ProjectTeam, Team
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import SECRET_KEY_VERSION, encrypt_secret, secret_fingerprint, secret_hmac

GOLDEN_PATH_CAPABILITIES = ('memories:read', 'observations:write')
AGENT_KEY_CAPABILITIES = ('memories:read', 'observations:write', 'search:query', 'projects:agent')
GOLDEN_PATH_PROVIDER_SECRET = 'sk-engram_golden_path_local_provider_secret_1234567890'


class Command(BaseCommand):
    help = 'Create the deterministic local golden-path project, identity, and scoped API key.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--api-key', required=True)
        parser.add_argument('--agent-key', default='', dest='agent_key')
        parser.add_argument('--provider-base-url', default='', dest='provider_base_url')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        result = bootstrap_golden_path(
            str(options['api_key']),
            agent_key=str(options['agent_key']) or None,
            provider_base_url=str(options['provider_base_url']) or None,
        )
        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))

            return

        self.stdout.write(f'organization_id={result["organization_id"]}')
        self.stdout.write(f'project_id={result["project_id"]}')
        self.stdout.write(f'team_id={result["team_id"]}')
        self.stdout.write(f'api_key_fingerprint={result["api_key_fingerprint"]}')


def bootstrap_golden_path(
    raw_key: str,
    *,
    agent_key: str | None = None,
    provider_base_url: str | None = None,
) -> dict[str, object]:
    policy_metadata = {'base_url': provider_base_url} if provider_base_url else {}
    with transaction.atomic():
        organization, _created = Organization.objects.update_or_create(
            slug='engram-e2e',
            defaults={'name': 'Engram E2E'},
        )
        OrganizationSettings.objects.update_or_create(
            organization=organization,
            defaults={'distillation_auto_approve_threshold': Decimal('0.000')},
        )
        team, _created = Team.objects.update_or_create(
            organization=organization,
            slug='platform',
            defaults={'name': 'Platform'},
        )
        project, _created = Project.objects.update_or_create(
            organization=organization,
            slug='backend',
            defaults={
                'name': 'Backend',
                'repository_url': 'https://example.test/engram.git',
                'repository_root': '/workspace/engram',
                'default_branch': 'master',
            },
        )
        ProjectTeam.objects.get_or_create(organization=organization, team=team, project=project)
        developer = Role.objects.get(code='developer')
        identity, _created = Identity.objects.update_or_create(
            organization=organization,
            identity_type=IdentityType.SERVICE_ACCOUNT,
            external_id='golden-path-agent',
            defaults={
                'display_name': 'Golden path agent',
                'active': True,
            },
        )
        OrganizationMembership.objects.update_or_create(
            organization=organization,
            identity=identity,
            defaults={
                'role': developer,
                'active': True,
            },
        )
        ProjectGrant.objects.update_or_create(
            organization=organization,
            project=project,
            identity=identity,
            defaults={
                'role': developer,
                'active': True,
            },
        )
        api_key, _created = ApiKey.objects.update_or_create(
            key_hash=hash_api_key(raw_key),
            defaults={
                'organization': organization,
                'owner_identity': identity,
                'name': 'Golden path key',
                'key_prefix': api_key_prefix(raw_key),
                'key_fingerprint': api_key_fingerprint(raw_key),
                'team': team,
                'project': project,
                'active': True,
            },
        )
        for capability in Capability.objects.filter(code__in=GOLDEN_PATH_CAPABILITIES):
            ApiKeyCapability.objects.get_or_create(api_key=api_key, capability=capability)

        provider_secret, _created = ProviderSecret.objects.update_or_create(
            organization=organization,
            team=team,
            name='Golden path OpenAI',
            provider='openai',
            scope='team',
            defaults={
                'current_version': 1,
                'active': True,
                'rotation_state': 'active',
                'secret_fingerprint': secret_fingerprint(GOLDEN_PATH_PROVIDER_SECRET),
            },
        )
        ProviderSecretEnvelope.objects.get_or_create(
            organization=organization,
            team=team,
            secret=provider_secret,
            version=1,
            defaults={
                'key_version': SECRET_KEY_VERSION,
                'ciphertext': encrypt_secret(GOLDEN_PATH_PROVIDER_SECRET),
                'hmac_digest': secret_hmac(GOLDEN_PATH_PROVIDER_SECRET),
                'active': True,
            },
        )
        generation_policy, _created = ModelPolicy.objects.update_or_create(
            organization=organization,
            team=team,
            project=project,
            task_type='generation',
            scope='project',
            defaults={
                'name': 'Golden path generation',
                'provider': 'openai',
                'model': 'gpt-4.1-mini',
                'secret': provider_secret,
                'version': 1,
                'active': True,
                'metadata': policy_metadata,
            },
        )
        embedding_policy, _created = ModelPolicy.objects.update_or_create(
            organization=organization,
            team=team,
            project=project,
            task_type='embedding',
            scope='project',
            defaults={
                'name': 'Golden path embeddings',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'secret': provider_secret,
                'version': 1,
                'active': True,
                'metadata': policy_metadata,
            },
        )

        organization_generation_policy, _created = ModelPolicy.objects.update_or_create(
            organization=organization,
            team=None,
            project=None,
            task_type='generation',
            scope='organization',
            defaults={
                'name': 'Golden path org generation',
                'provider': 'openai',
                'model': 'gpt-4.1-mini',
                'secret': provider_secret,
                'version': 1,
                'active': True,
                'metadata': policy_metadata,
            },
        )
        organization_embedding_policy, _created = ModelPolicy.objects.update_or_create(
            organization=organization,
            team=None,
            project=None,
            task_type='embedding',
            scope='organization',
            defaults={
                'name': 'Golden path org embeddings',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'secret': provider_secret,
                'version': 1,
                'active': True,
                'metadata': policy_metadata,
            },
        )
        for organization_task_type in ('digest', 'curation'):
            ModelPolicy.objects.update_or_create(
                organization=organization,
                team=None,
                project=None,
                task_type=organization_task_type,
                scope='organization',
                defaults={
                    'name': f'Golden path org {organization_task_type}',
                    'provider': 'openai',
                    'model': 'gpt-4.1-mini',
                    'secret': provider_secret,
                    'version': 1,
                    'active': True,
                    'metadata': policy_metadata,
                },
            )

        result: dict[str, object] = {
            'organization_id': str(organization.id),
            'team_id': str(team.id),
            'project_id': str(project.id),
            'identity_id': str(identity.id),
            'api_key_id': str(api_key.id),
            'api_key_fingerprint': api_key.key_fingerprint,
            'capabilities': list(GOLDEN_PATH_CAPABILITIES),
            'provider_secret_id': str(provider_secret.id),
            'generation_policy_id': str(generation_policy.id),
            'embedding_policy_id': str(embedding_policy.id),
            'organization_generation_policy_id': str(organization_generation_policy.id),
            'organization_embedding_policy_id': str(organization_embedding_policy.id),
        }
        if agent_key:
            operator_role = Role.objects.get(code='organization_admin')
            operator_identity, _created = Identity.objects.update_or_create(
                organization=organization,
                identity_type=IdentityType.SERVICE_ACCOUNT,
                external_id='golden-path-operator',
                defaults={
                    'display_name': 'Golden path operator',
                    'active': True,
                },
            )
            OrganizationMembership.objects.update_or_create(
                organization=organization,
                identity=operator_identity,
                defaults={
                    'role': operator_role,
                    'active': True,
                },
            )
            agent_api_key, _created = ApiKey.objects.update_or_create(
                key_hash=hash_api_key(agent_key),
                defaults={
                    'organization': organization,
                    'owner_identity': operator_identity,
                    'name': 'Golden path agent key',
                    'key_prefix': api_key_prefix(agent_key),
                    'key_fingerprint': api_key_fingerprint(agent_key),
                    'team': None,
                    'project': None,
                    'active': True,
                },
            )
            for capability in Capability.objects.filter(code__in=AGENT_KEY_CAPABILITIES):
                ApiKeyCapability.objects.get_or_create(api_key=agent_api_key, capability=capability)
            result['agent_api_key_id'] = str(agent_api_key.id)
            result['agent_api_key_fingerprint'] = agent_api_key.key_fingerprint
            result['agent_capabilities'] = list(AGENT_KEY_CAPABILITIES)

        return result
