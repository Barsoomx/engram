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
