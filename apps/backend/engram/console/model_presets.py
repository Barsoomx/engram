from __future__ import annotations

from typing import Any

PRESETS: list[dict[str, Any]] = [
    {
        'key': 'anthropic_openai',
        'name': 'Anthropic + OpenAI embeddings',
        'description': 'Anthropic for text generation and reasoning; OpenAI for embeddings.',
        'providers_needed': ['anthropic', 'openai'],
        'task_models': [
            {
                'task_type': 'generation',
                'provider': 'anthropic',
                'model': 'claude-3-5-sonnet-latest',
                'base_url': '',
                'key_slot': 'anthropic',
            },
            {
                'task_type': 'admin_assistant',
                'provider': 'anthropic',
                'model': 'claude-3-5-sonnet-latest',
                'base_url': '',
                'key_slot': 'anthropic',
            },
            {
                'task_type': 'curation',
                'provider': 'anthropic',
                'model': 'claude-3-5-haiku-latest',
                'base_url': '',
                'key_slot': 'anthropic',
            },
            {
                'task_type': 'digest',
                'provider': 'anthropic',
                'model': 'claude-3-5-haiku-latest',
                'base_url': '',
                'key_slot': 'anthropic',
            },
            {
                'task_type': 'rerank',
                'provider': 'anthropic',
                'model': 'claude-3-5-haiku-latest',
                'base_url': '',
                'key_slot': 'anthropic',
            },
            {
                'task_type': 'embedding',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'base_url': '',
                'key_slot': 'openai',
            },
        ],
    },
    {
        'key': 'openai_all',
        'name': 'OpenAI (all tasks)',
        'description': 'OpenAI for all tasks.',
        'providers_needed': ['openai'],
        'task_models': [
            {
                'task_type': 'generation',
                'provider': 'openai',
                'model': 'gpt-4o',
                'base_url': '',
                'key_slot': 'openai',
            },
            {
                'task_type': 'admin_assistant',
                'provider': 'openai',
                'model': 'gpt-4o',
                'base_url': '',
                'key_slot': 'openai',
            },
            {
                'task_type': 'curation',
                'provider': 'openai',
                'model': 'gpt-4o-mini',
                'base_url': '',
                'key_slot': 'openai',
            },
            {
                'task_type': 'digest',
                'provider': 'openai',
                'model': 'gpt-4o-mini',
                'base_url': '',
                'key_slot': 'openai',
            },
            {
                'task_type': 'rerank',
                'provider': 'openai',
                'model': 'gpt-4o-mini',
                'base_url': '',
                'key_slot': 'openai',
            },
            {
                'task_type': 'embedding',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'base_url': '',
                'key_slot': 'openai',
            },
        ],
    },
    {
        'key': 'deepseek_openai',
        'name': 'DeepSeek + OpenAI embeddings',
        'description': 'DeepSeek for text generation; OpenAI for embeddings.',
        'providers_needed': ['deepseek', 'openai'],
        'task_models': [
            {
                'task_type': 'generation',
                'provider': 'deepseek',
                'model': 'deepseek-chat',
                'base_url': '',
                'key_slot': 'deepseek',
            },
            {
                'task_type': 'admin_assistant',
                'provider': 'deepseek',
                'model': 'deepseek-chat',
                'base_url': '',
                'key_slot': 'deepseek',
            },
            {
                'task_type': 'curation',
                'provider': 'deepseek',
                'model': 'deepseek-chat',
                'base_url': '',
                'key_slot': 'deepseek',
            },
            {
                'task_type': 'digest',
                'provider': 'deepseek',
                'model': 'deepseek-chat',
                'base_url': '',
                'key_slot': 'deepseek',
            },
            {
                'task_type': 'rerank',
                'provider': 'deepseek',
                'model': 'deepseek-reasoner',
                'base_url': '',
                'key_slot': 'deepseek',
            },
            {
                'task_type': 'embedding',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'base_url': '',
                'key_slot': 'openai',
            },
        ],
    },
    {
        'key': 'glm_openai',
        'name': 'GLM + OpenAI embeddings',
        'description': 'GLM (via OpenAI-compatible API) for text generation; OpenAI for embeddings.',
        'providers_needed': ['glm', 'openai'],
        'task_models': [
            {
                'task_type': 'generation',
                'provider': 'openai',
                'model': 'glm-4-plus',
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'key_slot': 'glm',
            },
            {
                'task_type': 'admin_assistant',
                'provider': 'openai',
                'model': 'glm-4-plus',
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'key_slot': 'glm',
            },
            {
                'task_type': 'curation',
                'provider': 'openai',
                'model': 'glm-4-flash',
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'key_slot': 'glm',
            },
            {
                'task_type': 'digest',
                'provider': 'openai',
                'model': 'glm-4-flash',
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'key_slot': 'glm',
            },
            {
                'task_type': 'rerank',
                'provider': 'openai',
                'model': 'glm-4-flash',
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'key_slot': 'glm',
            },
            {
                'task_type': 'embedding',
                'provider': 'openai',
                'model': 'text-embedding-3-small',
                'base_url': '',
                'key_slot': 'openai',
            },
        ],
    },
]

PRESET_BY_KEY: dict[str, dict] = {p['key']: p for p in PRESETS}

ALL_TASK_TYPES = ('generation', 'embedding', 'curation', 'digest', 'rerank', 'admin_assistant')
