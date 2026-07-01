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


MEMORY_VERSION_BODY_MAX_LENGTH = 16000


class MemoryVersionSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    body = serializers.CharField(max_length=MEMORY_VERSION_BODY_MAX_LENGTH, allow_blank=False)
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_REASON_MAX_LENGTH,
    )
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )


MEMORY_LINK_TARGET_MAX_LENGTH = 1024
MEMORY_LINK_LABEL_MAX_LENGTH = 255


class MemoryLinkSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    link_type = serializers.ChoiceField(choices=('file', 'symbol', 'commit', 'issue'))
    target = serializers.CharField(max_length=MEMORY_LINK_TARGET_MAX_LENGTH, allow_blank=False)
    label = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_LINK_LABEL_MAX_LENGTH,
    )
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )


class MemoryLinkQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)


class MemoryLinkDeleteSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
    link_id = serializers.UUIDField()
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )


class MemoryVersionQuerySerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)


class MemoryDiffQuerySerializer(serializers.Serializer):
    from_version = serializers.IntegerField(min_value=1)
    to_version = serializers.IntegerField(min_value=1)
    project_id = serializers.UUIDField()
    team_id = serializers.UUIDField(required=False, allow_null=True)
