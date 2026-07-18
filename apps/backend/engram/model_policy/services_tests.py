from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest
from structlog.testing import capture_logs

from engram.context.context_api_tests import create_project_scope
from engram.core.models import AuditEvent
from engram.memory.curation_judge import _ALLOWED_COMBINATIONS
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.models import ProviderCallRecord
from engram.model_policy.real_provider_tests import _opener_raising, _opener_returning, make_real_policy
from engram.model_policy.services import (
    _ANTHROPIC_STRUCTURED_TOOLS,
    AnthropicMessagesGateway,
    CreateProviderSecret,
    EmbeddingCallInput,
    OpenAICompatibleGateway,
    ProviderCallInput,
    ProviderSecretInput,
    RotateProviderSecret,
    RotateProviderSecretInput,
    UpdateModelPolicy,
    UpdateModelPolicyInput,
    _split_completion,
    curation_schema_prompt_prefix,
    generated_candidates_payload,
    secret_fingerprint,
)

PLAINTEXT_PROVIDER_SECRET = 'provider-plaintext-value-abc123'


def _openai_chat_body(content: str, usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _anthropic_message_body(content: str, usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'content': [{'type': 'text', 'text': content}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _openai_embedding_body(embedding: list[float], usage: dict[str, int] | None = None) -> bytes:
    response: dict[str, Any] = {'data': [{'embedding': embedding}]}
    if usage is not None:
        response['usage'] = usage

    return json.dumps(response).encode()


def _openai_call(policy: Any, prompt: str, body: bytes) -> ProviderCallRecord:
    gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=_opener_returning(body),
    )
    result = gateway.call(
        ProviderCallInput(
            organization_id=policy.organization_id,
            project_id=policy.project_id,
            team_id=None,
            policy=policy,
            request_id='cost-call-1',
            trace_id='cost-call-1',
            prompt=prompt,
        ),
    )

    return ProviderCallRecord.objects.get(id=result.call_record_id)


@pytest.mark.django_db
def test_openai_gateway_records_provider_token_usage_when_usage_present() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 120, 'completion_tokens': 45, 'total_tokens': 165},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.token_usage == {
        'input_tokens': 120,
        'output_tokens': 45,
        'total_tokens': 165,
        'source': 'provider',
    }


@pytest.mark.django_db
def test_openai_gateway_falls_back_to_estimated_token_usage_without_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    content = 'Title: Memory\nBody: one two'
    body = _openai_chat_body(content)

    record = _openai_call(policy, 'alpha beta gamma', body)

    assert record.token_usage == {
        'input_tokens': 3,
        'output_tokens': len(content.split()),
        'source': 'estimated',
    }


@pytest.mark.django_db
def test_openai_gateway_computes_policy_cost_when_pricing_and_usage_present() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': '0.28', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 1_000_000, 'completion_tokens': 1_000_000, 'total_tokens': 2_000_000},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': False,
        'cost_usd': '0.700000',
        'pricing_source': 'policy',
    }


@pytest.mark.django_db
def test_openai_gateway_marks_no_usage_cost_when_pricing_but_no_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': '0.28', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body('Title: Memory\nBody: line one')

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'no_usage',
    }


@pytest.mark.django_db
def test_openai_gateway_marks_unknown_cost_without_pricing() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    )

    record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'unknown',
    }


@pytest.mark.django_db
def test_openai_gateway_ignores_malformed_pricing_and_still_records() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        metadata={'pricing': {'input_per_mtok': 'not-a-number', 'output_per_mtok': '0.42'}},
    )
    body = _openai_chat_body(
        'Title: Memory\nBody: line one',
        usage={'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    )

    with capture_logs() as logs:
        record = _openai_call(policy, 'a prompt', body)

    assert record.cost_metadata == {
        'estimated': True,
        'cost_usd': '0.0000',
        'pricing_source': 'unknown',
    }
    assert any(entry.get('event') == 'provider_pricing_malformed' for entry in logs)


@pytest.mark.django_db
def test_anthropic_gateway_records_provider_token_usage() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, provider='anthropic')
    body = _anthropic_message_body(
        'Title: Memory\nBody: line one',
        usage={'input_tokens': 30, 'output_tokens': 12},
    )
    gateway = AnthropicMessagesGateway(
        base_url='https://api.anthropic.com',
        api_key='key',
        opener=_opener_returning(body),
    )

    result = gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='cost-call-anthropic-1',
            trace_id='cost-call-anthropic-1',
            prompt='a prompt',
        ),
    )
    record = ProviderCallRecord.objects.get(id=result.call_record_id)

    assert record.token_usage == {
        'input_tokens': 30,
        'output_tokens': 12,
        'total_tokens': 42,
        'source': 'provider',
    }


