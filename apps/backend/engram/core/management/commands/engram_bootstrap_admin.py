from __future__ import annotations

import json
import os
import secrets
from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import Organization, Project, ProjectTeam, Team

DEFAULT_ORG_SLUG = 'default'
DEFAULT_ORG_NAME = 'Default organization'
DEFAULT_TEAM_SLUG = 'default'
DEFAULT_TEAM_NAME = 'Default team'
DEFAULT_PROJECT_SLUG = 'default'
DEFAULT_PROJECT_NAME = 'Default project'
DEFAULT_ADMIN_USERNAME = 'admin'
DEFAULT_DISPLAY_NAME = 'Engram admin'
ADMIN_API_KEY_NAME = 'Engram bootstrap admin key'
OWNER_ROLE_CODE = 'organization_owner'

ADMIN_API_KEY_CAPABILITIES = (
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

GENERATED_PASSWORD_ALPHABET = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*'
GENERATED_PASSWORD_LENGTH = 32

LOGIN_URL_PATH = '/v1/auth/login'


class Command(BaseCommand):
    help = 'Create the first Django admin user, default org/team/project, owner identity, and admin API key.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--api-key', default='')
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, *args: Any, **options: Any) -> None:
        raw_api_key = str(options['api_key'] or '')
        if not raw_api_key:
            raw_api_key = _env_or_default('ENGRAM_BOOTSTRAP_ADMIN_API_KEY', '')

        result = bootstrap_admin(raw_api_key=raw_api_key)

        if options['as_json']:
            self.stdout.write(json.dumps(result, sort_keys=True))

            return

        _write_report(self.stdout, result)


def bootstrap_admin(*, raw_api_key: str) -> dict[str, object]:
    org_slug = _env_or_default('ENGRAM_BOOTSTRAP_ORG_SLUG', DEFAULT_ORG_SLUG)
    team_slug = _env_or_default('ENGRAM_BOOTSTRAP_TEAM_SLUG', DEFAULT_TEAM_SLUG)
    project_slug = _env_or_default('ENGRAM_BOOTSTRAP_PROJECT_SLUG', DEFAULT_PROJECT_SLUG)
    username = _env_or_default('ENGRAM_BOOTSTRAP_ADMIN_USERNAME', DEFAULT_ADMIN_USERNAME)
    password_env = _env_or_default('ENGRAM_BOOTSTRAP_ADMIN_PASSWORD', '')

    with transaction.atomic():
        organization, _created = Organization.objects.update_or_create(
            slug=org_slug,
            defaults={'name': DEFAULT_ORG_NAME},
        )
        team, _created = Team.objects.update_or_create(
            organization=organization,
            slug=team_slug,
            defaults={'name': DEFAULT_TEAM_NAME},
        )
        project, _created = Project.objects.update_or_create(
            organization=organization,
            slug=project_slug,
            defaults={
                'name': DEFAULT_PROJECT_NAME,
                'repository_url': '',
                'repository_root': '',
                'default_branch': 'master',
            },
        )
        ProjectTeam.objects.get_or_create(
            organization=organization,
            team=team,
            project=project,
        )

        generated_password = ''
        user = User.objects.filter(username=username).first()
        if user is None:
            password = password_env
            if not password:
                generated_password = _generate_password()

                password = generated_password

            user = User.objects.create_user(
                username=username,
                password=password,
                email='',
                is_staff=True,
                is_superuser=True,
            )
            user.is_active = True

            user.save(update_fields=['is_active'])

        identity, _created = Identity.objects.update_or_create(
            organization=organization,
            identity_type=IdentityType.USER,
            external_id=external_id_for_user(user),
            defaults={
                'display_name': DEFAULT_DISPLAY_NAME,
                'email': getattr(user, 'email', '') or '',
                'active': True,
            },
        )

        owner_role = Role.objects.get(code=OWNER_ROLE_CODE)
        OrganizationMembership.objects.update_or_create(
            organization=organization,
            identity=identity,
            defaults={
                'role': owner_role,
                'active': True,
            },
        )

        effective_raw_key = raw_api_key
        key_generated = False
        if not effective_raw_key:
            effective_raw_key = _generate_api_key(organization.slug)
            key_generated = True

        api_key, _created = ApiKey.objects.update_or_create(
            key_hash=hash_api_key(effective_raw_key),
            defaults={
                'organization': organization,
                'owner_identity': identity,
                'name': ADMIN_API_KEY_NAME,
                'key_prefix': api_key_prefix(effective_raw_key),
                'key_fingerprint': api_key_fingerprint(effective_raw_key),
                'team': team,
                'project': project,
                'active': True,
            },
        )
        for capability in Capability.objects.filter(code__in=ADMIN_API_KEY_CAPABILITIES):
            ApiKeyCapability.objects.get_or_create(api_key=api_key, capability=capability)

        return {
            'organization_slug': organization.slug,
            'organization_id': str(organization.id),
            'team_slug': team.slug,
            'team_id': str(team.id),
            'project_slug': project.slug,
            'project_id': str(project.id),
            'username': user.get_username(),
            'generated_password': generated_password,
            'login_url': LOGIN_URL_PATH,
            'api_key': effective_raw_key,
            'api_key_fingerprint': api_key.key_fingerprint,
            'api_key_generated': key_generated,
            'identity_id': str(identity.id),
        }


def _env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)

    if value is None or value == '':
        return default

    return value


def _generate_password() -> str:
    return ''.join(secrets.choice(GENERATED_PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))


def _generate_api_key(org_slug: str) -> str:
    token = secrets.token_urlsafe(32).replace('-', '').replace('_', '')[:40]

    return f'engram-{org_slug}-{token}'


def _write_report(stdout: Any, result: dict[str, object]) -> None:
    stdout.write('Engram bootstrap complete')
    stdout.write('-------------------------')
    stdout.write(f'Login URL: {result["login_url"]}')
    stdout.write(f'Username: {result["username"]}')
    generated_password = result.get('generated_password') or ''
    if generated_password:
        stdout.write(f'Generated admin password: {generated_password}')
    stdout.write(f'Organization: {result["organization_slug"]}')
    stdout.write(f'Team: {result["team_slug"]}')
    stdout.write(f'Project: {result["project_slug"]}')
    stdout.write('API key (shown once):')
    stdout.write(str(result['api_key']))
    stdout.write(f'API key fingerprint: {result["api_key_fingerprint"]}')
