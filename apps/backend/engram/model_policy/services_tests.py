from __future__ import annotations

import json
import urllib.error

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.core.models import AuditEvent
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.real_provider_tests import _opener_raising, make_real_policy
from engram.model_policy.services import (
    _ANTHROPIC_STRUCTURED_TOOLS,
    AnthropicMessagesGateway,
    CreateProviderSecret,
    EmbeddingCallInput,
    OpenAICompatibleGateway,
    ProviderCallInput,
    ProviderSecretInput,
    RotateProviderSecret,
    RotateProviderSecretInput,
    UpdateModelPolicy,
    UpdateModelPolicyInput,
    _split_completion,
    generated_candidates_payload,
    secret_fingerprint,
)

PLAINTEXT_PROVIDER_SECRET = 'provider-plaintext-value-abc123'


def test_split_completion_strips_title_and_body_labels_same_line() -> None:
    title, body = _split_completion('Title: Memory title\nBody: line one\nline two')

    assert title == 'Memory title'
    assert body == 'line one\nline two'


def test_split_completion_strips_title_label_blank_line_layout() -> None:
    title, body = _split_completion('Title: Memory title\n\nline one\nline two')

    assert title == 'Memory title'
    assert body == 'line one\nline two'


def test_split_completion_is_case_insensitive() -> None:
    title, body = _split_completion('title: Memory title\nbody: line one')

    assert title == 'Memory title'
    assert body == 'line one'


def test_split_completion_strips_label_from_single_line_body() -> None:
    title, body = _split_completion('Title: X')

    assert title == 'X'
    assert body == 'X'


def test_split_completion_no_op_without_labels() -> None:
    title, body = _split_completion('Memory title\nline one\nline two')

    assert title == 'Memory title'
    assert body == 'line one\nline two'


def test_split_completion_keeps_255_char_cap_after_stripping_label() -> None:
    long_title = 'x' * 300
    title, _body = _split_completion(f'Title: {long_title}\nBody: line one')

    assert title == long_title[:255]


@pytest.mark.django_db
def test_openai_gateway_call_raises_provider_timeout_on_timeout_error() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(TimeoutError('timed out'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='timeout-call-1',
                trace_id='timeout-call-1',
                prompt='prompt text',
            ),
        )

    assert exc_info.value.code == 'provider_timeout'
    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_openai_gateway_embed_raises_provider_timeout_on_timeout_error() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(TimeoutError('timed out'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.embed(
            EmbeddingCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='timeout-embed-1',
                trace_id='timeout-embed-1',
                text='text to embed',
            ),
        )

    assert exc_info.value.code == 'provider_timeout'
    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_anthropic_gateway_call_raises_provider_timeout_on_timeout_error() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic')
    opener = _opener_raising(TimeoutError('timed out'))
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.com', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='timeout-call-2',
                trace_id='timeout-call-2',
                prompt='prompt text',
            ),
        )

    assert exc_info.value.code == 'provider_timeout'
    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_update_model_policy_clear_context_window_tokens_removes_override_keeps_other_keys() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='clear-context-window-1',
            actor_id='actor-1',
            clear_context_window_tokens=True,
        ),
    )

    assert 'context_window_tokens' not in updated.metadata
    assert updated.metadata.get('base_url') == 'https://provider.example/v1'
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_clear_context_window_tokens_is_noop_when_absent() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)

    assert 'context_window_tokens' not in policy.metadata

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='clear-context-window-2',
            actor_id='actor-1',
            clear_context_window_tokens=True,
        ),
    )

    assert 'context_window_tokens' not in updated.metadata
    assert updated.metadata == {'base_url': 'https://provider.example/v1'}
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_omitted_context_window_tokens_preserves_override() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='omit-context-window-1',
            actor_id='actor-1',
        ),
    )

    assert updated.metadata.get('context_window_tokens') == 8000
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_sets_context_window_tokens_when_not_clearing() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='set-context-window-1',
            actor_id='actor-1',
            context_window_tokens=32000,
        ),
    )

    assert updated.metadata.get('context_window_tokens') == 32000
    assert updated.version == 2


@pytest.mark.django_db
def test_openai_gateway_call_raises_provider_timeout_when_url_error_wraps_timeout() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.URLError(TimeoutError('timed out')))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='timeout-call-3',
                trace_id='timeout-call-3',
                prompt='prompt text',
            ),
        )

    assert exc_info.value.code == 'provider_timeout'
    assert exc_info.value.retryable is True


def test_curation_judgment_tool_schema_decision_enum_includes_contradicts() -> None:
    decision_schema = _ANTHROPIC_STRUCTURED_TOOLS['curation_judgment']['input_schema']['properties']['decision']

    assert 'contradicts' in decision_schema['enum']


def test_emit_memories_tool_schema_declares_kind_enum() -> None:
    memory_schema = _ANTHROPIC_STRUCTURED_TOOLS['candidates']['input_schema']['properties']['memories']['items']
    kind_schema = memory_schema['properties']['kind']

    assert kind_schema['enum'] == ['decision', 'convention', 'gotcha', 'architecture', 'incident']


def test_generated_candidates_payload_first_memory_carries_kind() -> None:
    payload = json.loads(generated_candidates_payload('a prompt'))

    memories = payload['memories']
    assert memories[0]['kind'] == 'gotcha'
    assert 'kind' not in memories[1]


@pytest.mark.django_db
def test_create_provider_secret_audit_stores_fingerprint_not_cleartext() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    secret = CreateProviderSecret().execute(
        ProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            name='Team OpenAI',
            provider='openai',
            scope='team',
            raw_secret=PLAINTEXT_PROVIDER_SECRET,
            request_id='req-create-audit',
            actor_id=str(owner.id),
        ),
    )
    event = AuditEvent.objects.get(target_id=str(secret.id), event_type='ProviderSecretCreated')

    assert 'raw_secret' not in event.metadata
    assert event.metadata['fingerprint'] == secret_fingerprint(PLAINTEXT_PROVIDER_SECRET)
    assert PLAINTEXT_PROVIDER_SECRET not in json.dumps(event.metadata)


@pytest.mark.django_db
def test_rotate_provider_secret_audit_stores_fingerprint_not_cleartext() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    secret = CreateProviderSecret().execute(
        ProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            name='Team OpenAI',
            provider='openai',
            scope='team',
            raw_secret=PLAINTEXT_PROVIDER_SECRET,
            request_id='req-create-audit-2',
            actor_id=str(owner.id),
        ),
    )
    rotated = 'rotated-plaintext-value-xyz789'
    RotateProviderSecret().execute(
        RotateProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            secret_id=secret.id,
            raw_secret=rotated,
            request_id='req-rotate-audit',
            actor_id=str(owner.id),
            allowed_team_ids=(team.id,),
        ),
    )
    event = AuditEvent.objects.get(target_id=str(secret.id), event_type='ProviderSecretRotated')

    assert 'raw_secret' not in event.metadata
    assert event.metadata['fingerprint'] == secret_fingerprint(rotated)
    assert rotated not in json.dumps(event.metadata)
