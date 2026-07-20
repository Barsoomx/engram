from __future__ import annotations

from rest_framework import serializers

from engram.core.models import LinkType

MEMORY_FEEDBACK_REASON_MAX_LENGTH = 2000
MEMORY_FEEDBACK_METADATA_MAX_LENGTH = 255
MEMORY_REPOSITORY_URL_MAX_LENGTH = 1024
MEMORY_PROPOSE_BODY_MAX_LENGTH = 16000


def _limit_error(code: str, detail: str) -> dict[str, list[str]]:
    return {'code': [code], 'detail': [detail]}


def _validate_text_limit(value: str, *, max_length: int, code: str, label: str) -> str:
    if len(value) > max_length:
        raise serializers.ValidationError(_limit_error(code, f'{label} must be at most {max_length} characters.'))

    return value


class _RepositoryUrlMixin:
    def validate_repository_url(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=MEMORY_REPOSITORY_URL_MAX_LENGTH,
            code='memory_repository_url_too_long',
            label='repository_url',
        )


class MemoryFeedbackSerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
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


class MemoryVersionSerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
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


class MemoryLinkSerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)
    link_type = serializers.ChoiceField(choices=LinkType.values)
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


class MemoryLinkQuerySerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)


class MemoryLinkDeleteSerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)
    link_id = serializers.UUIDField()
    request_id = serializers.CharField(max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )


class MemoryVersionQuerySerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)


class MemoryDiffQuerySerializer(_RepositoryUrlMixin, serializers.Serializer):
    from_version = serializers.IntegerField(min_value=1)
    to_version = serializers.IntegerField(min_value=1)
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)


class MemoryProposeSerializer(_RepositoryUrlMixin, serializers.Serializer):
    title = serializers.CharField(max_length=255, allow_blank=False, trim_whitespace=True)
    body = serializers.CharField(max_length=MEMORY_PROPOSE_BODY_MAX_LENGTH, allow_blank=False, trim_whitespace=True)
    kind = serializers.CharField(required=False, allow_blank=True, default='', max_length=40)
    request_id = serializers.CharField(allow_blank=False, max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH)
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_REPOSITORY_URL_MAX_LENGTH,
    )
    team_id = serializers.UUIDField(required=False, allow_null=True)
    correlation_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        max_length=MEMORY_FEEDBACK_METADATA_MAX_LENGTH,
    )
