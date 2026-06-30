from __future__ import annotations

from rest_framework import serializers


class SearchDebugRequestSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    query = serializers.CharField(allow_blank=True)
    team_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    file_paths = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    symbols = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
