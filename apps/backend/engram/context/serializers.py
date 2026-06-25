from __future__ import annotations

from rest_framework import serializers


class ContextRequestSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    agent_runtime = serializers.CharField(max_length=40)
    session_id = serializers.CharField(max_length=255)
    request_id = serializers.CharField(max_length=255)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    agent_version = serializers.CharField(required=False, allow_blank=True, default='')
    agent_external_id = serializers.CharField(required=False, allow_blank=True, default='')
    correlation_id = serializers.CharField(required=False, allow_blank=True, default='')
    trace_id = serializers.CharField(required=False, allow_blank=True, default='')
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    repository_root = serializers.CharField(required=False, allow_blank=True, default='')
    branch = serializers.CharField(required=False, allow_blank=True, default='')
    cwd = serializers.CharField(required=False, allow_blank=True, default='')
    query = serializers.CharField(required=False, allow_blank=True, default='')
    file_paths = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    symbols = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=10, default=5)
    token_budget = serializers.IntegerField(required=False, min_value=1, allow_null=True, default=None)
