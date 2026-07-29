from __future__ import annotations

import pytest

from engram.model_policy.models import ModelPolicy
from engram.model_policy.openai_request_shape import (
    FLEX_ATTEMPT_BUDGET,
    REASONING_COMPLETION_TOKENS_PARAM,
    REASONING_EFFORT_BY_RESPONSE_KIND,
    openai_model_family,
    openai_policy_metadata,
    policy_openai_family,
    policy_requests_flex_tier,
    requires_completion_tokens_param,
    supports_service_tier_flex,
)

GLM_BASE_URL = 'https://api.z.ai/api/paas/v4'


@pytest.mark.parametrize(
    'model',
    ['gpt-5-nano', 'gpt-5-mini', 'gpt-5.4', 'gpt-5.4-mini', 'GPT-5-Nano'],
)
def test_gpt5_models_resolve_to_the_gpt5_family(model: str) -> None:
    assert openai_model_family('openai', model) == 'gpt-5'


def test_reasoning_families_are_recognised_separately() -> None:
    assert openai_model_family('openai', 'o4-mini') == 'o4-mini'
    assert openai_model_family('openai', 'o3-mini') == 'o3'
    assert openai_model_family('openai', 'o1') == 'o1'


@pytest.mark.parametrize(
    'model',
    ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'text-embedding-3-small'],
)
def test_chat_completion_models_have_no_reasoning_family(model: str) -> None:
    assert openai_model_family('openai', model) == ''
    assert requires_completion_tokens_param('openai', model) is False
    assert supports_service_tier_flex('openai', model) is False


def test_other_providers_never_get_an_openai_family() -> None:
    assert openai_model_family('deepseek', 'deepseek-v4-pro') == ''
    assert openai_model_family('anthropic', 'claude-sonnet-5') == ''


def test_openai_compatible_third_party_base_url_is_not_openai() -> None:
    assert openai_model_family('openai', 'glm-4.7', GLM_BASE_URL) == ''
    assert openai_model_family('openai', 'gpt-5-nano', GLM_BASE_URL) == ''


def test_explicit_openai_base_url_still_counts_as_openai() -> None:
    assert openai_model_family('openai', 'gpt-5-nano', 'https://api.openai.com/v1') == 'gpt-5'


def test_flex_is_offered_on_gpt5_o3_and_o4_mini_only() -> None:
    assert supports_service_tier_flex('openai', 'gpt-5-mini') is True
    assert supports_service_tier_flex('openai', 'o3-mini') is True
    assert supports_service_tier_flex('openai', 'o4-mini') is True
    assert supports_service_tier_flex('openai', 'o1') is False
    assert supports_service_tier_flex('deepseek', 'deepseek-v4-pro') is False


def test_gpt5_metadata_matches_the_proven_wire_shape() -> None:
    metadata = openai_policy_metadata(provider='openai', model='gpt-5-nano')

    assert metadata['request_shape']['completion_tokens_param'] == REASONING_COMPLETION_TOKENS_PARAM
    assert metadata['request_shape']['temperature'] is None
    assert metadata['request_shape']['reasoning_effort'] == REASONING_EFFORT_BY_RESPONSE_KIND
    assert 'service_tier' not in metadata


def test_reasoning_effort_covers_every_production_response_kind() -> None:
    expected_kinds = {
        'candidates',
        'curation_decision_v1',
        'curation_judgment',
        'distill_extract.v1',
        'distill_reduce.v2',
        'single',
    }

    assert set(REASONING_EFFORT_BY_RESPONSE_KIND) == expected_kinds
    assert set(REASONING_EFFORT_BY_RESPONSE_KIND.values()) == {'minimal'}


def test_flex_opt_in_adds_the_service_tier_block() -> None:
    metadata = openai_policy_metadata(provider='openai', model='gpt-5-mini', flex=True)

    assert metadata['service_tier'] == {'tier': 'flex', 'attempt_budget': FLEX_ATTEMPT_BUDGET}


def test_flex_opt_in_is_dropped_for_families_that_reject_it() -> None:
    assert openai_policy_metadata(provider='openai', model='gpt-4o', flex=True) == {}
    assert 'service_tier' not in openai_policy_metadata(provider='openai', model='o1', flex=True)
    assert openai_policy_metadata(provider='deepseek', model='deepseek-v4-pro', flex=True) == {}


def test_minimal_reasoning_effort_is_only_sent_to_the_gpt5_family() -> None:
    metadata = openai_policy_metadata(provider='openai', model='o3-mini')

    assert metadata['request_shape']['completion_tokens_param'] == REASONING_COMPLETION_TOKENS_PARAM
    assert 'reasoning_effort' not in metadata['request_shape']


def test_non_reasoning_models_get_no_metadata_at_all() -> None:
    assert openai_policy_metadata(provider='openai', model='text-embedding-3-small') == {}
    assert openai_policy_metadata(provider='openai', model='glm-4.7', base_url=GLM_BASE_URL) == {}


def test_policy_family_reads_the_base_url_from_metadata() -> None:
    openai_policy = ModelPolicy(provider='openai', model='gpt-5-nano', metadata={})
    proxied_policy = ModelPolicy(provider='openai', model='gpt-5-nano', metadata={'base_url': GLM_BASE_URL})

    assert policy_openai_family(openai_policy) == 'gpt-5'
    assert policy_openai_family(proxied_policy) == ''


def test_policy_requests_flex_tier_reads_the_service_tier_block() -> None:
    flex_policy = ModelPolicy(provider='openai', model='gpt-4o', metadata={'service_tier': {'tier': 'flex'}})
    auto_policy = ModelPolicy(provider='openai', model='gpt-4o', metadata={'service_tier': {'tier': 'auto'}})

    assert policy_requests_flex_tier(flex_policy) is True
    assert policy_requests_flex_tier(auto_policy) is False
    assert policy_requests_flex_tier(ModelPolicy(provider='openai', model='gpt-4o', metadata={})) is False
