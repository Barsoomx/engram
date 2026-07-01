from __future__ import annotations

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.filters import ModelPolicyFilterSet
from engram.model_policy.models import ModelPolicy, ProviderSecret


def _create_policy(organization: object, *, task_type: str, name: str) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        name=f'secret-{name}',
        provider='anthropic',
        scope='organization',
    )

    return ModelPolicy.objects.create(
        organization=organization,
        secret=secret,
        name=name,
        scope='organization',
        task_type=task_type,
        provider='anthropic',
        model='claude-test',
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