@pytest.mark.django_db
def test_openai_gateway_embed_records_usage_and_input_only_cost() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='embedding',
        metadata={'pricing': {'input_per_mtok': '0.02'}},
    )
    body = _openai_embedding_body(
        [0.1, 0.2, 0.3],
        usage={'prompt_tokens': 1_000_000, 'total_tokens': 1_000_000},
    )
    gateway = OpenAICompatibleGateway(
        base_url='https://provider.example/v1',
        api_key='key',
        opener=_opener_returning(body),
    )

    result = gateway.embed(
        EmbeddingCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='cost-embed-1',
            trace_id='cost-embed-1',
            text='text to embed',
        ),
    )
    record = ProviderCallRecord.objects.get(id=result.call_record_id)

    assert record.token_usage == {
        'input_tokens': 1_000_000,
        'output_tokens': 0,
        'total_tokens': 1_000_000,
        'source': 'provider',
    }
    assert record.cost_metadata == {
        'estimated': False,
        'cost_usd': '0.020000',
        'pricing_source': 'policy',
    }


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
def test_openai_curation_decision_prompt_carries_verdict_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('{}'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='curation-schema-1',
            trace_id='curation-schema-1',
            prompt='{"schema":"curation_judge_input.v1"}',
            response_kind='curation_decision_v1',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    user_message = sent['messages'][-1]['content']

    assert 'exactly one JSON object' in user_message
    assert 'candidate_evidence_refs' in user_message
    assert 'supersede_memory' in user_message
    assert 'temporal_order' in user_message
    assert user_message.rstrip().endswith('{"schema":"curation_judge_input.v1"}')


@pytest.mark.django_db
def test_openai_distill_extract_prompt_carries_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('{}'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='distill-extract-schema-1',
            trace_id='distill-extract-schema-1',
            prompt='Observation: 6f1d0b3c-1f6c-4a4a-9f61-2f2a1c9d4b77',
            response_kind='distill_extract.v1',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    user_message = sent['messages'][-1]['content']

    assert 'memories' in user_message
    assert 'no_signal_observation_ids' in user_message
    assert 'supporting_observation_ids' in user_message
    assert 'confidence' in user_message
    assert user_message.rstrip().endswith('Observation: 6f1d0b3c-1f6c-4a4a-9f61-2f2a1c9d4b77')


@pytest.mark.django_db
def test_openai_distill_reduce_prompt_carries_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('{}'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='distill-reduce-schema-1',
            trace_id='distill-reduce-schema-1',
            prompt='{"drafts":[]}',
            response_kind='distill_reduce.v1',
        ),
    )

    sent = json.loads(opener.requests[0].data)
    user_message = sent['messages'][-1]['content']

    assert 'memories' in user_message
    assert 'source_ids' in user_message
    assert 'confidence' in user_message
    assert user_message.rstrip().endswith('{"drafts":[]}')


def test_distill_extract_schema_prefix_states_parser_enforced_rules() -> None:
    instructions = curation_schema_prompt_prefix('distill_extract.v1')

    assert instructions == (
        'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
        'The object must contain exactly these keys and no additional properties: '
        'memories (array of at most 8 objects); '
        'no_signal_observation_ids (array of observation ids, unique, may be empty). '
        'Each memories entry must contain exactly these keys and no additional properties: '
        'title (non-blank string, at most 255 characters); '
        'body (non-blank string, at most 2000 characters); '
        'confidence (a JSON number between 0 and 1, never a string); '
        'supporting_observation_ids (non-empty array of unique observation ids); '
        'kind (optional, one of: decision, convention, gotcha, architecture, incident). '
        'Only use observation ids copied verbatim from the input observations. '
        'Every input observation id must appear at least once across the memories supporting_observation_ids '
        'and no_signal_observation_ids: none may be omitted, and no id may appear in both. '
        'The same observation id may support more than one memory.'
    )


def test_distill_reduce_schema_prefix_states_parser_enforced_rules() -> None:
    instructions = curation_schema_prompt_prefix('distill_reduce.v1')

    assert instructions == (
        'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
        'The object must contain exactly the key memories (array of objects) and no additional properties. '
        'Each memories entry must contain exactly these keys and no additional properties: '
        'title (non-blank string, at most 255 characters); '
        'body (non-blank string, at most 3000 characters); '
        'confidence (a JSON number between 0 and 1); '
        'source_ids (non-empty array of unique draft ids); '
        'kind (optional, one of: decision, convention, gotcha, architecture, incident). '
        'Only use draft ids copied verbatim from the input drafts. '
        'Every input draft id must appear in the source_ids of at least one memories entry: none may be omitted. '
        'Return at most reduction_target memories, as given by the reduction_target key of the input object, '
        'and when more than one draft is given return strictly fewer memories than the number of input drafts.'
    )


def test_curation_decision_schema_prefix_states_allowed_combination_table() -> None:
    instructions = curation_schema_prompt_prefix('curation_decision_v1')

    assert instructions == (
        'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
        'The object must contain exactly these keys and no additional properties (recursively): '
        'schema_version (integer, always 1); '
        'outcome (one of: publish_new, merge_evidence, revise_memory, supersede_memory, reject_candidate, '
        'open_conflict); '
        'relation (one of: unrelated, compatible_distinct, equivalent, candidate_revises, candidate_supersedes, '
        'redundant, unsupported, mutually_incompatible); '
        'target_memory_version_id (a shortlist memory_version_id string, or null); '
        'candidate_evidence_refs (array of provided evidence reference tokens, unique, at most 16); '
        'comparisons (array with exactly one object per shortlist entry, in the given order; each object has '
        'memory_version_id, relation, and target_evidence_refs); '
        'applicability (one of: same, different); '
        'temporal_order (one of: candidate_newer, target_newer, unordered, not_applicable); '
        'reason_code (one of: distinct_claim, equivalent_claim, same_subject_revision, ordered_replacement, '
        'redundant_claim, unsupported_claim, same_scope_contradiction); '
        'reason (a short redacted explanation, at most 500 characters). '
        'Only reference memory_version_id values and evidence tokens present in the input. '
        'A non-null target_memory_version_id must be one of the shortlist entries, and its comparison relation '
        'must equal the top-level relation.'
        ' Allowed outcome and relation combinations (any other combination is invalid): '
        'publish_new with relation unrelated or compatible_distinct, target_memory_version_id null, '
        'candidate evidence tier supported or corroborated and comparison_complete true; '
        'merge_evidence with relation equivalent, a non-null target and both candidate and target evidence tiers '
        'supported or corroborated; '
        'revise_memory with relation candidate_revises, a non-null target, candidate evidence tier corroborated, '
        'target evidence tier supported or corroborated and temporal_order candidate_newer used only when the '
        'candidate evidence is clearly newer, which the system verifies; '
        'supersede_memory with relation candidate_supersedes, a non-null target, candidate evidence tier '
        'corroborated, target evidence tier supported or corroborated, temporal_order candidate_newer used only '
        'when the candidate evidence is clearly newer, which the system verifies, and comparison_complete true; '
        'reject_candidate with relation redundant, a non-null target and target evidence tier supported or '
        'corroborated; '
        'reject_candidate with relation unsupported, target null and the candidate having no supporting evidence, '
        'evidence tier none; '
        'open_conflict with relation mutually_incompatible, a non-null target, temporal_order unordered, both '
        'candidate and target evidence tiers supported or corroborated, non-empty evidence refs on both sides, '
        'comparison_complete true and applicability same. '
        'The top-level relation describes the selected target; with a null target use unrelated, '
        'compatible_distinct or unsupported. '
        'merge_evidence, revise_memory and supersede_memory additionally require applicability same. '
        'When comparison_complete is true, every shortlist comparison is unrelated or compatible_distinct and the '
        'candidate evidence tier is supported or corroborated, choose publish_new with target null. '
        'When no combination satisfies its requirements, choose the reject_candidate form that matches the '
        'candidate evidence.'
    )


def test_curation_decision_instructions_align_with_allowed_combinations() -> None:
    instructions = curation_schema_prompt_prefix('curation_decision_v1')
    marker = 'combinations (any other combination is invalid): '

    assert marker in instructions

    enumeration = instructions.split(marker, 1)[1].split('. The top-level relation describes', 1)[0]
    clauses = [clause.strip() for clause in enumeration.split(';')]
    gate_tokens = {
        ('publish_new', 'unrelated'): ('supported or corroborated', 'comparison_complete true'),
        ('publish_new', 'compatible_distinct'): ('supported or corroborated', 'comparison_complete true'),
        ('merge_evidence', 'equivalent'): ('candidate and target evidence tiers supported or corroborated',),
        ('revise_memory', 'candidate_revises'): (
            'candidate evidence tier corroborated',
            'target evidence tier supported or corroborated',
            'candidate_newer',
        ),
        ('supersede_memory', 'candidate_supersedes'): (
            'candidate evidence tier corroborated',
            'target evidence tier supported or corroborated',
            'comparison_complete true',
        ),
        ('reject_candidate', 'redundant'): ('target evidence tier supported or corroborated',),
        ('reject_candidate', 'unsupported'): ('no supporting evidence', 'evidence tier none'),
        ('open_conflict', 'mutually_incompatible'): (
            'supported or corroborated',
            'evidence refs on both sides',
            'comparison_complete true',
            'applicability same',
        ),
    }
    for outcome, relation in _ALLOWED_COMBINATIONS:
        matches = [clause for clause in clauses if outcome in clause and relation in clause]

        assert matches, (outcome, relation)

        clause = matches[0]
        for token in gate_tokens[(outcome, relation)]:
            assert token in clause, (outcome, relation, token)


@pytest.mark.django_db
def test_openai_single_kind_prompt_has_no_curation_schema_instructions() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)
    opener = _opener_returning(_openai_chat_body('Title: X\nBody: Y'))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)

    gateway.call(
        ProviderCallInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy=policy,
            request_id='single-schema-1',
            trace_id='single-schema-1',
            prompt='plain prompt',
        ),
    )

    sent = json.loads(opener.requests[0].data)

    assert sent['messages'][-1]['content'] == 'plain prompt'


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
def test_update_model_policy_clear_context_window_tokens_removes_override_keeps_other_keys() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='clear-context-window-1',
            actor_id='actor-1',
            clear_context_window_tokens=True,
        ),
    )

    assert 'context_window_tokens' not in updated.metadata
    assert updated.metadata.get('base_url') == 'https://provider.example/v1'
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_clear_context_window_tokens_is_noop_when_absent() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project)

    assert 'context_window_tokens' not in policy.metadata

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='clear-context-window-2',
            actor_id='actor-1',
            clear_context_window_tokens=True,
        ),
    )

    assert 'context_window_tokens' not in updated.metadata
    assert updated.metadata == {'base_url': 'https://provider.example/v1'}
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_omitted_context_window_tokens_preserves_override() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='omit-context-window-1',
            actor_id='actor-1',
        ),
    )

    assert updated.metadata.get('context_window_tokens') == 8000
    assert updated.version == 2


