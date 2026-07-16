from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest
from structlog.testing import capture_logs

from engram.context.context_api_tests import create_project_scope
from engram.core.models import AuditEvent
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.models import ProviderCallRecord
from engram.model_policy.real_provider_tests import _opener_raising, _opener_returning, make_real_policy
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


def _openai_chat_body(content: str, usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _anthropic_message_body(content: str, usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'content': [{'type': 'text', 'text': content}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _openai_embedding_body(embedding: list[float], usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'data': [{'embedding': embedding}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _openai_call(policy: Any, prompt: str, body: bytes) -> ProviderCallRecord:
    gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=_opener_returning(body),
    )
    result = gateway.call(
        ProviderCallInput(
            organization_id=policy.organization_id,
            project_id=policy.project_id,
            team_id=None,
            policy=policy,
            request_id='cost-call-1',
            trace_id='cost-call-1',
            prompt=prompt,
        ),
    )

    return ProviderCallRecord.objects.get(id=result.call_record_id)


@pytest.mark.django_db
def test_openai_gateway_records_provider_token_usage_when_usage_present() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 120, 'completion_tokens': 45, 'total_tokens': 165},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.token_usage == {
        'input_tokens': 120,
        'output_tokens': 45,
        'total_tokens': 165,
        'source': 'provider',
    }


@pytest.mark.django_db
def test_openai_gateway_falls_back_to_estimated_token_usage_without_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    content = 'Title: Memory\nBody: one two'
    body = _openai_chat_body(content)

    record = _openai_call(policy, 'alpha beta gamma', body)

    assert record.token_usage == {
        'input_tokens': 3,
        'output_tokens': len(content.split()),
        'source': 'estimated',
    }


@pytest.mark.django_db
def test_openai_gateway_computes_policy_cost_when_pricing_and_usage_present() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': '0.28', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 1_000_000, 'completion_tokens': 1_000_000, 'total_tokens': 2_000_000},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': False,
        'cost_usd': '0.700000',
        'pricing_source': 'policy',
    }


@pytest.mark.django_db
def test_openai_gateway_marks_no_usage_cost_when_pricing_but_no_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': '0.28', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body('Title: Memory\nBody: line one')

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'no_usage',
    }


@pytest.mark.django_db
def test_openai_gateway_marks_unknown_cost_without_pricing() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'unknown',
    }


@pytest.mark.django_db
def test_openai_gateway_ignores_malformed_pricing_and_still_records() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': 'not-a-number', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    )

    with capture_logs() as logs:
        record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'unknown',
    }
    assert any(entry.get('event') == 'provider_pricing_malformed' for entry in logs)


@pytest.mark.django_db
def test_anthropic_gateway_records_provider_token_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic')
    body = _anthropic_message_body(
        'Title: Memory\nBody: line one',
        usage={'input_tokens': 30, 'output_tokens': 12},
    )
    gateway = AnthropicMessagesGateway(
        base_url='https://api.anthropic.com',
        api_key='key',
        opener=_opener_returning(body),
    )

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='cost-call-anthropic-1',
            trace_id='cost-call-anthropic-1',
            prompt='a prompt',
        ),
    )
    record = ProviderCallRecord.objects.get(id=result.call_record_id)

    assert record.token_usage == {
        'input_tokens': 30,
        'output_tokens': 12,
        'total_tokens': 42,
        'source': 'provider',
    }


@pytest.mark.django_db
def test_openai_gateway_embed_records_usage_and_input_only_cost() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='embedding',
        metadata={'pricing': {'input_per_mtok': '0.02'}},
    )
    body = _openai_embedding_body(
        [0.1, 0.2, 0.3],
        usage={'prompt_tokens': 1_000_000, 'total_tokens': 1_000_000},
    )
    gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=_opener_returning(body),
    )

    result = gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='cost-embed-1',
            trace_id='cost-embed-1',
            text='text to embed',
        ),
    )
    record = ProviderCallRecord.objects.get(id=result.call_record_id)

    assert record.token_usage == {
        'input_tokens': 1_000_000,
        'output_tokens': 0,
        'total_tokens': 1_000_000,
        'source': 'provider',
    }
    assert record.cost_metadata == {
        'estimated': False,
        'cost_usd': '0.020000',
        'pricing_source': 'policy',
    }


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
def test_openai_curation_decision_prompt_carries_verdict_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('{}'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='curation-schema-1',
            trace_id='curation-schema-1',
            prompt='{"schema":"curation_judge_input.v1"}',
            response_kind='curation_decision_v1',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    user_message = sent['messages'][-1]['content']

    assert 'exactly one JSON object' in user_message
    assert 'candidate_evidence_refs' in user_message
    assert 'supersede_memory' in user_message
    assert 'temporal_order' in user_message
    assert user_message.rstrip().endswith('{"schema":"curation_judge_input.v1"}')


@pytest.mark.django_db
def test_openai_single_kind_prompt_has_no_curation_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('Title: X\nBody: Y'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='single-schema-1',
            trace_id='single-schema-1',
            prompt='plain prompt',
        ),
    )

    sent = json.loads(opener.requests[0].data)

    assert sent['messages'][-1]['content'] == 'plain prompt'


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


def test_distill_extract_tool_schema_matches_provider_contract() -> None:
    assert 'distill_extract.v1' in _ANTHROPIC_STRUCTURED_TOOLS
    tool = _ANTHROPIC_STRUCTURED_TOOLS['distill_extract.v1']

    assert tool['name'] == 'emit_distillation_extraction'
    schema = tool['input_schema']
    assert schema['type'] == 'object'
    assert set(schema['properties']) == {'memories', 'no_signal_observation_ids'}
    assert set(schema['required']) == {'memories', 'no_signal_observation_ids'}
    assert schema['additionalProperties'] is False

    memories = schema['properties']['memories']
    assert memories['type'] == 'array'
    assert memories['maxItems'] == 12
    memory = memories['items']
    assert memory['type'] == 'object'
    assert memory['additionalProperties'] is False
    assert set(memory['required']) == {'title', 'body', 'confidence', 'supporting_observation_ids'}
    assert set(memory['properties']) == {
        'title',
        'body',
        'confidence',
        'supporting_observation_ids',
        'kind',
    }
    assert memory['properties']['title'] == {'type': 'string', 'minLength': 1, 'maxLength': 255}
    assert memory['properties']['body'] == {'type': 'string', 'maxLength': 3000}
    assert memory['properties']['confidence'] == {'type': 'number', 'minimum': 0, 'maximum': 1}
    assert memory['properties']['supporting_observation_ids'] == {
        'type': 'array',
        'items': {'type': 'string'},
        'minItems': 1,
        'uniqueItems': True,
    }
    assert schema['properties']['no_signal_observation_ids'] == {
        'type': 'array',
        'items': {'type': 'string'},
        'uniqueItems': True,
    }
    assert memory['properties']['kind']['enum'] == [
        'decision',
        'convention',
        'gotcha',
        'architecture',
        'incident',
    ]


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
