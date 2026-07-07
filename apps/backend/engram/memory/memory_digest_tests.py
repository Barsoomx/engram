from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from engram.context.context_api_tests import create_project_scope
from engram.core.models import (
    AuditEvent,
    AuditResult,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.services import (
    DigestInput,
    GenerateDigest,
    MemoryWorkerError,
    digest_content_hash,
    digest_prompt,
    run_daily_digest_with_tracking,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.real_provider_tests import _opener_returning, make_real_policy
from engram.model_policy.services import FakeProviderGateway, ModelPolicyError, ProviderCallInput, ProviderCallResult


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


def create_digest_policy(organization: Organization, team: Team | None, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='Org Digest OpenAI',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=None,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-digest-secret',
        hmac_digest='digest-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='Digest policy',
        scope='project',
        task_type='digest',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


def create_digest_generation_policy(organization: Organization, project: Project) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=None,
        name='Org Generation OpenAI',
        provider='openai',
        scope='organization',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=None,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-generation-secret',
        hmac_digest='generation-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=None,
        project=project,
        name='Generation policy',
        scope='project',
        task_type='generation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


def create_source_memory(organization: Organization, team: Team | None, project: Project, *, title: str) -> Memory:
    return Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title,
        body=f'{title} body detail.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )


@pytest.mark.django_db
def test_generate_digest_creates_memory_from_sources() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    first = create_source_memory(organization, team, project, title='First source')
    second = create_source_memory(organization, team, project, title='Second source')

    result = GenerateDigest().execute(
        DigestInput(project_id=project.id, memory_ids=(second.id, first.id), request_id='digest-1'),
    )

    assert result.memory.metadata['kind'] == 'digest'
    assert result.memory.metadata['digest_kind'] == 'daily_structured'
    assert result.memory.metadata['source_memory_ids'] == [str(first.id), str(second.id)]
    assert result.memory.title.startswith('Digest ')
    assert result.memory.body
    assert result.memory.visibility_scope == VisibilityScope.PROJECT
    assert MemoryVersion.objects.filter(memory=result.memory).count() == 1
    assert RetrievalDocument.objects.filter(memory=result.memory).count() == 1
    provider_call = ProviderCallRecord.objects.get(id=result.provider_call_id)
    assert provider_call.task_type == 'digest'
    assert AuditEvent.objects.filter(event_type='DigestGenerated', target_id=str(result.memory.id)).exists()


@pytest.mark.django_db
def test_generate_digest_raises_for_no_approved_sources() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)

    with pytest.raises(MemoryWorkerError, match='no approved source memories'):
        GenerateDigest().execute(
            DigestInput(project_id=project.id, memory_ids=(uuid.uuid4(),), request_id='digest-empty'),
        )


@pytest.mark.django_db
def test_generate_digest_raises_for_missing_policy() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    source = create_source_memory(organization, team, project, title='Source')

    with pytest.raises(ModelPolicyError):
        GenerateDigest().execute(
            DigestInput(project_id=project.id, memory_ids=(source.id,), request_id='digest-no-policy'),
        )


@pytest.mark.django_db
def test_generate_digest_wraps_provider_failure_as_worker_error() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    policy = create_digest_policy(organization, team, project)
    ProviderSecretEnvelope.objects.filter(secret=policy.secret).delete()
    source = create_source_memory(organization, team, project, title='Source')

    with pytest.raises(MemoryWorkerError, match='digest provider unavailable'):
        GenerateDigest().execute(
            DigestInput(project_id=project.id, memory_ids=(source.id,), request_id='digest-fail'),
        )
    assert Memory.objects.filter(metadata__kind='digest').count() == 0


@pytest.mark.django_db
def test_generate_digest_is_idempotent_on_replay() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    source = create_source_memory(organization, team, project, title='Idempotent source')
    digest_input = DigestInput(
        project_id=project.id,
        memory_ids=(source.id,),
        request_id='digest-idem-1',
    )

    result1 = GenerateDigest().execute(digest_input)
    result2 = GenerateDigest().execute(digest_input)

    assert Memory.objects.filter(metadata__kind='digest', project=project).count() == 1
    assert result2.memory.id == result1.memory.id
    assert result2.memory_version.id == result1.memory_version.id
    assert result2.retrieval_document.id == result1.retrieval_document.id


@pytest.mark.django_db
def test_legacy_orphan_without_version_or_document_is_not_reused_or_reduplicated() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    source = create_source_memory(organization, team, project, title='Orphan-regression source')
    digest_input = DigestInput(
        project_id=project.id,
        memory_ids=(source.id,),
        request_id='digest-orphan-regress',
    )

    seed_result = GenerateDigest().execute(digest_input)
    orphan_id = seed_result.memory.id
    MemoryVersion.objects.filter(memory_id=orphan_id).delete()

    result1 = GenerateDigest().execute(digest_input)
    result2 = GenerateDigest().execute(digest_input)

    assert result1.memory.id == result2.memory.id
    assert result1.memory.id != orphan_id
    assert Memory.objects.filter(metadata__kind='digest', project=project).count() == 2
    assert MemoryVersion.objects.filter(memory=result1.memory).count() == 1
    assert RetrievalDocument.objects.filter(memory=result1.memory).count() == 1


@pytest.mark.django_db
def test_generate_digest_makes_real_call_when_provider_record_exists_without_memory(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    policy = make_real_policy(
        organization,
        project,
        task_type='digest',
        base_url='https://provider.example/v1',
        raw_key='real-key',
    )
    source = create_source_memory(organization, team, project, title='Orphan record source')
    m_monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')
    completion = {'choices': [{'message': {'content': 'Digest title\nFresh synthesized digest body.'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    m_monkeypatch.setattr('urllib.request.urlopen', opener)
    request_id = 'digest-orphan-1'
    content_hash = digest_content_hash(project.id, (source.id,))
    ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=None,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type='digest',
        policy_version=policy.version,
        request_id=f'{request_id}:{content_hash}',
        trace_id=request_id,
        redaction_state='clean',
        token_usage={'input_tokens': 1, 'output_tokens': 0},
        cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
        metadata={'prompt_retained': False},
    )

    result = GenerateDigest().execute(
        DigestInput(project_id=project.id, memory_ids=(source.id,), request_id=request_id),
    )

    assert len(opener.requests) == 1
    assert result.memory.body == 'Fresh synthesized digest body.'
    assert 'Orphan record source' not in result.memory.body
    assert ProviderCallRecord.objects.filter(request_id=f'{request_id}:{content_hash}').count() == 2


@pytest.mark.django_db
def test_run_daily_digest_rerun_with_real_gateway_does_not_replay_stale_provider_call(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    make_real_policy(
        organization,
        project,
        task_type='digest',
        base_url='https://provider.example/v1',
        raw_key='real-key',
    )
    first_source = create_source_memory(organization, team, project, title='First day source')
    second_source = create_source_memory(organization, team, project, title='Second day source')
    m_monkeypatch.setenv('ENGRAM_PROVIDER_MODE', 'real')
    completion = {'choices': [{'message': {'content': 'Digest title\nSynthesized digest body from provider.'}}]}
    opener = _opener_returning(json.dumps(completion).encode())
    m_monkeypatch.setattr('urllib.request.urlopen', opener)
    request_id = f'daily-digest:{project.id}'

    first = run_daily_digest_with_tracking(
        organization_id=organization.id,
        project_id=project.id,
        memory_ids=(first_source.id,),
        request_id=request_id,
        correlation_id=request_id,
    )
    second = run_daily_digest_with_tracking(
        organization_id=organization.id,
        project_id=project.id,
        memory_ids=(second_source.id,),
        request_id=request_id,
        correlation_id=request_id,
    )

    assert len(opener.requests) == 2
    assert second.provider_call_id != first.provider_call_id
    assert second.memory.id != first.memory.id
    assert second.memory.body == 'Synthesized digest body from provider.'
    assert 'Second day source' not in second.memory.body


class _DigestRaisingGateway:
    def __init__(self, error: ModelPolicyError) -> None:
        self._error = error

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        raise self._error


def digest_policy_routed_gateway(error: ModelPolicyError) -> object:
    def stub_get_provider_gateway(policy: ModelPolicy, **_kwargs: object) -> object:
        if policy.task_type == 'digest':
            return _DigestRaisingGateway(error)

        return FakeProviderGateway()

    return stub_get_provider_gateway


@pytest.mark.django_db
def test_generate_digest_falls_back_when_policy_call_fails(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    digest_policy = create_digest_policy(organization, team, project)
    digest_policy.fallback_enabled = True
    digest_policy.save(update_fields=['fallback_enabled'])
    create_digest_generation_policy(organization, project)
    source = create_source_memory(organization, team, project, title='Fallback source')
    error = ModelPolicyError('provider_http_error', 'provider returned 400', retryable=False, http_status=400)
    m_monkeypatch.setattr('engram.memory.services.get_provider_gateway', digest_policy_routed_gateway(error))

    result = GenerateDigest().execute(
        DigestInput(project_id=project.id, memory_ids=(source.id,), request_id='digest-fallback'),
    )

    assert result.memory.metadata['kind'] == 'digest'
    audit = AuditEvent.objects.get(event_type='ProviderFallbackUsed')
    assert audit.metadata['task_type'] == 'digest'
    assert audit.metadata['error_code'] == 'provider_http_error'
    assert audit.metadata['primary_policy_id'] == str(digest_policy.id)


@pytest.mark.django_db
def test_generate_digest_does_not_fall_back_when_disabled(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    create_digest_generation_policy(organization, project)
    source = create_source_memory(organization, team, project, title='No fallback source')
    error = ModelPolicyError('provider_http_error', 'provider returned 400', retryable=False, http_status=400)
    m_monkeypatch.setattr('engram.memory.services.get_provider_gateway', digest_policy_routed_gateway(error))

    with pytest.raises(MemoryWorkerError, match='digest provider unavailable'):
        GenerateDigest().execute(
            DigestInput(project_id=project.id, memory_ids=(source.id,), request_id='digest-no-fallback'),
        )

    assert Memory.objects.filter(metadata__kind='digest').count() == 0
    assert not AuditEvent.objects.filter(event_type='ProviderFallbackUsed').exists()


class _PromptSource:
    def __init__(self, title: str, body: str) -> None:
        self.title = title
        self.body = body


def test_digest_prompt_truncates_long_source_line() -> None:
    source = _PromptSource(title='Ttl', body='x' * 500)

    prompt = digest_prompt((source,), cap=60)

    assert len(prompt) <= 60
    assert '[truncated' in prompt


def test_digest_prompt_keeps_short_source_line_intact() -> None:
    source = _PromptSource(title='Short', body='body')

    prompt = digest_prompt((source,), cap=1000)

    assert prompt == '- Short: body'
    assert '[truncated' not in prompt


@pytest.mark.django_db
def test_generate_digest_caps_sources_and_audits_truncation(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    m_monkeypatch.setenv('ENGRAM_DIGEST_MAX_SOURCES', '2')
    recent = create_source_memory(organization, team, project, title='AAA recent')
    middle = create_source_memory(organization, team, project, title='BBB middle')
    oldest = create_source_memory(organization, team, project, title='CCC oldest')
    now = timezone.now()
    Memory.objects.filter(id=recent.id).update(updated_at=now)
    Memory.objects.filter(id=middle.id).update(updated_at=now - timedelta(hours=1))
    Memory.objects.filter(id=oldest.id).update(updated_at=now - timedelta(hours=2))

    result = GenerateDigest().execute(
        DigestInput(
            project_id=project.id,
            memory_ids=(recent.id, middle.id, oldest.id),
            request_id='digest-cap',
        ),
    )

    assert result.memory.metadata['source_memory_ids'] == [str(recent.id), str(middle.id)]
    assert str(oldest.id) not in result.memory.metadata['source_memory_ids']
    audit = AuditEvent.objects.get(event_type='DigestSourcesTruncated', target_id=str(project.id))
    assert audit.metadata['total_sources'] == 3
    assert audit.metadata['sources_used'] == 2


@pytest.mark.django_db
def test_generate_digest_does_not_audit_truncation_within_cap(m_monkeypatch: pytest.MonkeyPatch) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    m_monkeypatch.setenv('ENGRAM_DIGEST_MAX_SOURCES', '10')
    source = create_source_memory(organization, team, project, title='Only source')

    GenerateDigest().execute(
        DigestInput(project_id=project.id, memory_ids=(source.id,), request_id='digest-no-cap'),
    )

    assert not AuditEvent.objects.filter(event_type='DigestSourcesTruncated').exists()


@pytest.mark.django_db
def test_daily_digest_non_retryable_failure_audits_and_marks_run_failed(
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, _owner, _api_key = create_project_scope()
    create_digest_policy(organization, team, project)
    source = create_source_memory(organization, team, project, title='Failing source')
    error = ModelPolicyError('provider_payment_required', 'provider returned 402', retryable=False, http_status=402)
    m_monkeypatch.setattr('engram.memory.services.get_provider_gateway', digest_policy_routed_gateway(error))
    request_id = f'daily-digest:{project.id}'

    with pytest.raises(MemoryWorkerError):
        run_daily_digest_with_tracking(
            organization_id=organization.id,
            project_id=project.id,
            memory_ids=(source.id,),
            request_id=request_id,
            correlation_id=request_id,
        )

    run = WorkflowRun.objects.get(project=project, run_type=WorkflowRunType.DAILY_DIGEST)
    assert run.status == WorkflowRunStatus.FAILED
    audit = AuditEvent.objects.get(event_type='DigestFailed', target_id=str(project.id))
    assert audit.result == AuditResult.ERROR
    assert audit.metadata['digest_kind'] == 'daily_structured'
