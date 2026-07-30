from __future__ import annotations

from engram.console.model_presets import ALL_TASK_TYPES, OPENAI_FLEX_PRESET_KEY, PRESET_BY_KEY, PRESETS

EXPECTED_GENERATION_MODEL = {
    'anthropic_openai': 'claude-haiku-4-5',
    'openai_all': 'gpt-5-nano',
    'openai_all_flex': 'gpt-5-nano',
    'deepseek_openai': 'deepseek-v4-flash',
    'glm_openai': 'glm-4.7-flash',
}

EXPECTED_CURATION_MODEL = {
    'anthropic_openai': 'claude-sonnet-5',
    'openai_all': 'gpt-5-mini',
    'openai_all_flex': 'gpt-5-mini',
    'deepseek_openai': 'deepseek-v4-pro',
    'glm_openai': 'glm-4.7',
}

# Flex prices per 1M tokens measured 2026-07-27: gpt-5-nano 0.025/0.20,
# gpt-5-mini 0.125/1.00, gpt-5.4-mini 0.375/2.25. Anything above the mini tier
# costs multiples of the DeepSeek setup the preset replaces.
AFFORDABLE_OPENAI_MODELS = frozenset({'gpt-5-nano', 'gpt-5-mini', 'text-embedding-3-small'})

OPENAI_PRESET_KEYS = ('openai_all', 'openai_all_flex')
CHAT_TASK_TYPES = ('generation', 'curation', 'digest')


def _task_model(preset: dict, task_type: str) -> dict:
    return next(tm for tm in preset['task_models'] if tm['task_type'] == task_type)


def test_all_task_types_has_exactly_four_real_types() -> None:
    assert ALL_TASK_TYPES == ('generation', 'embedding', 'curation', 'digest')


def test_no_preset_contains_rerank_or_admin_assistant_task_model() -> None:
    for preset in PRESETS:
        task_types = {tm['task_type'] for tm in preset['task_models']}
        assert 'rerank' not in task_types, f'preset {preset["key"]} still has rerank'
        assert 'admin_assistant' not in task_types, f'preset {preset["key"]} still has admin_assistant'


def test_every_preset_is_covered_by_the_expected_model_maps() -> None:
    keys = {preset['key'] for preset in PRESETS}

    assert keys == set(EXPECTED_GENERATION_MODEL)
    assert keys == set(EXPECTED_CURATION_MODEL)


def test_generation_uses_cheap_model_tier() -> None:
    for key, expected_model in EXPECTED_GENERATION_MODEL.items():
        preset = PRESET_BY_KEY[key]
        generation = _task_model(preset, 'generation')
        assert generation['model'] == expected_model, (
            f'preset {key} generation model is {generation["model"]!r}, expected {expected_model!r}'
        )


def test_curation_uses_premium_model_tier() -> None:
    for key, expected_model in EXPECTED_CURATION_MODEL.items():
        preset = PRESET_BY_KEY[key]
        curation = _task_model(preset, 'curation')
        assert curation['model'] == expected_model, (
            f'preset {key} curation model is {curation["model"]!r}, expected {expected_model!r}'
        )


def test_curation_model_differs_from_cheap_digest_tier() -> None:
    for preset in PRESETS:
        curation = _task_model(preset, 'curation')
        digest = _task_model(preset, 'digest')
        assert curation['model'] != digest['model'], (
            f'preset {preset["key"]} curation model {curation["model"]!r} '
            f'must use the premium tier, not the cheap digest tier {digest["model"]!r}'
        )


def test_generation_provider_and_key_slot_unchanged() -> None:
    expected = {
        'anthropic_openai': ('anthropic', '', 'anthropic'),
        'openai_all': ('openai', '', 'openai'),
        'openai_all_flex': ('openai', '', 'openai'),
        'deepseek_openai': ('deepseek', '', 'deepseek'),
        'glm_openai': ('openai', 'https://api.z.ai/api/paas/v4', 'glm'),
    }
    for key, (provider, base_url, key_slot) in expected.items():
        generation = _task_model(PRESET_BY_KEY[key], 'generation')
        assert generation['provider'] == provider
        assert generation['base_url'] == base_url
        assert generation['key_slot'] == key_slot


def test_curation_provider_and_key_slot_unchanged() -> None:
    expected = {
        'anthropic_openai': ('anthropic', '', 'anthropic'),
        'openai_all': ('openai', '', 'openai'),
        'openai_all_flex': ('openai', '', 'openai'),
        'deepseek_openai': ('deepseek', '', 'deepseek'),
        'glm_openai': ('openai', 'https://api.z.ai/api/paas/v4', 'glm'),
    }
    for key, (provider, base_url, key_slot) in expected.items():
        curation = _task_model(PRESET_BY_KEY[key], 'curation')
        assert curation['provider'] == provider
        assert curation['base_url'] == base_url
        assert curation['key_slot'] == key_slot


def test_openai_presets_stay_inside_the_affordable_model_tier() -> None:
    for key in OPENAI_PRESET_KEYS:
        for tm in PRESET_BY_KEY[key]['task_models']:
            assert tm['model'] in AFFORDABLE_OPENAI_MODELS, (
                f'preset {key} names {tm["model"]!r}, which is outside the measured affordable tier'
            )


def test_openai_chat_policies_ship_the_reasoning_request_shape() -> None:
    for key in OPENAI_PRESET_KEYS:
        for task_type in CHAT_TASK_TYPES:
            shape = _task_model(PRESET_BY_KEY[key], task_type)['metadata']['request_shape']
            assert shape['completion_tokens_param'] == 'max_completion_tokens'
            assert shape['temperature'] is None
            assert shape['reasoning_effort']['curation_decision_v2'] == 'minimal'


def test_only_the_flex_preset_requests_the_flex_tier() -> None:
    assert OPENAI_FLEX_PRESET_KEY == 'openai_all_flex'
    for preset in PRESETS:
        wants_flex = preset['key'] == OPENAI_FLEX_PRESET_KEY
        for tm in preset['task_models']:
            tier = (tm['metadata'].get('service_tier') or {}).get('tier')
            if wants_flex and tm['task_type'] in CHAT_TASK_TYPES:
                assert tier == 'flex', f'preset {preset["key"]} {tm["task_type"]} lost its flex tier'
            else:
                assert tier is None, f'preset {preset["key"]} {tm["task_type"]} must not request a service tier'


def test_embedding_and_non_openai_task_models_carry_no_request_shape() -> None:
    for preset in PRESETS:
        for tm in preset['task_models']:
            is_openai_chat = preset['key'] in OPENAI_PRESET_KEYS and tm['task_type'] in CHAT_TASK_TYPES
            if is_openai_chat:
                continue
            shaping = {key: value for key, value in tm['metadata'].items() if key != 'pricing'}
            assert shaping == {}, f'preset {preset["key"]} {tm["task_type"]} must not carry provider request shaping'
