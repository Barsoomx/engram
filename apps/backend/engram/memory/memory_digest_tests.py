from __future__ import annotations

import json
import uuid

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
)
from engram.memory.services import DigestInput, GenerateDigest, MemoryWorkerError, run_daily_digest_with_tracking
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.real_provider_tests import _opener_returning, make_real_policy
from engram.model_policy.services import ModelPolicyError


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
