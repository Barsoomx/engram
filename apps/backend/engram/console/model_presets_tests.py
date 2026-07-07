from __future__ import annotations

from engram.console.model_presets import ALL_TASK_TYPES, PRESET_BY_KEY, PRESETS

EXPECTED_GENERATION_MODEL = {
    'anthropic_openai': 'claude-haiku-4-5',
    'openai_all': 'gpt-5.4-mini',
    'deepseek_openai': 'deepseek-v4-flash',
    'glm_openai': 'glm-4.7-flash',
}

EXPECTED_CURATION_MODEL = {
    'anthropic_openai': 'claude-sonnet-5',
    'openai_all': 'gpt-5.4',
    'deepseek_openai': 'deepseek-v4-pro',
    'glm_openai': 'glm-4.7',
}


def _task_model(preset: dict, task_type: str) -> dict:
    return next(tm for tm in preset['task_models'] if tm['task_type'] == task_type)


def test_all_task_types_has_exactly_four_real_types() -> None:
    assert ALL_TASK_TYPES == ('generation', 'embedding', 'curation', 'digest')


def test_no_preset_contains_rerank_or_admin_assistant_task_model() -> None:
    for preset in PRESETS:
        task_types = {tm['task_type'] for tm in preset['task_models']}
        assert 'rerank' not in task_types, f'preset {preset["key"]} still has rerank'
        assert 'admin_assistant' not in task_types, f'preset {preset["key"]} still has admin_assistant'


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
        'deepseek_openai': ('deepseek', '', 'deepseek'),
        'glm_openai': ('openai', 'https://api.z.ai/api/paas/v4', 'glm'),
    }
    for key, (provider, base_url, key_slot) in expected.items():
        curation = _task_model(PRESET_BY_KEY[key], 'curation')
        assert curation['provider'] == provider
        assert curation['base_url'] == base_url
        assert curation['key_slot'] == key_slot
