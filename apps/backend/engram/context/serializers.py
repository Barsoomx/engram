from __future__ import annotations

from rest_framework import serializers

CONTEXT_QUERY_MAX_LENGTH = 8000
CONTEXT_LIST_VALUE_MAX_LENGTH = 1024
CONTEXT_LIST_MAX_ITEMS = 100
CONTEXT_PATH_MAX_LENGTH = 1024
CONTEXT_AGENT_VERSION_MAX_LENGTH = 80
CONTEXT_METADATA_MAX_LENGTH = 255


def _limit_error(code: str, detail: str) -> dict[str, list[str]]:
    return {'code': [code], 'detail': [detail]}


def _validate_text_limit(value: str, *, max_length: int, code: str, label: str) -> str:
    if len(value) > max_length:
        raise serializers.ValidationError(_limit_error(code, f'{label} must be at most {max_length} characters.'))

    return value


def _validate_text_list(
    values: list[str],
    *,
    list_code: str,
    item_code: str,
    label: str,
) -> list[str]:
    if len(values) > CONTEXT_LIST_MAX_ITEMS:
        raise serializers.ValidationError(
            _limit_error(list_code, f'{label} must contain at most {CONTEXT_LIST_MAX_ITEMS} entries.'),
        )
    if any(len(value) > CONTEXT_LIST_VALUE_MAX_LENGTH for value in values):
        raise serializers.ValidationError(
            _limit_error(item_code, f'{label} entries must be at most {CONTEXT_LIST_VALUE_MAX_LENGTH} characters.'),
        )

    return values


class ContextRequestSerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
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

    def validate_agent_version(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_AGENT_VERSION_MAX_LENGTH,
            code='context_agent_version_too_long',
            label='agent_version',
        )

    def validate_agent_external_id(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_METADATA_MAX_LENGTH,
            code='context_agent_external_id_too_long',
            label='agent_external_id',
        )

    def validate_correlation_id(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_METADATA_MAX_LENGTH,
            code='context_correlation_id_too_long',
            label='correlation_id',
        )

    def validate_trace_id(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_METADATA_MAX_LENGTH,
            code='context_trace_id_too_long',
            label='trace_id',
        )

    def validate_branch(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_METADATA_MAX_LENGTH,
            code='context_branch_too_long',
            label='branch',
        )

    def validate_repository_url(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_PATH_MAX_LENGTH,
            code='context_repository_url_too_long',
            label='repository_url',
        )

    def validate_repository_root(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_PATH_MAX_LENGTH,
            code='context_repository_root_too_long',
            label='repository_root',
        )

    def validate_cwd(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_PATH_MAX_LENGTH,
            code='context_cwd_too_long',
            label='cwd',
        )

    def validate_query(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=CONTEXT_QUERY_MAX_LENGTH,
            code='context_query_too_large',
            label='query',
        )

    def validate_file_paths(self, value: list[str]) -> list[str]:
        return _validate_text_list(
            value,
            list_code='context_file_paths_too_many',
            item_code='context_file_paths_value_too_long',
            label='file_paths',
        )

    def validate_symbols(self, value: list[str]) -> list[str]:
        return _validate_text_list(
            value,
            list_code='context_symbols_too_many',
            item_code='context_symbols_value_too_long',
            label='symbols',
        )
