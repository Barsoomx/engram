from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.core.models import AuditResult
from engram.model_policy import services
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
    deepseek_thinking_override,
    default_base_url,
    encrypt_secret,
    get_provider_gateway,
    policy_supports_json_object,
    resolve_context_window_tokens,
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
        opener.timeouts.append(timeout)

        return _FakeResponse(body)

    opener.requests = []  # type: ignore[attr-defined]
    opener.timeouts = []  # type: ignore[attr-defined]

    return opener


def _opener_raising(error: Exception) -> Any:
    def opener(_request: Any, timeout: float = 30) -> Any:
        raise error

    return opener


def _opener_returning_sequence(bodies: list[bytes]) -> Any:
    def opener(request: Any, timeout: float = 30) -> _FakeResponse:
        index = len(opener.requests)  # type: ignore[attr-defined]
        opener.requests.append(request)  # type: ignore[attr-defined]

        return _FakeResponse(bodies[index])

    opener.requests = []  # type: ignore[attr-defined]

    return opener


_ANTHROPIC_TOOL_NAME_BY_KIND = {
    'candidates': 'emit_memories',
    'curation_judgment': 'emit_judgment',
    'distill_extract.v1': 'emit_distillation_extraction',
}


def _response_bytes_for(gateway_cls: type, response_kind: str, label: str) -> tuple[bytes, str, str]:
    if response_kind == 'single':
        content = f'Response title {label}\nResponse body {label} line'
        title = f'Response title {label}'
        body = f'Response body {label} line'
    elif response_kind == 'candidates':
        payload: dict[str, Any] = {
            'memories': [
                {
                    'title': f'Memory title {label}',
                    'body': f'Memory body {label}',
                    'confidence': 0.5,
                    'supporting_observation_ids': [],
                    'source_ids': [0],
                },
            ],
        }
        content = json.dumps(payload)
        title = ''
        body = content
    elif response_kind == 'distill_extract.v1':
        suffix = '1' if label == 'A' else '2'
        payload = {
            'memories': [
                {
                    'title': f'Distilled title {label}',
                    'body': f'Distilled body {label}',
                    'confidence': 0.8,
                    'supporting_observation_ids': [f'11111111-1111-4111-8111-11111111111{suffix}'],
                },
            ],
            'no_signal_observation_ids': [],
        }
        content = json.dumps(payload)
        title = ''
        body = content
    else:
        payload = {'decision': 'reject', 'reason': f'reason {label}'}
        content = json.dumps(payload)
        title = ''
        body = content

    if gateway_cls is OpenAICompatibleGateway:
        response: dict[str, Any] = {'choices': [{'message': {'content': content}}]}
    elif response_kind == 'single':
        response = {'content': [{'type': 'text', 'text': content}]}
    else:
        tool_name = _ANTHROPIC_TOOL_NAME_BY_KIND[response_kind]
        response = {'content': [{'type': 'tool_use', 'name': tool_name, 'input': json.loads(content)}]}

    return json.dumps(response).encode(), title, body


@pytest.mark.django_db
@pytest.mark.parametrize('response_kind', ['single', 'candidates', 'curation_judgment', 'distill_extract.v1'])
@pytest.mark.parametrize('gateway_cls', [OpenAICompatibleGateway, AnthropicMessagesGateway])
def test_gateway_call_never_echoes_prompt_on_repeated_request_id(
    gateway_cls: type,
    response_kind: str,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    provider = 'openai' if gateway_cls is OpenAICompatibleGateway else 'anthropic'
    policy = make_real_policy(organization, project, provider=provider, base_url='https://provider.example/v1')

    body_a, _title_a, expected_body_a = _response_bytes_for(gateway_cls, response_kind, 'A')
    body_b, title_b, expected_body_b = _response_bytes_for(gateway_cls, response_kind, 'B')
    opener = _opener_returning_sequence([body_a, body_b])
    gateway = gateway_cls(base_url='https://provider.example/v1', api_key='key', opener=opener)

    prompt_text = 'PROMPT_MARKER_never_should_appear_in_output for repeated request replay'
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='anti-echo-1',
        trace_id='anti-echo-1',
        prompt=prompt_text,
        response_kind=response_kind,
    )

    first = gateway.call(data)
    second = gateway.call(data)

    assert len(opener.requests) == 2
    assert second.generated_body == expected_body_b
    assert second.generated_body != expected_body_a
    assert second.generated_title == title_b
    assert prompt_text not in second.generated_body
    assert prompt_text not in second.generated_title
    assert second.call_record_id != first.call_record_id
    assert ProviderCallRecord.objects.filter(request_id='anti-echo-1').count() == 2


