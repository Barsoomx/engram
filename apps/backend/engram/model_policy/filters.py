from __future__ import annotations

import django_filters

from engram.model_policy.models import ModelPolicy, ProviderSecret


class ModelPolicyFilterSet(django_filters.FilterSet):
    class Meta:
        model = ModelPolicy
        fields = ['task_type', 'provider', 'scope', 'active']


class ProviderSecretFilterSet(django_filters.FilterSet):
    class Meta:
        model = ProviderSecret
        fields = ['provider', 'scope', 'active']
