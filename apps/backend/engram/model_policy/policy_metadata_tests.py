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
    resolve_completion_tokens_param,
    resolve_temperature,
)

# A policy created by hand in the console must work on the model it names. The preset path
# derives the OpenAI request shape; without the same derivation here, an operator who types
# a gpt-5 model into /model-policies gets a policy that 400s on every call.


@pytest.fixture
def f_scope() -> tuple:
    return create_project_scope()


def _secret(organization: object, provider: str = 'openai') -> ProviderSecret:
    return ProviderSecret.objects.create(
        organization=organization,
        name=f'secret-{uuid.uuid4()}',
        provider=provider,
        scope='organization',
        current_version=1,
    )


def _create(organization: object, project: object, *, provider: str, model: str) -> ModelPolicy:
    secret = _secret(organization, provider)

    return CreateModelPolicy().execute(
        ModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            name=f'policy-{uuid.uuid4()}',
            scope='organization',
            task_type='curation',
            provider=provider,
            model=model,
            secret_id=secret.id,
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )


@pytest.mark.django_db
def test_manual_gpt5_policy_is_created_with_a_working_request_shape(f_scope: tuple) -> None:
    organization, _team, project, _owner, _api_key = f_scope

    policy = _create(organization, project, provider='openai', model='gpt-5-nano')

    assert resolve_completion_tokens_param(policy, 'candidates') == 'max_completion_tokens'
    assert resolve_temperature(policy, 'candidates') is None


@pytest.mark.django_db
def test_manual_gpt5_policy_does_not_silently_opt_into_flex(f_scope: tuple) -> None:
    organization, _team, project, _owner, _api_key = f_scope

    policy = _create(organization, project, provider='openai', model='gpt-5-nano')

    assert 'service_tier' not in (policy.metadata or {})


@pytest.mark.django_db
def test_non_reasoning_policies_keep_the_historic_request_shape(f_scope: tuple) -> None:
    organization, _team, project, _owner, _api_key = f_scope

    for provider, model in (('openai', 'gpt-4o-mini'), ('deepseek', 'deepseek-v4-pro')):
        policy = _create(organization, project, provider=provider, model=model)

        assert resolve_completion_tokens_param(policy, 'candidates') == 'max_tokens'
        assert resolve_temperature(policy, 'candidates') == 0.2


@pytest.mark.django_db
def test_switching_an_existing_policy_to_gpt5_updates_its_request_shape(f_scope: tuple) -> None:
    organization, _team, project, _owner, _api_key = f_scope
    policy = _create(organization, project, provider='openai', model='gpt-4o-mini')

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            model='gpt-5-mini',
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )

    assert resolve_completion_tokens_param(updated, 'candidates') == 'max_completion_tokens'
    assert resolve_temperature(updated, 'candidates') is None


@pytest.mark.django_db
def test_switching_away_from_gpt5_restores_the_historic_request_shape(f_scope: tuple) -> None:
    organization, _team, project, _owner, _api_key = f_scope
    policy = _create(organization, project, provider='openai', model='gpt-5-mini')

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            model='gpt-4o-mini',
            request_id=str(uuid.uuid4()),
            actor_id='tests',
        )
    )

    assert resolve_completion_tokens_param(updated, 'candidates') == 'max_tokens'
    assert resolve_temperature(updated, 'candidates') == 0.2