def make_real_policy(
    organization: Any,
    project: Any,
    *,
    task_type: str = 'generation',
    base_url: str = 'https://provider.example/v1',
    raw_key: str = 'test-provider-key',
    provider: str = 'openai',
    metadata: dict[str, object] | None = None,
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
        metadata={'base_url': base_url, **(metadata or {})},
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
def test_openai_compatible_gateway_call_makes_fresh_provider_call_on_repeat() -> None:
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

    assert second.call_record_id != first.call_record_id
    assert len(opener.requests) == 2
    assert ProviderCallRecord.objects.filter(request_id='real-call-reuse').count() == 2


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
@pytest.mark.parametrize(
    'response_kind',
    ['candidates', 'curation_judgment', 'distill_extract.v1', 'distill_reduce.v1'],
)
def test_openai_gateway_sends_json_mode_for_structured_kinds(response_kind: str) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    if response_kind == 'curation_judgment':
        content = '{"decision": "keep_both"}'
    elif response_kind == 'distill_extract.v1':
        content = '{"memories": [], "no_signal_observation_ids": []}'
    else:
        content = '{"memories": []}'
    completion = {'choices': [{'message': {'content': content}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'json-mode-{response_kind}',
            trace_id=f'json-mode-{response_kind}',
            prompt='prompt text',
            response_kind=response_kind,
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
@pytest.mark.parametrize('response_kind', ['candidates', 'distill_extract.v1', 'distill_reduce.v1'])
def test_openai_gateway_omits_json_mode_when_policy_disables_it(response_kind: str) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation', metadata={'json_mode': False})
    content = (
        '{"memories": [], "no_signal_observation_ids": []}'
        if response_kind == 'distill_extract.v1'
        else '{"memories": []}'
    )
    completion = {'choices': [{'message': {'content': content}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'json-mode-opt-out-{response_kind}',
            trace_id=f'json-mode-opt-out-{response_kind}',
            prompt='prompt text',
            response_kind=response_kind,
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'response_format' not in sent_body


@pytest.mark.django_db
def test_openai_gateway_disables_thinking_but_omits_json_mode_for_deepseek_candidates() -> None:
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
    assert 'response_format' not in sent_body


def test_deepseek_thinking_override_enables_for_curation_decision() -> None:
    assert deepseek_thinking_override('deepseek', 'curation', 'curation_decision_v1') == {
        'thinking': {'type': 'enabled'},
    }


def test_deepseek_thinking_override_disables_for_other_deepseek_curation_kinds() -> None:
    assert deepseek_thinking_override('deepseek', 'curation', 'candidates') == {'thinking': {'type': 'disabled'}}
    assert deepseek_thinking_override('deepseek', 'digest', 'curation_judgment') == {'thinking': {'type': 'disabled'}}


def test_deepseek_thinking_override_empty_for_other_providers_and_tasks() -> None:
    assert deepseek_thinking_override('openai', 'curation', 'curation_decision_v1') == {}
    assert deepseek_thinking_override('deepseek', 'generation', 'candidates') == {}


@pytest.mark.django_db
def test_openai_gateway_enables_thinking_for_deepseek_curation_decision_only() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='deepseek', task_type='curation')
    decision_opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    decision_gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=decision_opener,
    )
    candidates_opener = _opener_returning(
        json.dumps({'choices': [{'message': {'content': '{"memories": []}'}}]}).encode(),
    )
    candidates_gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=candidates_opener,
    )

    decision_gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='ds-decision-1',
            trace_id='ds-decision-1',
            prompt='prompt text',
            response_kind='curation_decision_v1',
        ),
    )
    candidates_gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='ds-candidates-1',
            trace_id='ds-candidates-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    decision_body = json.loads(decision_opener.requests[0].data)
    candidates_body = json.loads(candidates_opener.requests[0].data)
    assert decision_body['thinking'] == {'type': 'enabled'}
    assert candidates_body['thinking'] == {'type': 'disabled'}


@pytest.mark.django_db
def test_openai_gateway_sends_json_mode_for_deepseek_candidates_with_metadata_opt_in() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        provider='deepseek',
        task_type='curation',
        metadata={'json_mode': True},
    )
    completion = {'choices': [{'message': {'content': '{"memories": []}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-opt-in-1',
            trace_id='json-mode-opt-in-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['thinking'] == {'type': 'disabled'}
    assert sent_body['response_format'] == {'type': 'json_object'}


@pytest.mark.django_db
def test_openai_gateway_omits_json_mode_for_openai_candidates_with_metadata_opt_out() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation', metadata={'json_mode': False})
    completion = {'choices': [{'message': {'content': '{"memories": []}'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='json-mode-opt-out-1',
            trace_id='json-mode-opt-out-1',
            prompt='prompt text',
            response_kind='candidates',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'response_format' not in sent_body


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
def test_anthropic_gateway_forces_tool_for_distill_extract_and_returns_tool_input() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    expected = {
        'memories': [
            {
                'title': 'T',
                'body': 'B',
                'confidence': 0.9,
                'supporting_observation_ids': ['11111111-1111-4111-8111-111111111111'],
            },
        ],
        'no_signal_observation_ids': [],
    }
    message = {
        'content': [
            {
                'type': 'tool_use',
                'name': 'emit_distillation_extraction',
                'input': expected,
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
            request_id='anthropic-distill-1',
            trace_id='anthropic-distill-1',
            prompt='prompt text',
            response_kind='distill_extract.v1',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'tool_choice' in sent_body
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_distillation_extraction'}
    assert 'tools' in sent_body
    assert sent_body['tools'][0]['name'] == 'emit_distillation_extraction'
    assert sent_body['tools'][0]['input_schema']['required'] == ['memories', 'no_signal_observation_ids']
    assert sent_body['max_tokens'] == 8192
    assert json.loads(result.generated_body) == expected


@pytest.mark.django_db
def test_anthropic_gateway_forces_closed_distill_reduce_tool_with_string_source_ids() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    expected = {
        'memories': [
            {
                'title': 'T',
                'body': 'B',
                'confidence': 0.9,
                'source_ids': ['draft-a', 'draft-b'],
            },
        ],
    }
    message = {
        'content': [
            {
                'type': 'tool_use',
                'name': 'emit_distillation_reduction',
                'input': expected,
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
            request_id='anthropic-distill-reduce-1',
            trace_id='anthropic-distill-reduce-1',
            prompt='prompt text',
            response_kind='distill_reduce.v1',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_distillation_reduction'}
    tool = sent_body['tools'][0]
    assert tool['name'] == 'emit_distillation_reduction'
    assert tool['input_schema']['required'] == ['memories']
    assert tool['input_schema']['additionalProperties'] is False
    memory_schema = tool['input_schema']['properties']['memories']['items']
    assert memory_schema['additionalProperties'] is False
    assert memory_schema['properties']['source_ids']['items'] == {'type': 'string'}
    assert sent_body['max_tokens'] == 8192
    assert json.loads(result.generated_body) == expected


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
        'contradicts',
    ]
    assert sent_body['tools'][0]['input_schema']['required'] == ['decision', 'reason']
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
@pytest.mark.parametrize(
    ('response_kind', 'metadata_max_tokens', 'expected_max_tokens'),
    [
        ('candidates', 2048, 2048),
        ('distill_extract.v1', 2048, 8192),
        ('distill_extract.v1', 20000, 8192),
        ('distill_reduce.v1', 2048, 8192),
        ('distill_reduce.v1', 20000, 8192),
    ],
)
def test_anthropic_gateway_max_tokens_metadata_override(
    response_kind: str,
    metadata_max_tokens: int,
    expected_max_tokens: int,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    policy.metadata = {**policy.metadata, 'max_tokens': metadata_max_tokens}
    policy.save(update_fields=['metadata'])
    if response_kind == 'distill_extract.v1':
        tool_name = 'emit_distillation_extraction'
        tool_input = {'memories': [], 'no_signal_observation_ids': []}
    elif response_kind == 'distill_reduce.v1':
        tool_name = 'emit_distillation_reduction'
        tool_input = {'memories': []}
    else:
        tool_name = 'emit_memories'
        tool_input = {'memories': []}
    message = {'content': [{'type': 'tool_use', 'name': tool_name, 'input': tool_input}]}
    opener = _opener_returning(json.dumps(message).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'anthropic-tool-4-{response_kind}-{metadata_max_tokens}',
            trace_id=f'anthropic-tool-4-{response_kind}-{metadata_max_tokens}',
            prompt='prompt text',
            response_kind=response_kind,
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == expected_max_tokens


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


@pytest.mark.django_db
def test_openai_gateway_chat_completion_uses_default_http_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    monkeypatch.delenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', raising=False)
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='timeout-default-1',
            trace_id='timeout-default-1',
            prompt='prompt text',
        ),
    )

    assert opener.timeouts == [60]


@pytest.mark.django_db
def test_openai_gateway_chat_completion_honors_env_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '180')
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='timeout-override-1',
            trace_id='timeout-override-1',
            prompt='prompt text',
        ),
    )

    assert opener.timeouts == [180]


@pytest.mark.django_db
def test_openai_gateway_embeddings_use_shorter_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    monkeypatch.delenv('ENGRAM_EMBEDDING_HTTP_TIMEOUT', raising=False)
    opener = _opener_returning(json.dumps({'data': [{'embedding': [0.1, 0.2, 0.3]}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='embed-timeout-default-1',
            trace_id='embed-timeout-default-1',
            text='text to embed',
        ),
    )

    assert opener.timeouts == [30]


@pytest.mark.django_db
def test_openai_gateway_embeddings_honor_env_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    monkeypatch.setenv('ENGRAM_EMBEDDING_HTTP_TIMEOUT', '5')
    opener = _opener_returning(json.dumps({'data': [{'embedding': [0.1, 0.2, 0.3]}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='embed-timeout-override-1',
            trace_id='embed-timeout-override-1',
            text='text to embed',
        ),
    )

    assert opener.timeouts == [5]


@pytest.mark.django_db
def test_anthropic_gateway_messages_uses_default_http_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    monkeypatch.delenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', raising=False)
    opener = _opener_returning(json.dumps({'content': [{'text': 'Title\nBody'}]}).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-timeout-default-1',
            trace_id='anthropic-timeout-default-1',
            prompt='prompt text',
        ),
    )

    assert opener.timeouts == [60]


@pytest.mark.django_db
def test_anthropic_gateway_messages_honors_env_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '240')
    opener = _opener_returning(json.dumps({'content': [{'text': 'Title\nBody'}]}).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='anthropic-timeout-override-1',
            trace_id='anthropic-timeout-override-1',
            prompt='prompt text',
        ),
    )

    assert opener.timeouts == [240]


def test_resolve_context_window_tokens_prefix_match_claude() -> None:
    policy = ModelPolicy(model='claude-3-5-sonnet-20241022', metadata={})

    assert resolve_context_window_tokens(policy) == 200000


def test_resolve_context_window_tokens_prefix_match_gpt5() -> None:
    policy = ModelPolicy(model='gpt-5-mini', metadata={})

    assert resolve_context_window_tokens(policy) == 400000


def test_resolve_context_window_tokens_prefix_match_deepseek() -> None:
    policy = ModelPolicy(model='deepseek-chat', metadata={})

    assert resolve_context_window_tokens(policy) == 128000


def test_resolve_context_window_tokens_longest_prefix_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        services,
        '_MODEL_PREFIX_CONTEXT_WINDOWS',
        {'gpt-': 50000, 'gpt-4': 128000},
    )
    policy = ModelPolicy(model='gpt-4o-mini', metadata={})

    assert resolve_context_window_tokens(policy) == 128000


def test_resolve_context_window_tokens_metadata_override_wins() -> None:
    policy = ModelPolicy(model='claude-3-5-sonnet-20241022', metadata={'context_window_tokens': 999999})

    assert resolve_context_window_tokens(policy) == 999999


@pytest.mark.parametrize('garbage_override', [0, -5, 'not-a-number'])
def test_resolve_context_window_tokens_garbage_override_ignored(garbage_override: object) -> None:
    policy = ModelPolicy(
        model='claude-3-5-sonnet-20241022',
        metadata={'context_window_tokens': garbage_override},
    )

    assert resolve_context_window_tokens(policy) == 200000


def test_resolve_context_window_tokens_unknown_model_returns_none() -> None:
    policy = ModelPolicy(model='some-unknown-model', metadata={})

    assert resolve_context_window_tokens(policy) is None


def test_policy_supports_json_object_defaults_true_for_openai() -> None:
    policy = ModelPolicy(provider='openai', metadata={})

    assert policy_supports_json_object(policy) is True


def test_policy_supports_json_object_defaults_false_for_deepseek() -> None:
    policy = ModelPolicy(provider='deepseek', metadata={})

    assert policy_supports_json_object(policy) is False


def test_policy_supports_json_object_metadata_override_true_for_deepseek() -> None:
    policy = ModelPolicy(provider='deepseek', metadata={'json_mode': True})

    assert policy_supports_json_object(policy) is True


def test_policy_supports_json_object_metadata_override_false_for_openai() -> None:
    policy = ModelPolicy(provider='openai', metadata={'json_mode': False})

    assert policy_supports_json_object(policy) is False


def test_policy_supports_json_object_defaults_false_for_unknown_provider() -> None:
    policy = ModelPolicy(provider='mystery', metadata={})

    assert policy_supports_json_object(policy) is False


@pytest.mark.django_db
def test_openai_gateway_constructor_timeout_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    monkeypatch.setenv('ENGRAM_PROVIDER_HTTP_TIMEOUT', '180')
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': 'Title\nBody'}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener, timeout=9)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='ctor-timeout-1',
            trace_id='ctor-timeout-1',
            prompt='prompt text',
        ),
    )

    assert opener.timeouts == [9]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('response_kind', 'expected_max_tokens'),
    [
        ('single', 1024),
        ('candidates', 8192),
        ('curation_judgment', 1024),
        ('distill_extract.v1', 8192),
        ('distill_reduce.v1', 8192),
    ],
)
def test_openai_gateway_chat_payload_includes_max_tokens(response_kind: str, expected_max_tokens: int) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    if response_kind == 'single':
        content = 'Title\nBody'
    elif response_kind == 'distill_extract.v1':
        content = '{"memories": [], "no_signal_observation_ids": []}'
    else:
        content = '{"memories": []}'
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'max-tokens-{response_kind}',
            trace_id=f'max-tokens-{response_kind}',
            prompt='prompt text',
            response_kind=response_kind,
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == expected_max_tokens


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('response_kind', 'metadata_max_tokens', 'expected_max_tokens'),
    [
        ('candidates', 2048, 2048),
        ('distill_extract.v1', 2048, 8192),
        ('distill_extract.v1', 20000, 8192),
        ('distill_reduce.v1', 2048, 8192),
        ('distill_reduce.v1', 20000, 8192),
    ],
)
def test_openai_gateway_chat_payload_max_tokens_metadata_override_respected(
    response_kind: str,
    metadata_max_tokens: int,
    expected_max_tokens: int,
) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        metadata={'max_tokens': metadata_max_tokens},
    )
    content = (
        '{"memories": [], "no_signal_observation_ids": []}'
        if response_kind == 'distill_extract.v1'
        else '{"memories": []}'
    )
    opener = _opener_returning(json.dumps({'choices': [{'message': {'content': content}}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id=f'max-tokens-override-{response_kind}-{metadata_max_tokens}',
            trace_id=f'max-tokens-override-{response_kind}-{metadata_max_tokens}',
            prompt='prompt text',
            response_kind=response_kind,
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['max_tokens'] == expected_max_tokens


@pytest.mark.django_db
def test_openai_gateway_embed_payload_omits_max_tokens() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    opener = _opener_returning(json.dumps({'data': [{'embedding': [0.1, 0.2, 0.3]}]}).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='embed-no-max-tokens-1',
            trace_id='embed-no-max-tokens-1',
            text='text to embed',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert 'max_tokens' not in sent_body


@pytest.mark.django_db
def test_openai_gateway_call_http_400_creates_error_record_and_still_raises() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(urllib.error.HTTPError('url', 400, 'bad request', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='error-record-call-1',
        trace_id='error-record-call-1',
        prompt='PROMPT_MARKER_call_should_never_leak_into_error_metadata',
    )

    with pytest.raises(ModelPolicyError):
        gateway.call(data)

    records = ProviderCallRecord.objects.filter(request_id='error-record-call-1')
    assert records.count() == 1
    record = records.get()
    assert record.result == AuditResult.ERROR
    assert record.metadata['http_status'] == 400
    assert record.metadata['error_code'] == 'provider_http_error'
    assert 'PROMPT_MARKER_call_should_never_leak_into_error_metadata' not in json.dumps(record.metadata)


@pytest.mark.django_db
def test_openai_gateway_embed_http_400_creates_error_record_and_still_raises() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='embedding')
    opener = _opener_raising(urllib.error.HTTPError('url', 400, 'bad request', {}, None))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    data = EmbeddingCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='error-record-embed-1',
        trace_id='error-record-embed-1',
        text='EMBED_MARKER_should_never_leak_into_error_metadata',
    )

    with pytest.raises(ModelPolicyError):
        gateway.embed(data)

    records = ProviderCallRecord.objects.filter(request_id='error-record-embed-1')
    assert records.count() == 1
    record = records.get()
    assert record.result == AuditResult.ERROR
    assert record.metadata['http_status'] == 400
    assert record.metadata['error_code'] == 'provider_http_error'
    assert 'EMBED_MARKER_should_never_leak_into_error_metadata' not in json.dumps(record.metadata)


@pytest.mark.django_db
def test_anthropic_gateway_call_http_400_creates_error_record_and_still_raises() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic', base_url='https://api.z.ai/api/anthropic')
    opener = _opener_raising(urllib.error.HTTPError('url', 400, 'bad request', {}, None))
    gateway = AnthropicMessagesGateway(base_url='https://api.z.ai/api/anthropic', api_key='key', opener=opener)
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='anthropic-error-record-call-1',
        trace_id='anthropic-error-record-call-1',
        prompt='ANTHROPIC_PROMPT_MARKER_should_never_leak_into_error_metadata',
    )

    with pytest.raises(ModelPolicyError):
        gateway.call(data)

    records = ProviderCallRecord.objects.filter(request_id='anthropic-error-record-call-1')
    assert records.count() == 1
    record = records.get()
    assert record.result == AuditResult.ERROR
    assert record.metadata['http_status'] == 400
    assert record.metadata['error_code'] == 'provider_http_error'
    assert 'ANTHROPIC_PROMPT_MARKER_should_never_leak_into_error_metadata' not in json.dumps(record.metadata)


@pytest.mark.django_db
def test_openai_gateway_call_timeout_creates_error_record_with_no_http_status() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_raising(TimeoutError())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    data = ProviderCallInput(
        organization_id=organization.id,
        project_id=project.id,
        team_id=None,
        policy=policy,
        request_id='error-record-timeout-1',
        trace_id='error-record-timeout-1',
        prompt='prompt text',
    )

    with pytest.raises(ModelPolicyError):
        gateway.call(data)

    record = ProviderCallRecord.objects.get(request_id='error-record-timeout-1')
    assert record.result == AuditResult.ERROR
    assert record.metadata['http_status'] is None
    assert record.metadata['error_code'] == 'provider_timeout'


@pytest.mark.django_db
def test_openai_gateway_call_records_measured_latency_not_hardcoded_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {'choices': [{'message': {'content': 'Title\nBody'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    monotonic_values = iter([100.0, 100.25])
    monkeypatch.setattr('time.monotonic', lambda: next(monotonic_values))

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='latency-measured-1',
            trace_id='latency-measured-1',
            prompt='prompt text',
        ),
    )

    record = ProviderCallRecord.objects.get(id=result.call_record_id)
    assert record.latency_ms == 250
    assert record.latency_ms > 0


@pytest.mark.django_db
def test_openai_gateway_call_success_creates_single_recorded_record_no_error_record() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    completion = {'choices': [{'message': {'content': 'Memory title\nMemory body'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='regression-success-1',
            trace_id='regression-success-1',
            prompt='prompt text',
        ),
    )

    assert result.generated_title == 'Memory title'
    assert result.generated_body == 'Memory body'
    records = ProviderCallRecord.objects.filter(request_id='regression-success-1')
    assert records.count() == 1
    record = records.get()
    assert record.result == AuditResult.RECORDED
    assert not ProviderCallRecord.objects.filter(
        request_id='regression-success-1',
        result=AuditResult.ERROR,
    ).exists()


@pytest.mark.django_db
def test_openai_gateway_curation_decision_v1_uses_json_mode_and_fixed_budget() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, task_type='curation')
    body = {
        'choices': [
            {
                'message': {
                    'content': json.dumps(
                        {
                            'schema_version': 1,
                            'outcome': 'publish_new',
                            'relation': 'unrelated',
                            'target_memory_version_id': None,
                            'candidate_evidence_refs': ['candidate-ref'],
                            'comparisons': [],
                            'applicability': 'same',
                            'temporal_order': 'not_applicable',
                            'reason_code': 'distinct_claim',
                            'reason': 'distinct claim',
                        },
                    ),
                },
            },
        ],
    }
    opener = _opener_returning(json.dumps(body).encode())
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='curation-decision-openai-1',
            trace_id='curation-decision-openai-1',
            prompt='bounded judge envelope',
            response_kind='curation_decision_v1',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    assert sent_body['response_format'] == {'type': 'json_object'}
    assert sent_body['max_tokens'] == 16384
    assert json.loads(result.generated_body) == json.loads(body['choices'][0]['message']['content'])


@pytest.mark.django_db
def test_anthropic_gateway_curation_decision_v1_forces_closed_dedicated_tool() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='curation',
        provider='anthropic',
        base_url='https://api.anthropic.example',
    )
    response = {
        'content': [
            {
                'type': 'tool_use',
                'name': 'emit_curation_decision',
                'input': {
                    'schema_version': 1,
                    'outcome': 'publish_new',
                    'relation': 'unrelated',
                    'target_memory_version_id': None,
                    'candidate_evidence_refs': ['candidate-ref'],
                    'comparisons': [],
                    'applicability': 'same',
                    'temporal_order': 'not_applicable',
                    'reason_code': 'distinct_claim',
                    'reason': 'distinct claim',
                },
            },
        ],
    }
    opener = _opener_returning(json.dumps(response).encode())
    gateway = AnthropicMessagesGateway(base_url='https://api.anthropic.example', api_key='key', opener=opener)

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='curation-decision-anthropic-1',
            trace_id='curation-decision-anthropic-1',
            prompt='bounded judge envelope',
            response_kind='curation_decision_v1',
        ),
    )

    sent_body = json.loads(opener.requests[0].data)
    tool = sent_body['tools'][0]
    assert sent_body['max_tokens'] == 16384
    assert sent_body['tool_choice'] == {'type': 'tool', 'name': 'emit_curation_decision'}
    schema = tool['input_schema']
    expected_keys = {
        'schema_version',
        'outcome',
        'relation',
        'target_memory_version_id',
        'candidate_evidence_refs',
        'comparisons',
        'applicability',
        'temporal_order',
        'reason_code',
        'reason',
    }
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == expected_keys
    assert schema['properties']['candidate_evidence_refs']['maxItems'] == 16
    assert schema['properties']['comparisons']['maxItems'] == 12
    comparison_schema = schema['properties']['comparisons']['items']
    assert comparison_schema['additionalProperties'] is False
    assert set(comparison_schema['required']) == {
        'memory_version_id',
        'relation',
        'target_evidence_refs',
    }
    assert comparison_schema['properties']['target_evidence_refs']['maxItems'] == 16
    assert json.loads(result.generated_body) == response['content'][0]['input']
