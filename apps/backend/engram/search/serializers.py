from __future__ import annotations

from rest_framework import serializers

from engram.core.models import MEMORY_KINDS

SEARCH_QUERY_MAX_LENGTH = 8000
SEARCH_LIST_VALUE_MAX_LENGTH = 1024
SEARCH_LIST_MAX_ITEMS = 100
SEARCH_KINDS_MAX_ITEMS = 6


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
    if len(values) > SEARCH_LIST_MAX_ITEMS:
        raise serializers.ValidationError(
            _limit_error(list_code, f'{label} must contain at most {SEARCH_LIST_MAX_ITEMS} entries.'),
        )
    if any(len(value) > SEARCH_LIST_VALUE_MAX_LENGTH for value in values):
        raise serializers.ValidationError(
            _limit_error(item_code, f'{label} entries must be at most {SEARCH_LIST_VALUE_MAX_LENGTH} characters.'),
        )

    return values


class SearchRequestSerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    repository_url = serializers.CharField(required=False, allow_blank=True, default='')
    repository_root = serializers.CharField(required=False, allow_blank=True, default='')
    query = serializers.CharField(required=False, allow_blank=True, default='')
    file_paths = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    symbols = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    kinds = serializers.ListField(
        child=serializers.CharField(max_length=40),
        required=False,
        default=list,
        max_length=SEARCH_KINDS_MAX_ITEMS,
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=10, default=5)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)
    trace_id = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)

    def validate_repository_url(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=SEARCH_LIST_VALUE_MAX_LENGTH,
            code='search_repository_url_too_long',
            label='repository_url',
        )

    def validate_repository_root(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=SEARCH_LIST_VALUE_MAX_LENGTH,
            code='search_repository_root_too_long',
            label='repository_root',
        )

    def validate_query(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=SEARCH_QUERY_MAX_LENGTH,
            code='search_query_too_large',
            label='query',
        )

    def validate_file_paths(self, value: list[str]) -> list[str]:
        return _validate_text_list(
            value,
            list_code='search_file_paths_too_many',
            item_code='search_file_paths_value_too_long',
            label='file_paths',
        )

    def validate_symbols(self, value: list[str]) -> list[str]:
        return _validate_text_list(
            value,
            list_code='search_symbols_too_many',
            item_code='search_symbols_value_too_long',
            label='symbols',
        )

    def validate_kinds(self, value: list[str]) -> list[str]:
        invalid = [item for item in value if item not in MEMORY_KINDS]
        if invalid:
            raise serializers.ValidationError(
                _limit_error('search_kinds_invalid', f'Invalid kind(s): {", ".join(invalid)}.'),
            )

        return value
