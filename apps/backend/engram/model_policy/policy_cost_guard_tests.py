from __future__ import annotations

import pytest

from engram.console.model_presets import PRESET_BY_KEY
from engram.core.provider_timeouts import FLEX_TIMEOUT_SIZING_ENV
from engram.model_policy.models import ModelPolicy
from engram.model_policy.services import _resolve_policy_pricing
from engram.model_policy.validation import FLEX_SIZING_ERROR_CODE, policy_metadata_error

# Spend can only be audited if every preset-created policy carries pricing; a policy without it
# records cost_usd 0.0000 and the money becomes invisible.

_OPENAI_PRESETS = ('openai_all', 'openai_all_flex')


@pytest.mark.parametrize('preset_key', _OPENAI_PRESETS)
def test_openai_presets_carry_pricing_for_every_task(preset_key: str) -> None:
    preset = PRESET_BY_KEY[preset_key]

    for task_model in preset['task_models']:
        pricing = (task_model.get('metadata') or {}).get('pricing')

        assert pricing, f'{preset_key}/{task_model["task_type"]} has no pricing'
        assert pricing.get('input_per_mtok'), f'{preset_key}/{task_model["task_type"]} missing input price'


@pytest.mark.parametrize('preset_key', _OPENAI_PRESETS)
def test_preset_pricing_is_readable_by_the_cost_recorder(preset_key: str) -> None:
    preset = PRESET_BY_KEY[preset_key]

    for task_model in preset['task_models']:
        policy = ModelPolicy(
            provider=task_model['provider'],
            model=task_model['model'],
            metadata=task_model['metadata'],
        )

        assert _resolve_policy_pricing(policy) is not None, f'{preset_key}/{task_model["task_type"]}'


def test_flex_policy_is_rejected_when_the_deployment_is_not_sized_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FLEX_TIMEOUT_SIZING_ENV, raising=False)
    policy = ModelPolicy(
        provider='openai',
        model='gpt-5-mini',
        task_type='curation',
        metadata=PRESET_BY_KEY['openai_all_flex']['task_models'][1]['metadata'],
    )

    assert policy_metadata_error(policy) == FLEX_SIZING_ERROR_CODE


def test_flex_policy_is_accepted_once_the_deployment_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLEX_TIMEOUT_SIZING_ENV, '1')
    policy = ModelPolicy(
        provider='openai',
        model='gpt-5-mini',
        task_type='curation',
        metadata=PRESET_BY_KEY['openai_all_flex']['task_models'][1]['metadata'],
    )

    assert policy_metadata_error(policy) is None


def test_standard_tier_policy_needs_no_flex_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLEX_TIMEOUT_SIZING_ENV, raising=False)
    policy = ModelPolicy(
        provider='openai',
        model='gpt-5-mini',
        task_type='curation',
        metadata=PRESET_BY_KEY['openai_all']['task_models'][1]['metadata'],
    )

    assert policy_metadata_error(policy) is None
