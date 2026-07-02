from __future__ import annotations

import json

from rest_framework import serializers

from engram.core.models import Runtime

HOOK_PAYLOAD_MAX_BYTES = 65536
HOOK_OBSERVATION_BODY_MAX_LENGTH = 16000
HOOK_PATH_MAX_LENGTH = 1024
HOOK_PATH_LIST_MAX_ITEMS = 100


def _limit_error(code: str, detail: str) -> dict[str, list[str]]:
    return {'code': [code], 'detail': [detail]}


def _json_size_bytes(value: object) -> int:
    serialized = json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    return len(serialized.encode())


def _validate_text_limit(value: str, *, max_length: int, code: str, label: str) -> str:
    if len(value) > max_length:
        raise serializers.ValidationError(_limit_error(code, f'{label} must be at most {max_length} characters.'))

    return value


def _text_list_error(
    values: list[str],
    *,
    list_code: str,
    item_code: str,
    label: str,
) -> dict[str, list[str]] | None:
    if len(values) > HOOK_PATH_LIST_MAX_ITEMS:
        return _limit_error(list_code, f'{label} must contain at most {HOOK_PATH_LIST_MAX_ITEMS} paths.')
    if any(len(value) > HOOK_PATH_MAX_LENGTH for value in values):
        return _limit_error(item_code, f'{label} entries must be at most {HOOK_PATH_MAX_LENGTH} characters.')

    return None


class HookDryRunSerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    agent_runtime = serializers.ChoiceField(choices=Runtime.values)
    agent_version = serializers.CharField(required=False, allow_blank=True, max_length=80)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)


class HookObservationSerializer(serializers.Serializer):
    type = serializers.CharField(max_length=80)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    body = serializers.CharField(required=False, allow_blank=True)
    files_read = serializers.ListField(child=serializers.CharField(), required=False)
    files_modified = serializers.ListField(child=serializers.CharField(), required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        errors = {}
        body = attrs.get('body', '')
        if isinstance(body, str) and len(body) > HOOK_OBSERVATION_BODY_MAX_LENGTH:
            errors['body'] = _limit_error(
                'hook_observation_body_too_large',
                f'observation body must be at most {HOOK_OBSERVATION_BODY_MAX_LENGTH} characters.',
            )
        for field_name, list_code, item_code in (
            (
                'files_read',
                'hook_observation_files_read_too_many',
                'hook_observation_files_read_path_too_long',
            ),
            (
                'files_modified',
                'hook_observation_files_modified_too_many',
                'hook_observation_files_modified_path_too_long',
            ),
        ):
            values = attrs.get(field_name, [])
            if isinstance(values, list):
                error = _text_list_error(
                    values,
                    list_code=list_code,
                    item_code=item_code,
                    label=field_name,
                )
                if error is not None:
                    errors[field_name] = error
        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class HookEventSerializer(serializers.Serializer):
    project_id = serializers.UUIDField(required=False, allow_null=True)
    team_id = serializers.UUIDField(required=False, allow_null=True)
    agent_runtime = serializers.ChoiceField(choices=Runtime.values)
    agent_version = serializers.CharField(required=False, allow_blank=True, max_length=80)
    agent_external_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    session_id = serializers.CharField(max_length=255)
    event_id = serializers.CharField(max_length=255)
    idempotency_key = serializers.CharField(max_length=255)
    event_type = serializers.CharField(max_length=120)
    payload_schema_version = serializers.CharField(max_length=40)
    sequence_number = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    occurred_at = serializers.DateTimeField(required=False, allow_null=True)
    content_hash = serializers.CharField(max_length=128)
    request_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    trace_id = serializers.CharField(required=False, allow_blank=True, max_length=255)
    repository_url = serializers.CharField(required=False, allow_blank=True)
    repository_root = serializers.CharField(required=False, allow_blank=True)
    branch = serializers.CharField(required=False, allow_blank=True, max_length=255)
    cwd = serializers.CharField(required=False, allow_blank=True)
    payload = serializers.JSONField()
    observation = HookObservationSerializer(required=False)

    def validate_repository_url(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=HOOK_PATH_MAX_LENGTH,
            code='hook_repository_url_too_long',
            label='repository_url',
        )

    def validate_repository_root(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=HOOK_PATH_MAX_LENGTH,
            code='hook_repository_root_too_long',
            label='repository_root',
        )

    def validate_cwd(self, value: str) -> str:
        return _validate_text_limit(
            value,
            max_length=HOOK_PATH_MAX_LENGTH,
            code='hook_cwd_too_long',
            label='cwd',
        )

    def validate_payload(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise serializers.ValidationError('Must be a JSON object.')
        if _json_size_bytes(value) > HOOK_PAYLOAD_MAX_BYTES:
            raise serializers.ValidationError(
                _limit_error('hook_payload_too_large', f'payload must be at most {HOOK_PAYLOAD_MAX_BYTES} bytes.'),
            )

        return value
