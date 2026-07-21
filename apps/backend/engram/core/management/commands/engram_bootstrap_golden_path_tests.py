from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from engram.access.models import ApiKey
from engram.access.services import hash_api_key
from engram.core.management.commands.engram_bootstrap_golden_path import (
    AGENT_KEY_CAPABILITIES,
    bootstrap_golden_path,
)
from engram.core.models import OrganizationSettings, Project
from engram.model_policy.models import ModelPolicy

RAW_KEY = 'egk_test_golden_path_key_0123456789abcdefghijklmnopqrstuv'
RAW_AGENT_KEY = 'egk_test_golden_path_agent_0123456789abcdefghijklmnopqrst'


@pytest.mark.django_db
def test_bootstrap_without_agent_key_keeps_existing_contract() -> None:
    result = bootstrap_golden_path(RAW_KEY)

    assert 'agent_api_key_id' not in result
    assert result['project_id']


@pytest.mark.django_db
def test_bootstrap_enables_realtime_candidates_for_e2e_org() -> None:
    result = bootstrap_golden_path(RAW_KEY)

    settings = OrganizationSettings.objects.get(organization_id=result['organization_id'])
    assert settings.realtime_candidates_enabled is True


@pytest.mark.django_db
def test_bootstrap_result_exposes_project_repository_url() -> None:
    result = bootstrap_golden_path(RAW_KEY)

    project = Project.objects.get(id=result['project_id'])
    assert result['repository_url'] == project.repository_url
    assert result['repository_url']


@pytest.mark.django_db
def test_bootstrap_agent_key_is_org_wide_with_agent_capabilities() -> None:
    result = bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)

    api_key = ApiKey.objects.get(key_hash=hash_api_key(RAW_AGENT_KEY))
    assert str(api_key.id) == result['agent_api_key_id']
    assert api_key.project_id is None
    assert api_key.team_id is None
    assert api_key.active is True
    capabilities = set(api_key.capability_links.values_list('capability__code', flat=True))
    assert capabilities == set(AGENT_KEY_CAPABILITIES)


@pytest.mark.django_db
def test_bootstrap_agent_key_grants_mcp_read_and_propose_capabilities() -> None:
    bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)

    api_key = ApiKey.objects.get(key_hash=hash_api_key(RAW_AGENT_KEY))
    capabilities = set(api_key.capability_links.values_list('capability__code', flat=True))
    assert {'audit:read', 'memories:propose', 'memories:review'}.issubset(capabilities)


@pytest.mark.django_db
def test_bootstrap_creates_organization_scope_policies_for_auto_projects() -> None:
    result = bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)

    organization_id = result['organization_id']
    for task_type in ('generation', 'embedding', 'digest', 'curation'):
        assert ModelPolicy.objects.filter(
            organization_id=organization_id,
            scope='organization',
            project__isnull=True,
            team__isnull=True,
            task_type=task_type,
            active=True,
        ).exists()


@pytest.mark.django_db
def test_bootstrap_agent_key_is_idempotent() -> None:
    bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)
    bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)

    assert ApiKey.objects.filter(key_hash=hash_api_key(RAW_AGENT_KEY)).count() == 1


@pytest.mark.django_db
def test_bootstrap_provider_base_url_lands_in_all_policies() -> None:
    bootstrap_golden_path(
        RAW_KEY,
        agent_key=RAW_AGENT_KEY,
        provider_base_url='http://host.docker.internal:9999/v1',
    )

    policies = ModelPolicy.objects.all()
    assert policies.count() == 6
    for policy in policies:
        assert policy.metadata.get('base_url') == 'http://host.docker.internal:9999/v1'


@pytest.mark.django_db
def test_bootstrap_agent_key_passes_dry_run_without_project() -> None:
    bootstrap_golden_path(RAW_KEY, agent_key=RAW_AGENT_KEY)

    client = APIClient()
    response = client.post(
        '/v1/hooks/dry-run',
        {
            'agent_runtime': 'claude_code',
            'agent_version': 'e2e',
            'request_id': 'agent-dry-run-1',
        },
        format='json',
        HTTP_AUTHORIZATION=f'Bearer {RAW_AGENT_KEY}',
    )

    assert response.status_code == 200, response.data
    assert 'projects:agent' in response.data['scope']['capabilities']
