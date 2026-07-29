from __future__ import annotations

import uuid

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.models import ModelPolicy, ProviderSecret
from engram.model_policy.services import (
    CreateModelPolicy,
    ModelPolicyInput,
    UpdateModelPolicy,
    UpdateModelPolicyInput,
)


@pytest.mark.django_db
def test_probe_hand_tuned_metadata_survives_an_unrelated_update() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    secret = ProviderSecret.objects.create(
        organization=organization,
        name=f'secret-{uuid.uuid4()}',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    policy = CreateModelPolicy().execute(
        ModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            name=f'policy-{uuid.uuid4()}',
            scope='organization',
            task_type='curation',
            provider='openai',
            model='gpt-5-mini',
            secret_id=secret.id,
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )

    policy.metadata['request_shape']['reasoning_effort']['curation_decision_v1'] = 'medium'
    policy.metadata['service_tier'] = {'tier': 'flex', 'attempt_budget': 2}
    policy.save(update_fields=['metadata'])
    print(f'PROBE before_update={policy.metadata}')

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            name='renamed-policy',
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )
    updated.refresh_from_db()
    print(f'PROBE after_rename={updated.metadata}')


@pytest.mark.django_db
def test_probe_pre_existing_gpt5_policy_is_not_backfilled() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    secret = ProviderSecret.objects.create(
        organization=organization,
        name=f'secret-{uuid.uuid4()}',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    legacy = CreateModelPolicy().execute(
        ModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            name=f'legacy-{uuid.uuid4()}',
            scope='organization',
            task_type='curation',
            provider='openai',
            model='gpt-4o-mini',
            secret_id=secret.id,
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )
    legacy.model = 'gpt-5.4'
    legacy.metadata = {}
    legacy.save(update_fields=['model', 'metadata'])
    from engram.model_policy.services import resolve_completion_tokens_param, resolve_temperature

    print(
        f'PROBE legacy param={resolve_completion_tokens_param(legacy, "candidates")} '
        f'temperature={resolve_temperature(legacy, "candidates")}'
    )
