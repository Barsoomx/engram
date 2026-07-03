from __future__ import annotations

from rest_framework import serializers

OBSERVATION_LIST_LIMIT_MAX = 100
OBSERVATION_REPOSITORY_URL_MAX_LENGTH = 1024


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
            max_length=OBSERVATION_REPOSITORY_URL_MAX_LENGTH,
            code='observation_repository_url_too_long',
            label='repository_url',
        )


class ObservationListQuerySerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=OBSERVATION_LIST_LIMIT_MAX, default=20)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)
    observation_type = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    session_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    since = serializers.DateTimeField(required=False, allow_null=True, default=None)
    until = serializers.DateTimeField(required=False, allow_null=True, default=None)


class ObservationDetailQuerySerializer(_RepositoryUrlMixin, serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    team_id = serializers.UUIDField(required=False, allow_null=True)
