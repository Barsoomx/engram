from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import (
    EMBEDDING_DIMENSION,
    AnthropicMessagesGateway,
    EmbeddingCallInput,
    FakeProviderGateway,
    ModelPolicyError,
    OpenAICompatibleGateway,
    ProviderCallInput,
    ProviderSecretError,
    _resolve_base_url,
    default_base_url,
    encrypt_secret,
    get_provider_gateway,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _opener_returning(body: bytes) -> Any:
    def opener(request: Any, timeout: float = 30) -> _FakeResponse:
        opener.requests.append(request)

        return _FakeResponse(body)

    opener.requests = []  # type: ignore[attr-defined]

    return opener


def _opener_raising(error: Exception) -> Any:
    def opener(_request: Any, timeout: float = 30) -> Any:
        raise error

    return opener


def make_real_policy(
    organization: Any,
    project: Any,
    *,
    task_type: str = 'generation',
    base_url: str = 'https://provider.example/v1',
    raw_key: str = 'test-provider-key',
    provider: str = 'openai',
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='Org Provider',
        provider=provider,
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=None,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext=encrypt_secret(raw_key),
        hmac_digest='hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='Real policy',
        scope='project',
        task_type=task_type,
        provider=provider,
        model='gpt-4o-mini' if provider == 'openai' else 'glm-4.7',
        secret=secret,
        version=1,
        metadata={'base_url': base_url},
    )


@pytest.mark.django_db
def test_factory_returns_fake_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    monkeypatch.delenv('ENGRAM_PROVIDER_MODE', raising=False)

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, FakeProviderGateway)


@pytest.mark.django_db
def test_factory_returns_real_gateway_under_env(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, base_url='https://provider.example/v1', raw_key='real-key')
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway._base_url == 'https://provider.example/v1'
    assert gateway._api_key == 'real-key'


@pytest.mark.django_db
def test_factory_real_mode_requires_active_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='No envelope',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='P',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
    )
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    with pytest.raises(ProviderSecretError):
        get_provider_gateway(policy)


@pytest.mark.django_db
def test_openai_compatible_gateway_call_parses_completion() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {
        'choices': [{'message': {'content': 'Memory title\nBody line one\nBody line two'}}],
    }
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='real-call-1',
            trace_id='real-call-1',
            prompt='prompt text',
        ),
    )

    assert result.generated_title == 'Memory title'
    assert result.generated_body == 'Body line one\nBody line two'
    assert result.provider == 'openai'
    assert result.model == 'gpt-4o-mini'
    record = ProviderCallRecord.objects.get(id=result.call_record_id)
    assert record.task_type == 'generation'
    assert record.metadata['transport'] == 'http'
    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['model'] == 'gpt-4o-mini'
    assert sent_body['messages'][0]['content'] == 'prompt text'
    assert opener.requests[0].headers['Authorization'] == 'Bearer key'
    assert opener.requests[0].full_url == 'https://provider.example/v1/chat/completions'


@pytest.mark.django_db
def test_openai_compatible_gateway_strips_title_body_markers() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {
        'choices': [
            {
                'message': {
                    'content': (
                        'Title: Deposit retries are idempotent\n'
                        'Body: Retries reuse the accepted replay row.\n'
                        'Second body line.'
                    ),
                },
            },
        ],
    }
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='real-call-markers-1',
            trace_id='real-call-markers-1',
            prompt='prompt text',
        ),
    )

    assert result.generated_title == 'Deposit retries are idempotent'
    assert result.generated_body == 'Retries reuse the accepted replay row.\nSecond body line.'


@pytest.mark.django_db
@pytest.mark.parametrize('task_type', ['curation', 'digest'])
def test_openai_gateway_disables_thinking_for_deepseek_cheap_tiers(task_type: str) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type=task_type, provider='deepseek')
    policy.model = 'deepseek-v4-flash'
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'ds-{task_type}-1',
            trace_id=f'ds-{task_type}-1',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['thinking'] == {'type': 'disabled'}


@pytest.mark.django_db
def test_openai_gateway_keeps_thinking_for_deepseek_generation() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='generation', provider='deepseek')
    policy.model = 'deepseek-v4-pro'
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='ds-gen-1',
            trace_id='ds-gen-1',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'thinking' not in sent_body


@pytest.mark.django_db
def test_openai_gateway_omits_thinking_for_non_deepseek_cheap_tier() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation', provider='openai')
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='oa-cur-1',
            trace_id='oa-cur-1',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'thinking' not in sent_body


