from __future__ import annotations

from rest_framework import serializers

from engram.model_policy.base_url_validation import BaseUrlValidationError, validate_base_url


class BaseUrlValidationMixin:
    def validate_base_url(self, value: str) -> str:
        if value:
            try:
                validate_base_url(value)
            except BaseUrlValidationError as error:
                raise serializers.ValidationError(str(error)) from error

        return value


class ProviderSecretCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255)
    provider = serializers.ChoiceField(choices=('anthropic', 'openai', 'deepseek'))
    scope = serializers.ChoiceField(choices=('organization', 'team'))
    raw_secret = serializers.CharField(max_length=4096, write_only=True)
    request_id = serializers.CharField(max_length=255)


class ProviderSecretRotateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    raw_secret = serializers.CharField(max_length=4096, write_only=True)
    request_id = serializers.CharField(max_length=255)


class ProviderSecretDisableSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    request_id = serializers.CharField(max_length=255)


class ProviderSecretQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class ModelPolicyCreateSerializer(BaseUrlValidationMixin, serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    scope_team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255)
    scope = serializers.ChoiceField(choices=('organization', 'team', 'project'))
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
    )
    provider = serializers.ChoiceField(choices=('anthropic', 'openai', 'deepseek'))
    model = serializers.CharField(max_length=120)
    secret_id = serializers.UUIDField()
    base_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    context_window_tokens = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    fallback_enabled = serializers.BooleanField(required=False, default=False)
    json_mode = serializers.BooleanField(required=False, allow_null=True)
    request_id = serializers.CharField(max_length=255)


class ModelPolicyResolveSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
    )


class ModelPolicyQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
        required=False,
    )
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)


class ModelPolicyUpdateSerializer(BaseUrlValidationMixin, serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False)
    provider = serializers.ChoiceField(choices=('anthropic', 'openai', 'deepseek'), required=False)
    model = serializers.CharField(max_length=120, required=False)
    secret_id = serializers.UUIDField(required=False)
    active = serializers.BooleanField(required=False)
    fallback_enabled = serializers.BooleanField(required=False)
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
        required=False,
    )
    base_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    context_window_tokens = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    request_id = serializers.CharField(max_length=255)


class ModelPolicyDisableSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    request_id = serializers.CharField(max_length=255)


class ProviderSecretEnableSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    request_id = serializers.CharField(max_length=255)


class ProviderSecretUpdateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255)
    request_id = serializers.CharField(max_length=255)
