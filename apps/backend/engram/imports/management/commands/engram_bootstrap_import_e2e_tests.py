from __future__ import annotations

import json
import uuid

import pytest

from engram.access.models import ApiKey
from engram.access.services import ResolveApiKeyScope, api_key_fingerprint, hash_api_key
from engram.core.models import Organization, Project, ProjectTeam, Team
from engram.imports.management.commands.engram_bootstrap_import_e2e import bootstrap_import_e2e

RAW_KEY = 'egk_import_e2e_0123456789abcdefghijklmnopqrstuvwxyz'


@pytest.mark.django_db
def test_bootstrap_creates_org_project_team() -> None:
    result = bootstrap_import_e2e(RAW_KEY)

    organization = Organization.objects.get(id=result['organization_id'])
    project = Project.objects.get(id=result['project_id'])
    team = Team.objects.get(id=result['team_id'])

    assert project.organization_id == organization.id
    assert team.organization_id == organization.id
    assert ProjectTeam.objects.filter(project=project, team=team).exists()
    assert result['repository_url'] == project.repository_url


@pytest.mark.django_db
def test_bootstrap_mints_memories_admin_key_for_project() -> None:
    result = bootstrap_import_e2e(RAW_KEY)

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='memories:admin',
        requested_project_id=uuid.UUID(result['project_id']),
    )

    assert 'memories:admin' in scope.capabilities
    assert uuid.UUID(result['project_id']) in scope.project_ids
    assert result['api_key_fingerprint'] == api_key_fingerprint(RAW_KEY)


@pytest.mark.django_db
def test_bootstrap_is_idempotent() -> None:
    first = bootstrap_import_e2e(RAW_KEY)
    second = bootstrap_import_e2e(RAW_KEY)

    assert first['project_id'] == second['project_id']
    assert first['organization_id'] == second['organization_id']
    assert ApiKey.objects.filter(key_hash=hash_api_key(RAW_KEY)).count() == 1


@pytest.mark.django_db
def test_command_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    from django.core.management import call_command

    call_command('engram_bootstrap_import_e2e', '--api-key', RAW_KEY, '--json')

    payload = json.loads(capsys.readouterr().out.strip())

    assert payload['project_id']
    assert payload['organization_slug']
    assert 'memories:admin' in payload['capabilities']
