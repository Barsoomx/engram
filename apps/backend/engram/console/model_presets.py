from __future__ import annotations

from typing import Any

from engram.model_policy.openai_request_shape import openai_policy_metadata

OPENAI_FLEX_PRESET_KEY = 'openai_all_flex'

# Flex prices per 1M tokens measured 2026-07-27: gpt-5-nano 0.025/0.20,
# gpt-5-mini 0.125/1.00, gpt-5.4-mini 0.375/2.25. The nano/mini pair keeps an
# all-OpenAI setup below the DeepSeek baseline it replaces.
_OPENAI_CHEAP_MODEL = 'gpt-5-nano'
_OPENAI_CURATION_MODEL = 'gpt-5-mini'
_OPENAI_EMBEDDING_MODEL = 'text-embedding-3-small'


def _task_model(
    task_type: str,
    provider: str,
    model: str,
    *,
    base_url: str = '',
    key_slot: str,
    flex: bool = False,
) -> dict[str, Any]:
    return {
        'task_type': task_type,
        'provider': provider,
        'model': model,
        'base_url': base_url,
        'key_slot': key_slot,
        'metadata': openai_policy_metadata(provider=provider, model=model, base_url=base_url, flex=flex),
    }


def _openai_task_models(*, flex: bool) -> list[dict[str, Any]]:
    return [
        _task_model('generation', 'openai', _OPENAI_CHEAP_MODEL, key_slot='openai', flex=flex),
        _task_model('curation', 'openai', _OPENAI_CURATION_MODEL, key_slot='openai', flex=flex),
        _task_model('digest', 'openai', _OPENAI_CHEAP_MODEL, key_slot='openai', flex=flex),
        _task_model('embedding', 'openai', _OPENAI_EMBEDDING_MODEL, key_slot='openai'),
    ]


PRESETS: list[dict[str, Any]] = [
    {
        'key': 'anthropic_openai',
        'name': 'Anthropic + OpenAI embeddings',
        'description': 'Anthropic for text generation and reasoning; OpenAI for embeddings.',
        'providers_needed': ['anthropic', 'openai'],
        'task_models': [
            _task_model('generation', 'anthropic', 'claude-haiku-4-5', key_slot='anthropic'),
            _task_model('curation', 'anthropic', 'claude-sonnet-5', key_slot='anthropic'),
            _task_model('digest', 'anthropic', 'claude-haiku-4-5', key_slot='anthropic'),
            _task_model('embedding', 'openai', _OPENAI_EMBEDDING_MODEL, key_slot='openai'),
        ],
    },
    {
        'key': 'openai_all',
        'name': 'OpenAI (all tasks)',
        'description': 'OpenAI for all tasks on the standard service tier.',
        'providers_needed': ['openai'],
        'task_models': _openai_task_models(flex=False),
    },
    {
        'key': OPENAI_FLEX_PRESET_KEY,
        'name': 'OpenAI (all tasks, flex tier)',
        'description': (
            'Same models as the OpenAI preset at roughly half the price on the flex service tier. '
            'Requests are queued provider-side, so they are slower and can be rejected when capacity '
            'is unavailable; the next attempt falls back to the standard tier.'
        ),
        'providers_needed': ['openai'],
        'task_models': _openai_task_models(flex=True),
    },
    {
        'key': 'deepseek_openai',
        'name': 'DeepSeek + OpenAI embeddings',
        'description': 'DeepSeek for text generation; OpenAI for embeddings.',
        'providers_needed': ['deepseek', 'openai'],
        'task_models': [
            _task_model('generation', 'deepseek', 'deepseek-v4-flash', key_slot='deepseek'),
            _task_model('curation', 'deepseek', 'deepseek-v4-pro', key_slot='deepseek'),
            _task_model('digest', 'deepseek', 'deepseek-v4-flash', key_slot='deepseek'),
            _task_model('embedding', 'openai', _OPENAI_EMBEDDING_MODEL, key_slot='openai'),
        ],
    },
    {
        'key': 'glm_openai',
        'name': 'GLM + OpenAI embeddings',
        'description': 'GLM (via OpenAI-compatible API) for text generation; OpenAI for embeddings.',
        'providers_needed': ['glm', 'openai'],
        'task_models': [
            _task_model(
                'generation',
                'openai',
                'glm-4.7-flash',
                base_url='https://api.z.ai/api/paas/v4',
                key_slot='glm',
            ),
            _task_model(
                'curation',
                'openai',
                'glm-4.7',
                base_url='https://api.z.ai/api/paas/v4',
                key_slot='glm',
            ),
            _task_model(
                'digest',
                'openai',
                'glm-4.7-flash',
                base_url='https://api.z.ai/api/paas/v4',
                key_slot='glm',
            ),
            _task_model('embedding', 'openai', _OPENAI_EMBEDDING_MODEL, key_slot='openai'),
        ],
    },
]

PRESET_BY_KEY: dict[str, dict] = {p['key']: p for p in PRESETS}

ALL_TASK_TYPES = ('generation', 'embedding', 'curation', 'digest')
