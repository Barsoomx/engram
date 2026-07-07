from __future__ import annotations

from rest_framework import serializers


class MetricsScopeQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    team_id = serializers.UUIDField(required=False, allow_null=True, default=None)
