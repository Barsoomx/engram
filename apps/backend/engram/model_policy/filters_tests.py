from __future__ import annotations

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.filters import ModelPolicyFilterSet, ProviderSecretFilterSet
from engram.model_policy.models import ModelPolicy, ProviderSecret


def _create_secret(
    organization: object,
    *,
    name: str,
    provider: str = 'anthropic',
    scope: str = 'organization',
    active: bool = True,
    team: object | None = None,
) -> ProviderSecret:
    return ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=name,
        provider=provider,
        scope=scope,
        active=active,
    )


def _create_policy(
    organization: object,
    *,
    task_type: str,
    name: str,
    provider: str = 'anthropic',
    scope: str = 'organization',
    active: bool = True,
) -> ModelPolicy:
    secret = _create_secret(organization, name=f'secret-{name}', provider=provider)

    return ModelPolicy.objects.create(
        organization=organization,
        secret=secret,
        name=name,
        scope=scope,
        task_type=task_type,
        provider=provider,
        model='claude-test',
        active=active,
    )


@pytest.mark.django_db
def test_filterset_filters_by_task_type() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    generation_policy = _create_policy(organization, task_type='generation', name='gen-policy')

    _create_policy(organization, task_type='digest', name='digest-policy')

    queryset = ModelPolicy.objects.filter(organization=organization)

    filtered = ModelPolicyFilterSet(data={'task_type': 'generation'}, queryset=queryset).qs

    assert {p.id for p in filtered} == {generation_policy.id}


@pytest.mark.django_db
def test_filterset_without_task_type_returns_all() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    first = _create_policy(organization, task_type='generation', name='gen-policy-2')

    second = _create_policy(organization, task_type='digest', name='digest-policy-2')

    queryset = ModelPolicy.objects.filter(organization=organization)

    filtered = ModelPolicyFilterSet(data={}, queryset=queryset).qs

    assert {p.id for p in filtered} == {first.id, second.id}


@pytest.mark.django_db
def test_model_policy_filterset_filters_by_provider() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    openai_policy = _create_policy(organization, task_type='generation', name='openai-policy', provider='openai')

    _create_policy(organization, task_type='generation', name='anthropic-policy', provider='anthropic')

    queryset = ModelPolicy.objects.filter(organization=organization)

    filtered = ModelPolicyFilterSet(data={'provider': 'openai'}, queryset=queryset).qs

    assert {p.id for p in filtered} == {openai_policy.id}


@pytest.mark.django_db
def test_model_policy_filterset_filters_by_active() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    active_policy = _create_policy(organization, task_type='generation', name='active-policy', active=True)

    _create_policy(organization, task_type='generation', name='inactive-policy', active=False)

    queryset = ModelPolicy.objects.filter(organization=organization)

    filtered = ModelPolicyFilterSet(data={'active': 'true'}, queryset=queryset).qs

    assert {p.id for p in filtered} == {active_policy.id}


@pytest.mark.django_db
def test_model_policy_filterset_filters_by_scope() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()

    org_policy = _create_policy(organization, task_type='generation', name='org-scope-policy', scope='organization')

    secret = _create_secret(organization, name='secret-team-scope-policy', scope='team', team=team)
    ModelPolicy.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        name='team-scope-policy',
        scope='team',
        task_type='generation',
        provider='anthropic',
        model='claude-test',
    )

    queryset = ModelPolicy.objects.filter(organization=organization)

    filtered = ModelPolicyFilterSet(data={'scope': 'organization'}, queryset=queryset).qs

    assert {p.id for p in filtered} == {org_policy.id}


@pytest.mark.django_db
def test_provider_secret_filterset_filters_by_provider() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    openai_secret = _create_secret(organization, name='openai-secret', provider='openai')

    _create_secret(organization, name='anthropic-secret', provider='anthropic')

    queryset = ProviderSecret.objects.filter(organization=organization)

    filtered = ProviderSecretFilterSet(data={'provider': 'openai'}, queryset=queryset).qs

    assert {s.id for s in filtered} == {openai_secret.id}


@pytest.mark.django_db
def test_provider_secret_filterset_filters_by_scope() -> None:
    organization, team, _project, _owner, _api_key = create_project_scope()

    org_secret = _create_secret(organization, name='org-scope-secret', scope='organization')

    _create_secret(organization, name='team-scope-secret', scope='team', team=team)

    queryset = ProviderSecret.objects.filter(organization=organization)

    filtered = ProviderSecretFilterSet(data={'scope': 'organization'}, queryset=queryset).qs

    assert {s.id for s in filtered} == {org_secret.id}


@pytest.mark.django_db
def test_provider_secret_filterset_filters_by_active() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    active_secret = _create_secret(organization, name='active-secret', active=True)

    inactive_secret = _create_secret(organization, name='inactive-secret', active=False)

    queryset = ProviderSecret.objects.filter(organization=organization)

    filtered_inactive = ProviderSecretFilterSet(data={'active': 'false'}, queryset=queryset).qs

    assert {s.id for s in filtered_inactive} == {inactive_secret.id}

    filtered_active = ProviderSecretFilterSet(data={'active': 'true'}, queryset=queryset).qs

    assert {s.id for s in filtered_active} == {active_secret.id}


@pytest.mark.django_db
def test_provider_secret_filterset_without_filters_returns_all() -> None:
    organization, _team, _project, _owner, _api_key = create_project_scope()

    first = _create_secret(organization, name='first-secret')

    second = _create_secret(organization, name='second-secret')

    queryset = ProviderSecret.objects.filter(organization=organization)

    filtered = ProviderSecretFilterSet(data={}, queryset=queryset).qs

    assert {s.id for s in filtered} == {first.id, second.id}
