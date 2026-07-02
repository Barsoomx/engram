from __future__ import annotations

import urllib.error

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.real_provider_tests import _opener_raising, make_real_policy
from engram.model_policy.services import (
    AnthropicMessagesGateway,
    EmbeddingCallInput,
    OpenAICompatibleGateway,
    ProviderCallInput,
    _split_completion,
)


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
