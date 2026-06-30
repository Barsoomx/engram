from __future__ import annotations

from django.urls import path

from engram.model_policy.views import (
    ModelPolicyDetailView,
    ModelPolicyDisableView,
    ModelPolicyListView,
    ModelPolicyResolveView,
    ProviderSecretDetailView,
    ProviderSecretDisableView,
    ProviderSecretEnableView,
    ProviderSecretListView,
    ProviderSecretRotateView,
)

urlpatterns = [
    path('secrets', ProviderSecretListView.as_view(), name='model-policy-secrets'),
    path('secrets/<uuid:secret_id>', ProviderSecretDetailView.as_view(), name='model-policy-secret-detail'),
    path('secrets/<uuid:secret_id>/rotate', ProviderSecretRotateView.as_view(), name='model-policy-secret-rotate'),
    path('secrets/<uuid:secret_id>/disable', ProviderSecretDisableView.as_view(), name='model-policy-secret-disable'),
    path('secrets/<uuid:secret_id>/enable', ProviderSecretEnableView.as_view(), name='model-policy-secret-enable'),
    path('policies', ModelPolicyListView.as_view(), name='model-policy-policies'),
    path('policies/<uuid:policy_id>', ModelPolicyDetailView.as_view(), name='model-policy-detail'),
    path('policies/<uuid:policy_id>/disable', ModelPolicyDisableView.as_view(), name='model-policy-disable'),
    path('resolve', ModelPolicyResolveView.as_view(), name='model-policy-resolve'),
]
