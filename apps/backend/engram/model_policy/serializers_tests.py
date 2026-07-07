from __future__ import annotations

import uuid

from pytest_django.fixtures import SettingsWrapper

from engram.model_policy.serializers import ModelPolicyCreateSerializer, ModelPolicyUpdateSerializer


def _create_payload(base_url: str) -> dict[str, object]:
    return {
        'project_id': str(uuid.uuid4()),
        'name': 'Policy',
        'scope': 'organization',
        'task_type': 'generation',
        'provider': 'openai',
        'model': 'gpt-4o-mini',
        'secret_id': str(uuid.uuid4()),
        'base_url': base_url,
        'request_id': 'req-1',
    }


def test_create_serializer_rejects_metadata_ssrf_url(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    serializer = ModelPolicyCreateSerializer(data=_create_payload('http://169.254.169.254/v1'))

    assert serializer.is_valid() is False
    assert 'base_url' in serializer.errors


def test_create_serializer_accepts_https_public_address(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    serializer = ModelPolicyCreateSerializer(data=_create_payload('https://8.8.8.8/v1'))

    assert serializer.is_valid() is True


def test_update_serializer_rejects_metadata_ssrf_url(settings: SettingsWrapper) -> None:
    settings.ENVIRONMENT = 'production'
    serializer = ModelPolicyUpdateSerializer(
        data={
            'project_id': str(uuid.uuid4()),
            'base_url': 'https://10.0.0.5/v1',
            'request_id': 'req-2',
        },
    )

    assert serializer.is_valid() is False
    assert 'base_url' in serializer.errors
