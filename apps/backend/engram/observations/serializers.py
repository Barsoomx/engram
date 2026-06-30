from __future__ import annotations

from rest_framework import serializers

OBSERVATION_LIST_LIMIT_MAX = 100


class ObservationListQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=OBSERVATION_LIST_LIMIT_MAX, default=20)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)
    observation_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    session_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    until = serializers.DateTimeField(required=False, allow_null=True, default=None)


class ObservationDetailQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
