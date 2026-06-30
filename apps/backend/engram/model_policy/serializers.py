from __future__ import annotations

from rest_framework import serializers


class ProviderSecretCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255)
    provider = serializers.ChoiceField(choices=('anthropic', 'openai'))
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


class ModelPolicyCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    scope_team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255)
    scope = serializers.ChoiceField(choices=('organization', 'team', 'project'))
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
    )
    provider = serializers.ChoiceField(choices=('anthropic', 'openai'))
    model = serializers.CharField(max_length=120)
    secret_id = serializers.UUIDField()
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


class ModelPolicyUpdateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False)
    provider = serializers.ChoiceField(choices=('anthropic', 'openai'), required=False)
    model = serializers.CharField(max_length=120, required=False)
    secret_id = serializers.UUIDField(required=False)
    active = serializers.BooleanField(required=False)
    fallback_enabled = serializers.BooleanField(required=False)
    task_type = serializers.ChoiceField(
        choices=('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant'),
        required=False,
    )
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