@pytest.mark.django_db
def test_update_model_policy_sets_context_window_tokens_when_not_clearing() -> None:
    organization, _team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(organization, project, metadata={'context_window_tokens': 8000})

    updated = UpdateModelPolicy().execute(
        UpdateModelPolicyInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            policy_id=policy.id,
            request_id='set-context-window-1',
            actor_id='actor-1',
            context_window_tokens=32000,
        ),
    )

    assert updated.metadata.get('context_window_tokens') == 32000
    assert updated.version == 2


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


def test_curation_judgment_tool_schema_decision_enum_includes_contradicts() -> None:
    decision_schema = _ANTHROPIC_STRUCTURED_TOOLS['curation_judgment']['input_schema']['properties']['decision']

    assert 'contradicts' in decision_schema['enum']


def test_emit_memories_tool_schema_declares_kind_enum() -> None:
    memory_schema = _ANTHROPIC_STRUCTURED_TOOLS['candidates']['input_schema']['properties']['memories']['items']
    kind_schema = memory_schema['properties']['kind']

    assert kind_schema['enum'] == ['decision', 'convention', 'gotcha', 'architecture', 'incident']


def test_distill_extract_tool_schema_matches_provider_contract() -> None:
    assert 'distill_extract.v1' in _ANTHROPIC_STRUCTURED_TOOLS
    tool = _ANTHROPIC_STRUCTURED_TOOLS['distill_extract.v1']

    assert tool['name'] == 'emit_distillation_extraction'
    schema = tool['input_schema']
    assert schema['type'] == 'object'
    assert set(schema['properties']) == {'memories', 'no_signal_observation_ids'}
    assert set(schema['required']) == {'memories', 'no_signal_observation_ids'}
    assert schema['additionalProperties'] is False

    memories = schema['properties']['memories']
    assert memories['type'] == 'array'
    assert memories['maxItems'] == 8
    memory = memories['items']
    assert memory['type'] == 'object'
    assert memory['additionalProperties'] is False
    assert set(memory['required']) == {'title', 'body', 'confidence', 'supporting_observation_ids'}
    assert set(memory['properties']) == {
        'title',
        'body',
        'confidence',
        'supporting_observation_ids',
        'kind',
    }
    assert memory['properties']['title'] == {'type': 'string', 'minLength': 1, 'maxLength': 255}
    assert memory['properties']['body'] == {'type': 'string', 'minLength': 1, 'maxLength': 2000}
    assert memory['properties']['confidence'] == {'type': 'number', 'minimum': 0, 'maximum': 1}
    assert memory['properties']['supporting_observation_ids'] == {
        'type': 'array',
        'items': {'type': 'string'},
        'minItems': 1,
        'uniqueItems': True,
    }
    assert schema['properties']['no_signal_observation_ids'] == {
        'type': 'array',
        'items': {'type': 'string'},
        'uniqueItems': True,
    }
    assert memory['properties']['kind']['enum'] == [
        'decision',
        'convention',
        'gotcha',
        'architecture',
        'incident',
    ]


