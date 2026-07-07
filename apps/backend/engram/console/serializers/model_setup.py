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
    replace_existing = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs['scope'] == 'team' and not attrs.get('team_id'):
            raise serializers.ValidationError({'team_id': 'team_id is required when scope is team'})

        return attrs
