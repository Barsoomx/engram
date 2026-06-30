from __future__ import annotations

from rest_framework import serializers


class ModelSetupStatusQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False)
    team_id = serializers.UUIDField(required=False)


class ApplyPresetSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    scope = serializers.ChoiceField(choices=['organization', 'project', 'team'])
    preset_key = serializers.CharField()
    provider_keys = serializers.DictField(child=serializers.CharField())
    request_id = serializers.CharField()