def test_generated_candidates_payload_first_memory_carries_kind() -> None:
    payload = json.loads(generated_candidates_payload('a prompt'))

    memories = payload['memories']
    assert memories[0]['kind'] == 'gotcha'
    assert 'kind' not in memories[1]


@pytest.mark.django_db
def test_create_provider_secret_audit_stores_fingerprint_not_cleartext() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    secret = CreateProviderSecret().execute(
        ProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            name='Team OpenAI',
            provider='openai',
            scope='team',
            raw_secret=PLAINTEXT_PROVIDER_SECRET,
            request_id='req-create-audit',
            actor_id=str(owner.id),
        ),
    )
    event = AuditEvent.objects.get(target_id=str(secret.id), event_type='ProviderSecretCreated')

    assert 'raw_secret' not in event.metadata
    assert event.metadata['fingerprint'] == secret_fingerprint(PLAINTEXT_PROVIDER_SECRET)
    assert PLAINTEXT_PROVIDER_SECRET not in json.dumps(event.metadata)


@pytest.mark.django_db
def test_rotate_provider_secret_audit_stores_fingerprint_not_cleartext() -> None:
    organization, team, project, owner, _api_key = create_project_scope()
    secret = CreateProviderSecret().execute(
        ProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            name='Team OpenAI',
            provider='openai',
            scope='team',
            raw_secret=PLAINTEXT_PROVIDER_SECRET,
            request_id='req-create-audit-2',
            actor_id=str(owner.id),
        ),
    )
    rotated = 'rotated-plaintext-value-xyz789'
    RotateProviderSecret().execute(
        RotateProviderSecretInput(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
            secret_id=secret.id,
            raw_secret=rotated,
            request_id='req-rotate-audit',
            actor_id=str(owner.id),
            allowed_team_ids=(team.id,),
        ),
    )
    event = AuditEvent.objects.get(target_id=str(secret.id), event_type='ProviderSecretRotated')

    assert 'raw_secret' not in event.metadata
    assert event.metadata['fingerprint'] == secret_fingerprint(rotated)
    assert rotated not in json.dumps(event.metadata)
