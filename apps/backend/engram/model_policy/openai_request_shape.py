from __future__ import annotations

from typing import Any

from engram.core.provider_service_tier import SERVICE_TIER_FLEX, SERVICE_TIER_METADATA_KEY
from engram.model_policy.models import ModelPolicy

OPENAI_PROVIDER = 'openai'
REASONING_COMPLETION_TOKENS_PARAM = 'max_completion_tokens'
FLEX_ATTEMPT_BUDGET = 2

# 'minimal' keeps these models at the behaviour the non-reasoning providers
# already deliver on the same prompts, and it is the only effort measured live
# to spend zero reasoning tokens (gpt-5-nano 2026-07-27: minimal 0, low 192,
# medium 832). Raising it is a per-policy metadata edit.
REASONING_EFFORT_BY_RESPONSE_KIND: dict[str, str] = {
    'candidates': 'minimal',
    'curation_decision_v1': 'minimal',
    'curation_judgment': 'minimal',
    'distill_extract.v1': 'minimal',
    'distill_reduce.v2': 'minimal',
    'single': 'minimal',
}

# Published USD per 1M tokens, captured 2026-07-27, as (standard, flex) pairs. Cost recording is
# blind without these: a policy with no pricing records cost_usd 0.0000 and the spend disappears.
_OPENAI_PRICING: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    'gpt-5-nano': (
        {'input_per_mtok': '0.05', 'output_per_mtok': '0.40'},
        {'input_per_mtok': '0.025', 'output_per_mtok': '0.20'},
    ),
    'gpt-5-mini': (
        {'input_per_mtok': '0.25', 'output_per_mtok': '2.00'},
        {'input_per_mtok': '0.125', 'output_per_mtok': '1.00'},
    ),
    'gpt-5.4-nano': (
        {'input_per_mtok': '0.20', 'output_per_mtok': '1.25'},
        {'input_per_mtok': '0.10', 'output_per_mtok': '0.625'},
    ),
    'gpt-5.4-mini': (
        {'input_per_mtok': '0.75', 'output_per_mtok': '4.50'},
        {'input_per_mtok': '0.375', 'output_per_mtok': '2.25'},
    ),
    'text-embedding-3-small': (
        {'input_per_mtok': '0.02'},
        {'input_per_mtok': '0.02'},
    ),
    'text-embedding-3-large': (
        {'input_per_mtok': '0.13'},
        {'input_per_mtok': '0.13'},
    ),
}

_REASONING_FAMILIES = ('gpt-5', 'o4-mini', 'o3', 'o1')
_FLEX_FAMILIES = frozenset({'gpt-5', 'o4-mini', 'o3'})
_MINIMAL_EFFORT_FAMILIES = frozenset({'gpt-5'})
_OPENAI_API_HOST_PREFIX = 'https://api.openai.com'


def openai_model_family(provider: str, model: str, base_url: str = '') -> str:
    if provider != OPENAI_PROVIDER or not _is_openai_api(base_url):
        return ''

    normalized = str(model or '').strip().lower()
    for family in _REASONING_FAMILIES:
        if normalized.startswith(family):
            return family

    return ''


def requires_completion_tokens_param(provider: str, model: str, base_url: str = '') -> bool:
    return bool(openai_model_family(provider, model, base_url))


def supports_service_tier_flex(provider: str, model: str, base_url: str = '') -> bool:
    return openai_model_family(provider, model, base_url) in _FLEX_FAMILIES


def openai_model_pricing(model: str, *, flex: bool = False) -> dict[str, str] | None:
    normalized = str(model or '').strip().lower()
    prices = _OPENAI_PRICING.get(normalized)
    if prices is None:
        return None

    standard, flex_prices = prices

    return dict(flex_prices if flex else standard)


def openai_policy_metadata(*, provider: str, model: str, base_url: str = '', flex: bool = False) -> dict[str, Any]:
    if provider != OPENAI_PROVIDER or not _is_openai_api(base_url):
        return {}

    family = openai_model_family(provider, model, base_url)
    metadata: dict[str, Any] = {}
    use_flex = flex and family in _FLEX_FAMILIES

    if family:
        request_shape: dict[str, Any] = {
            'completion_tokens_param': REASONING_COMPLETION_TOKENS_PARAM,
            'temperature': None,
        }
        if family in _MINIMAL_EFFORT_FAMILIES:
            request_shape['reasoning_effort'] = dict(REASONING_EFFORT_BY_RESPONSE_KIND)
        metadata['request_shape'] = request_shape

    if use_flex:
        metadata[SERVICE_TIER_METADATA_KEY] = {'tier': SERVICE_TIER_FLEX, 'attempt_budget': FLEX_ATTEMPT_BUDGET}

    pricing = openai_model_pricing(model, flex=use_flex)
    if pricing is not None:
        metadata['pricing'] = pricing

    return metadata


def policy_openai_family(policy: ModelPolicy) -> str:
    return openai_model_family(policy.provider, policy.model, _policy_base_url(policy))


def policy_supports_service_tier_flex(policy: ModelPolicy) -> bool:
    return supports_service_tier_flex(policy.provider, policy.model, _policy_base_url(policy))


def policy_requests_flex_tier(policy: ModelPolicy) -> bool:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    config = metadata.get(SERVICE_TIER_METADATA_KEY)
    if not isinstance(config, dict):
        return False

    return config.get('tier') == SERVICE_TIER_FLEX


def _policy_base_url(policy: ModelPolicy) -> str:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}

    return str(metadata.get('base_url') or '')


def _is_openai_api(base_url: str) -> bool:
    normalized = str(base_url or '').strip().lower()

    return not normalized or normalized.startswith(_OPENAI_API_HOST_PREFIX)
