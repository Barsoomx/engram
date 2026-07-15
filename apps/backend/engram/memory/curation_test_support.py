from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from engram.core.models import (
    Memory,
    MemoryCandidate,
    Organization,
    OrganizationSettings,
    Project,
    RetrievalDocument,
    Team,
)
from engram.memory.transitions_test_support import (
    candidate_in_scope,
    provenanced_candidate,
    transition_request,
    transitions_module,
)
from engram.model_policy.models import ModelPolicy, ProviderSecret, ProviderSecretEnvelope
from engram.model_policy.services import FakeProviderGateway, ProviderCallInput, ProviderCallResult

_LONG_BODY = 'The retrieval pipeline ranks documents by cosine similarity over embeddings.'


def set_curator_settings(
    organization: Organization,
    *,
    enabled: bool = True,
    threshold: str = '0.850',
    llm_judge_enabled: bool = False,
) -> None:
    OrganizationSettings.objects.update_or_create(
        organization=organization,
        defaults={
            'curator_enabled': enabled,
            'near_dup_threshold': Decimal(threshold),
            'curator_llm_judge_enabled': llm_judge_enabled,
        },
    )


def create_curation_policy(
    organization: Organization,
    team: Team,
    project: Project,
    *,
    task_type: str = 'curation',
) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=team,
        name=f'Team {task_type} Anthropic',
        provider='anthropic',
        scope='team',
        current_version=1,
    )
    ProviderSecretEnvelope.objects.create(
        organization=organization,
        team=team,
        secret=secret,
        version=1,
        key_version='v1',
        ciphertext='encrypted-curation-secret',
        hmac_digest='curation-hmac',
        active=True,
    )

    return ModelPolicy.objects.create(
        organization=organization,
        team=team,
        project=project,
        name=f'{task_type} policy',
        scope='project',
        task_type=task_type,
        provider='anthropic',
        model='claude-judge',
        secret=secret,
        version=1,
    )


def seed_atomic_existing_and_duplicate(
    suffix: str,
) -> tuple[Organization, Team, Project, Memory, MemoryCandidate]:
    source_candidate, source, (organization, project, session) = provenanced_candidate(suffix)
    existing = transitions_module().PromoteMemoryCandidate().execute(transition_request(source_candidate)).memory
    duplicate, _duplicate_source = candidate_in_scope(
        source_candidate,
        source,
        title=f'Atomic duplicate {suffix}',
        body=_LONG_BODY,
    )

    return organization, session.team, project, existing, duplicate


def patch_atomic_near_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    existing: Memory,
    *,
    score: float,
) -> None:
    document = RetrievalDocument.objects.get(memory=existing)
    monkeypatch.setattr('engram.memory.curation.embed_candidate', lambda _candidate: [1.0])
    monkeypatch.setattr(
        'engram.memory.curation.find_near_duplicate',
        lambda *_args, **_kwargs: (document, score),
    )


class JudgeGatewayStub(FakeProviderGateway):
    def __init__(self, body: str) -> None:
        self._body = body

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        return ProviderCallResult(
            provider=data.policy.provider,
            model=data.policy.model,
            call_record_id=uuid.uuid4(),
            redaction_state='clean',
            generated_title='',
            generated_body=self._body,
        )


def patch_judge_gateway(monkeypatch: pytest.MonkeyPatch, gateway: FakeProviderGateway) -> None:
    monkeypatch.setattr('engram.memory.curation.get_provider_gateway', lambda *_, **__: gateway)
