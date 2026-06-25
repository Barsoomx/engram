from __future__ import annotations

from rest_framework import serializers

from engram.core.models import Runtime


class HookDryRunSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    agent_runtime = serializers.ChoiceField(choices=Runtime.values)
    agent_version = serializers.CharField(required=False, allow_blank=True, max_length=80)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)


class HookObservationSerializer(serializers.Serializer):
    type = serializers.CharField(max_length=80)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    body = serializers.CharField(required=False, allow_blank=True)
    files_read = serializers.ListField(child=serializers.CharField(), required=False)
    files_modified = serializers.ListField(child=serializers.CharField(), required=False)


class HookEventSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    agent_runtime = serializers.ChoiceField(choices=Runtime.values)
    agent_version = serializers.CharField(required=False, allow_blank=True, max_length=80)
    agent_external_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    session_id = serializers.CharField(max_length=255)
    event_id = serializers.CharField(max_length=255)
    idempotency_key = serializers.CharField(max_length=255)
    event_type = serializers.CharField(max_length=120)
    payload_schema_version = serializers.CharField(max_length=40)
    sequence_number = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    occurred_at = serializers.DateTimeField(required=False, allow_null=True)
    content_hash = serializers.CharField(max_length=128)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    trace_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    repository_url = serializers.CharField(required=False, allow_blank=True)
    repository_root = serializers.CharField(required=False, allow_blank=True)
    branch = serializers.CharField(required=False, allow_blank=True, max_length=255)
    cwd = serializers.CharField(required=False, allow_blank=True)
    payload = serializers.JSONField()
    observation = HookObservationSerializer(required=False)

    def validate_payload(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise serializers.ValidationError('Must be a JSON object.')

        return value
