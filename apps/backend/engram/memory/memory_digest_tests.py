from __future__ import annotations

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
from engram.memory.services import DigestInput, GenerateDigest, MemoryWorkerError
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import ModelPolicyError


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
