from __future__ import annotations

import json
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
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import Organization, Project, ProjectTeam, Team

IMPORT_E2E_CAPABILITIES = ('memories:admin', 'memories:read')


class Command(BaseCommand):
    help = 'Create the deterministic claude-mem import e2e org, project, team, and memories:admin API key.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--api-key', required=True)
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        result = bootstrap_import_e2e(str(options['api_key']))
        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))

            return

        self.stdout.write(f'organization_id={result["organization_id"]}')
        self.stdout.write(f'organization_slug={result["organization_slug"]}')
        self.stdout.write(f'project_id={result["project_id"]}')
        self.stdout.write(f'team_id={result["team_id"]}')
        self.stdout.write(f'repository_url={result["repository_url"]}')
        self.stdout.write(f'api_key_fingerprint={result["api_key_fingerprint"]}')


def bootstrap_import_e2e(raw_key: str) -> dict[str, object]:
    with transaction.atomic():
        organization, _created = Organization.objects.update_or_create(
            slug='engram-import-e2e',
            defaults={'name': 'Engram Import E2E'},
        )
        team, _created = Team.objects.update_or_create(
            organization=organization,
            slug='import-platform',
            defaults={'name': 'Import Platform'},
        )
        project, _created = Project.objects.update_or_create(
            organization=organization,
            slug='import-backend',
            defaults={
                'name': 'Import Backend',
                'repository_url': 'https://example.test/engram-import.git',
                'repository_root': '/workspace/engram-import',
                'default_branch': 'master',
            },
        )
        ProjectTeam.objects.get_or_create(organization=organization, team=team, project=project)
        role, _created = Role.objects.get_or_create(
            code='import-e2e-admin',
            defaults={'name': 'Import E2E Admin', 'built_in': True},
        )
        for code in IMPORT_E2E_CAPABILITIES:
            RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(code))
        identity, _created = Identity.objects.update_or_create(
            organization=organization,
            identity_type=IdentityType.SERVICE_ACCOUNT,
            external_id='import-e2e-admin',
            defaults={'display_name': 'Import E2E admin', 'active': True},
        )
        OrganizationMembership.objects.update_or_create(
            organization=organization,
            identity=identity,
            defaults={'role': role, 'active': True},
        )
        ProjectGrant.objects.update_or_create(
            organization=organization,
            project=project,
            identity=identity,
            defaults={'role': role, 'active': True},
        )
        api_key, _created = ApiKey.objects.update_or_create(
            key_hash=hash_api_key(raw_key),
            defaults={
                'organization': organization,
                'owner_identity': identity,
                'name': 'Import E2E key',
                'key_prefix': api_key_prefix(raw_key),
                'key_fingerprint': api_key_fingerprint(raw_key),
                'team': team,
                'project': project,
                'active': True,
            },
        )
        for code in IMPORT_E2E_CAPABILITIES:
            ApiKeyCapability.objects.get_or_create(api_key=api_key, capability=_ensure_capability(code))

        return {
            'organization_id': str(organization.id),
            'organization_slug': organization.slug,
            'project_id': str(project.id),
            'team_id': str(team.id),
            'repository_url': project.repository_url,
            'api_key_id': str(api_key.id),
            'api_key_fingerprint': api_key.key_fingerprint,
            'capabilities': list(IMPORT_E2E_CAPABILITIES),
        }


def _ensure_capability(code: str) -> Capability:
    capability, _created = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability
