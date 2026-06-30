from __future__ import annotations

from rest_framework import serializers

INSPECTION_LIST_LIMIT_MAX = 200


class InspectionQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=INSPECTION_LIST_LIMIT_MAX, default=50)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    status = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    kind = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    event_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    correlation_id = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    until = serializers.DateTimeField(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs.get('project_id') is None:
            raise serializers.ValidationError(
                {
                    'project_id': {
                        'code': ['inspection_project_required'],
                        'detail': ['project_id is required.'],
                    },
                },
            )

        return attrs