@pytest.mark.django_db
def test_openai_compatible_gateway_call_reuses_record() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='real-call-reuse',
        trace_id='real-call-reuse',
        prompt='prompt',
    )

    first = gateway.call(data)
    second = gateway.call(data)

    assert second.call_record_id == first.call_record_id
    assert ProviderCallRecord.objects.filter(request_id='real-call-reuse').count() == 1


@pytest.mark.django_db
def test_openai_compatible_gateway_embed_parses_vector() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    opener = _opener_returning(json.dumps({'data': [{'embedding': [0.1, 0.2, 0.3]}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='real-embed-1',
            trace_id='real-embed-1',
            text='text to embed',
        ),
    )

    assert len(result.embedding) == EMBEDDING_DIMENSION
    assert result.embedding[:3] == (0.1, 0.2, 0.3)
    assert all(component == 0.0 for component in result.embedding[3:])
    assert opener.requests[0].full_url == 'https://provider.example/v1/embeddings'


@pytest.mark.django_db
def test_openai_gateway_embed_truncates_oversized_vector_and_renormalizes() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    opener = _opener_returning(json.dumps({'data': [{'embedding': [1.0] * (EMBEDDING_DIMENSION * 2)}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='real-embed-fit-1',
            trace_id='real-embed-fit-1',
            text='text to embed',
        ),
    )

    assert len(result.embedding) == EMBEDDING_DIMENSION
    norm = sum(component**2 for component in result.embedding) ** 0.5
    assert norm == pytest.approx(1.0)


@pytest.mark.django_db
def test_openai_gateway_embed_keeps_exact_dimension_vector() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    exact = [float(index) for index in range(EMBEDDING_DIMENSION)]
    opener = _opener_returning(json.dumps({'data': [{'embedding': exact}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='real-embed-exact-1',
            trace_id='real-embed-exact-1',
            text='text to embed',
        ),
    )

    assert result.embedding == tuple(exact)


@pytest.mark.django_db
def test_openai_compatible_gateway_translates_http_error() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.HTTPError('url', 500, 'server error', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError, match='provider returned 500'):
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='real-call-error',
                trace_id='real-call-error',
                prompt='prompt',
            ),
        )


@pytest.mark.django_db
def test_factory_returns_anthropic_gateway_for_glm(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='anthropic',
        base_url='https://api.z.ai/api/anthropic',
        raw_key='glm-key',
    )
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, AnthropicMessagesGateway)
    assert gateway._base_url == 'https://api.z.ai/api/anthropic'
    assert gateway._api_key == 'glm-key'


@pytest.mark.django_db
def test_anthropic_gateway_call_parses_message() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    response = {'content': [{'type': 'text', 'text': 'Memory title\nBody line one\nBody line two'}]}
    opener = _opener_returning(json.dumps(response).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-call-1',
            trace_id='anthropic-call-1',
            prompt='prompt text',
        ),
    )

    assert result.generated_title == 'Memory title'
    assert result.generated_body == 'Body line one\nBody line two'
    assert result.model == 'glm-4.7'
    record = ProviderCallRecord.objects.get(id=result.call_record_id)
    assert record.metadata['transport'] == 'http-anthropic'
    sent = json.loads(opener.requests[0].data)
    assert sent['model'] == 'glm-4.7'
    assert sent['messages'][0]['content'] == 'prompt text'
    sent_headers = {key.lower(): value for key, value in opener.requests[0].headers.items()}
    assert sent_headers.get('x-api-key') == 'key'
    assert sent_headers.get('anthropic-version') == '2023-06-01'
    assert opener.requests[0].full_url == 'https://api.z.ai/api/anthropic/v1/messages'


@pytest.mark.django_db
def test_anthropic_gateway_embed_is_unsupported() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding', provider='anthropic')
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key')

    with pytest.raises(ModelPolicyError, match='do not expose embeddings'):
        gateway.embed(
            EmbeddingCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='anthropic-embed-1',
                trace_id='anthropic-embed-1',
                text='text',
            ),
        )


@pytest.mark.django_db
def test_openai_gateway_classifies_5xx_as_retryable() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.HTTPError('url', 503, 'service unavailable', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='classify-503',
                trace_id='classify-503',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_openai_gateway_classifies_429_as_retryable() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.HTTPError('url', 429, 'too many requests', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='classify-429',
                trace_id='classify-429',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_openai_gateway_classifies_400_as_terminal() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.HTTPError('url', 400, 'bad request', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='classify-400',
                trace_id='classify-400',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is False


@pytest.mark.django_db
def test_openai_gateway_classifies_url_error_as_retryable() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.URLError('timed out'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='classify-timeout',
                trace_id='classify-timeout',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_anthropic_gateway_classifies_5xx_as_retryable() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    opener = _opener_raising(urllib.error.HTTPError('url', 500, 'internal server error', {}, None))
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='anthropic-classify-500',
                trace_id='anthropic-classify-500',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is True


@pytest.mark.django_db
def test_anthropic_gateway_classifies_401_as_terminal() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    opener = _opener_raising(urllib.error.HTTPError('url', 401, 'unauthorized', {}, None))
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    with pytest.raises(ModelPolicyError) as exc_info:
        gateway.call(
            ProviderCallInput(
                organization_id=organization.id,
                project_id=project.id,
                team_id=None,
                policy=policy,
                request_id='anthropic-classify-401',
                trace_id='anthropic-classify-401',
                prompt='prompt',
            ),
        )

    assert exc_info.value.retryable is False


@pytest.mark.django_db
def test_openai_compatible_gateway_sends_system_role_when_system_prompt_set() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {
        'choices': [{'message': {'content': 'Title\nBody'}}],
    }
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='sys-prompt-openai-1',
            trace_id='sys-prompt-openai-1',
            prompt='user content',
            system_prompt='system instruction',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    assert sent['messages'][0] == {'role': 'system', 'content': 'system instruction'}
    assert sent['messages'][1] == {'role': 'user', 'content': 'user content'}


@pytest.mark.django_db
def test_openai_compatible_gateway_omits_system_role_when_system_prompt_empty() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {
        'choices': [{'message': {'content': 'Title\nBody'}}],
    }
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='no-sys-prompt-openai-1',
            trace_id='no-sys-prompt-openai-1',
            prompt='user content',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    assert len(sent['messages']) == 1
    assert sent['messages'][0]['role'] == 'user'


@pytest.mark.django_db
def test_anthropic_gateway_sends_system_field_when_system_prompt_set() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    response = {'content': [{'type': 'text', 'text': 'Title\nBody'}]}
    opener = _opener_returning(json.dumps(response).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='sys-prompt-anthropic-1',
            trace_id='sys-prompt-anthropic-1',
            prompt='user content',
            system_prompt='system instruction',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    assert sent['system'] == 'system instruction'
    assert sent['messages'] == [{'role': 'user', 'content': 'user content'}]


@pytest.mark.django_db
def test_anthropic_gateway_omits_system_field_when_system_prompt_empty() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    response = {'content': [{'type': 'text', 'text': 'Title\nBody'}]}
    opener = _opener_returning(json.dumps(response).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='no-sys-prompt-anthropic-1',
            trace_id='no-sys-prompt-anthropic-1',
            prompt='user content',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    assert 'system' not in sent
    assert sent['messages'] == [{'role': 'user', 'content': 'user content'}]


def test_default_base_url_deepseek() -> None:
    assert default_base_url('deepseek') == 'https://api.deepseek.com/v1'


def test_default_base_url_openai_unchanged() -> None:
    assert default_base_url('openai') == 'https://api.openai.com/v1'


@pytest.mark.django_db
def test_get_provider_gateway_deepseek_returns_openai_compatible_with_default_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='deepseek', base_url='', raw_key='ds-key')
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway._base_url == 'https://api.deepseek.com/v1'
    assert gateway._api_key == 'ds-key'


@pytest.mark.django_db
def test_deepseek_policy_metadata_base_url_override_used_by_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='deepseek',
        base_url='https://custom.deepseek.proxy/v1',
        raw_key='ds-key-override',
    )
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway._base_url == 'https://custom.deepseek.proxy/v1'


@pytest.mark.django_db
def test_existing_policy_no_base_url_resolves_to_provider_default() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='Org OpenAI',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    policy = ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='No metadata policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4o-mini',
        secret=secret,
        version=1,
    )

    resolved = _resolve_base_url(policy)

    assert resolved == 'https://api.openai.com/v1'


def test_default_base_url_anthropic() -> None:
    assert default_base_url('anthropic') == 'https://api.anthropic.com'


@pytest.mark.django_db
def test_get_provider_gateway_anthropic_returns_anthropic_host_with_blank_metadata_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='anthropic',
        base_url='',
        raw_key='anthropic-key',
    )
    monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')

    gateway = get_provider_gateway(policy)

    assert isinstance(gateway, AnthropicMessagesGateway)
    assert gateway._base_url == 'https://api.anthropic.com'
    assert gateway._api_key == 'anthropic-key'


@pytest.mark.django_db
def test_openai_gateway_sends_json_mode_for_candidates() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    completion = {'choices': [{'message': {'content': '{"memories": []}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-1',
            trace_id='json-mode-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_sends_json_mode_for_curation_judgment() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    completion = {'choices': [{'message': {'content': '{"decision": "keep_both"}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-2',
            trace_id='json-mode-2',
            prompt='prompt text',
            response_kind='curation_judgment',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_merges_thinking_and_json_mode_for_deepseek_candidates() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='deepseek', task_type='curation')
    completion = {'choices': [{'message': {'content': '{"memories": []}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-merge-1',
            trace_id='json-mode-merge-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['thinking'] == {'type': 'disabled'}
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_omits_json_mode_for_single() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {'choices': [{'message': {'content': 'Title\nBody'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-3',
            trace_id='json-mode-3',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'response_format' not in sent_body


@pytest.mark.django_db
def test_anthropic_gateway_forces_tool_for_candidates() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {
        'content': [
            {
                'type': 'tool_use',
                'name': 'emit_memories',
                'input': {'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]},
            },
        ],
    }
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-1',
            trace_id='anthropic-tool-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_memories'}
    assert sent_body['tools'][0]['name'] == 'emit_memories'
    assert sent_body['tools'][0]['input_schema']['required'] == ['memories']
    assert sent_body['max_tokens'] == 8192
    assert json.loads(result.generated_body) == {'memories': [{'title': 'T', 'body': 'B', 'confidence': 0.9}]}


@pytest.mark.django_db
def test_anthropic_gateway_forces_tool_for_curation_judgment() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {
        'content': [
            {'type': 'tool_use', 'name': 'emit_judgment', 'input': {'decision': 'merge', 'reason': 'same fact'}},
        ],
    }
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-2',
            trace_id='anthropic-tool-2',
            prompt='prompt text',
            response_kind='curation_judgment',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_judgment'}
    assert sent_body['tools'][0]['input_schema']['properties']['decision']['enum'] == [
        'merge',
        'keep_both',
        'reject',
    ]
    assert sent_body['max_tokens'] == 1024
    assert json.loads(result.generated_body) == {'decision': 'merge', 'reason': 'same fact'}


@pytest.mark.django_db
def test_anthropic_gateway_single_kind_has_no_tools_and_default_budget() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {'content': [{'type': 'text', 'text': 'Title\nBody'}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-3',
            trace_id='anthropic-tool-3',
            prompt='prompt text',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'tools' not in sent_body
    assert 'tool_choice' not in sent_body
    assert sent_body['max_tokens'] == 1024


@pytest.mark.django_db
def test_anthropic_gateway_max_tokens_metadata_override() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    policy.metadata = {**policy.metadata, 'max_tokens': 2048}
    policy.save(update_fields=['metadata'])
    message = {'content': [{'type': 'tool_use', 'name': 'emit_memories', 'input': {'memories': []}}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-4',
            trace_id='anthropic-tool-4',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == 2048


@pytest.mark.django_db
def test_anthropic_gateway_thinking_only_content_returns_empty_body() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {'content': [{'type': 'thinking', 'thinking': 'x'}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-6',
            trace_id='anthropic-tool-6',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    assert result.generated_body == ''


@pytest.mark.django_db
def test_anthropic_gateway_bool_max_tokens_metadata_falls_back_to_kind_default() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    policy.metadata = {**policy.metadata, 'max_tokens': True}
    policy.save(update_fields=['metadata'])
    message = {'content': [{'type': 'tool_use', 'name': 'emit_memories', 'input': {'memories': []}}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-7',
            trace_id='anthropic-tool-7',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == 8192


@pytest.mark.django_db
def test_anthropic_gateway_structured_kind_falls_back_to_text_block() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    message = {'content': [{'type': 'text', 'text': '{"memories": []}'}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-tool-5',
            trace_id='anthropic-tool-5',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    assert result.generated_body == '{"memories": []}'
