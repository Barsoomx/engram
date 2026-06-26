from __future__ import annotations

from rest_framework import serializers


class InspectionQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False)
    team_id = serializers.UUIDField(required=False, allow_null=True)

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
