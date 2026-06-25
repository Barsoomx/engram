from __future__ import annotations

from rest_framework import serializers

MEMORY_FEEDBACK_REASON_MAX_LENGTH = 2000
MEMORY_FEEDBACK_METADATA_MAX_LENGTH = 255


class MemoryFeedbackSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    action = serializers.ChoiceField(choices=('stale', 'refuted'))
    reason = serializers.CharField(max_length=MEMORY_FEEDBACK_REASON_MAX_LENGTH, allow_blank=False)
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )
