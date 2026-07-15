from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from engram.core.models import (
    AgentSession,
    AuditEvent,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryTransition,
    MemoryVersion,
    MemoryVersionSource,
    Observation,
    ObservationSource,
    Organization,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    WorkflowWork,
)
from engram.imports.services import ClaudeMemImporter, ImportContext


@dataclass(frozen=True)
class CoreScope:
    organization: Organization
    project: Project
    context: ImportContext


@pytest.fixture
def f_core_scope() -> CoreScope:
    organization = Organization.objects.create(name='Core Org', slug='core-org')
    project = Project.objects.create(
        organization=organization,
        name='Core Project',
        slug='core-project',
        repository_root='/workspace/example-repo',
    )
    context = ImportContext(source_store_id='core-store', organization=organization, project=project, team=None)

    return CoreScope(organization=organization, project=project, context=context)


def _session_row() -> dict[str, Any]:
    return {
        'id': 1,
        'content_session_id': 'content-core-001',
        'memory_session_id': 'memory-core-001',
        'project': '/workspace/example-repo',
        'platform_source': 'codex',
        'started_at': '2026-06-25T09:00:00Z',
        'completed_at': '2026-06-25T09:10:00Z',
        'status': 'completed',
        'prompt_counter': 1,
    }


def _observation_row(memory_session_id: str = 'memory-core-001') -> dict[str, Any]:
    return {
        'id': 1,
        'memory_session_id': memory_session_id,
        'project': '/workspace/example-repo',
        'text': 'A core observation body.',
        'type': 'discovery',
        'title': 'Core observation',
        'created_at': '2026-06-25T09:02:00Z',
    }


def _summary_row() -> dict[str, Any]:
    return {
        'id': 1,
        'memory_session_id': 'memory-core-001',
        'project': '/workspace/example-repo',
        'request': 'Summarize the core session.',
        'learned': 'Something worth remembering.',
        'created_at': '2026-06-25T09:08:00Z',
    }


@pytest.mark.django_db
def test_import_batch_resolves_sessions_committed_in_a_prior_batch(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()

    session_result = importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])
    observation_result = importer.import_batch(
        f_core_scope.context,
        'observations',
        [_observation_row()],
        defer_embedding=True,
    )

    assert session_result.created == 1
    assert observation_result.created == 1
    assert observation_result.skipped == 0
    assert Observation.objects.filter(organization=f_core_scope.organization).count() == 1


@pytest.mark.django_db
def test_import_batch_promotes_with_explicit_confidence(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])

    importer.import_batch(f_core_scope.context, 'observations', [_observation_row()], defer_embedding=True)
    importer.import_batch(f_core_scope.context, 'session_summaries', [_summary_row()], defer_embedding=True)

    observation_memory = Memory.objects.get(
        organization=f_core_scope.organization,
        metadata__event_type='claude_mem.observation',
    )
    summary_memory = Memory.objects.get(
        organization=f_core_scope.organization,
        metadata__event_type='claude_mem.session_summary',
    )

    assert observation_memory.confidence == Decimal('0.700')
    assert summary_memory.confidence == Decimal('0.800')
    assert observation_memory.metadata['source'] == 'claude_mem_import'
    assert observation_memory.metadata['source_store_id'] == 'core-store'
    assert observation_memory.metadata['event_type'] == 'claude_mem.observation'
    assert summary_memory.metadata['source'] == 'claude_mem_import'
    assert summary_memory.metadata['source_store_id'] == 'core-store'
    assert summary_memory.metadata['event_type'] == 'claude_mem.session_summary'


@pytest.mark.django_db
def test_import_batch_publishes_v1_provenance_without_candidate_decision_work(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])
    result = importer.import_batch(f_core_scope.context, 'observations', [_observation_row()])

    assert result.created == 1
    candidate = MemoryCandidate.objects.get(organization=f_core_scope.organization)
    candidate_source = MemoryCandidateSource.objects.get(candidate=candidate)
    assert candidate.decision_work_contract_version == 1
    assert candidate_source.source_kind == 'import'
    assert candidate_source.window_id is None
    assert candidate_source.stage_id is None
    observation_source = ObservationSource.objects.get(observation=candidate.source_observation_id)
    assert candidate_source.import_source_id == observation_source.id

    memory = Memory.objects.get(source_candidates=candidate)
    version = MemoryVersion.objects.get(memory=memory)
    assert version.version == 1
    assert MemoryVersionSource.objects.filter(memory_version=version, candidate_source=candidate_source).count() == 1
    document = RetrievalDocument.objects.get(memory_version=version)
    assert document.full_text == version.body
    transition = MemoryTransition.objects.get(candidate=candidate)
    assert transition.embedding_work_id is not None
    assert AuditEvent.objects.filter(memory_transition__candidate=candidate).count() == 1
    assert WorkflowWork.objects.filter(id=transition.embedding_work_id, work_type='memory_embedding').count() == 1
    assert WorkflowWork.objects.filter(work_type='candidate_decision', subject_id=candidate.id).count() == 0


@pytest.mark.django_db
def test_import_batch_promotion_fault_rolls_back_import_row_and_semantics(
    f_core_scope: CoreScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])

    class ImportPromotionError(RuntimeError):
        pass

    from engram.memory import transitions

    def fail_after_transition_write(point: str) -> None:
        if point == 'transition':
            raise ImportPromotionError('typed import promotion fault')

    monkeypatch.setattr(transitions, '_fault_boundary', fail_after_transition_write)
    with pytest.raises(ImportPromotionError):
        importer.import_batch(f_core_scope.context, 'observations', [_observation_row()])

    assert not RawEventEnvelope.objects.filter(organization=f_core_scope.organization).exists()
    assert not Observation.objects.filter(organization=f_core_scope.organization).exists()
    assert not ObservationSource.objects.filter(organization=f_core_scope.organization).exists()
    assert not MemoryCandidate.objects.filter(organization=f_core_scope.organization).exists()
    assert not Memory.objects.filter(organization=f_core_scope.organization).exists()
    assert not MemoryVersion.objects.filter(organization=f_core_scope.organization).exists()
    assert not MemoryTransition.objects.filter(organization=f_core_scope.organization).exists()


@pytest.mark.django_db
def test_import_batch_skips_observation_without_source_session(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()

    result = importer.import_batch(
        f_core_scope.context,
        'observations',
        [_observation_row(memory_session_id='missing-session')],
        defer_embedding=True,
    )

    assert result.created == 0
    assert result.skipped == 1
    assert not Memory.objects.filter(organization=f_core_scope.organization).exists()


@pytest.mark.django_db
def test_import_batch_caps_oversized_session_char_fields(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    row = _session_row()
    row['platform_source'] = 'p' * 200
    row['content_session_id'] = 'c' * 300
    row['memory_session_id'] = 'm' * 300

    result = importer.import_batch(f_core_scope.context, 'sdk_sessions', [row])

    assert result.created == 1
    session = AgentSession.objects.get(organization=f_core_scope.organization)
    assert session.platform_source == 'p' * 80
    assert session.content_session_id == 'c' * 255
    assert session.memory_session_id == 'm' * 255
    assert len(session.external_session_id) <= 255


@pytest.mark.django_db
def test_import_batch_resolves_memories_for_capped_session_ids(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    session = _session_row()
    session['memory_session_id'] = 'm' * 300
    observation = _observation_row(memory_session_id='m' * 300)

    importer.import_batch(f_core_scope.context, 'sdk_sessions', [session])
    result = importer.import_batch(f_core_scope.context, 'observations', [observation], defer_embedding=True)

    assert result.created == 1
    assert result.skipped == 0


@pytest.mark.django_db
def test_import_batch_skips_session_row_missing_content_session_id(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    row = _session_row()
    del row['content_session_id']

    result = importer.import_batch(f_core_scope.context, 'sdk_sessions', [row, _session_row()])

    assert result.created == 1
    assert result.skipped == 1
    unsupported = result.report['unsupported']
    assert len(unsupported) == 1
    assert unsupported[0]['source_type'] == 'sdk_sessions'
    assert unsupported[0]['reason'] == 'missing_content_session_id'
    assert AgentSession.objects.filter(organization=f_core_scope.organization).count() == 1


@pytest.mark.django_db
def test_import_batch_caps_observation_type_and_generated_model(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])
    row = _observation_row()
    row['type'] = 't' * 200
    row['generated_by_model'] = 'g' * 300

    result = importer.import_batch(f_core_scope.context, 'observations', [row], defer_embedding=True)

    assert result.created == 1
    observation = Observation.objects.get(organization=f_core_scope.organization)
    assert observation.observation_type == 't' * 80
    assert observation.generated_model == 'g' * 120


@pytest.mark.django_db
def test_import_batch_row_replay_is_idempotent(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])
    importer.import_batch(f_core_scope.context, 'observations', [_observation_row()], defer_embedding=True)

    replay = importer.import_batch(f_core_scope.context, 'observations', [_observation_row()], defer_embedding=True)

    assert replay.created == 0
    assert replay.duplicates == 1
    assert Memory.objects.filter(organization=f_core_scope.organization).count() == 1
    before = {
        'candidate_sources': MemoryCandidateSource.objects.count(),
        'version_sources': MemoryVersionSource.objects.count(),
        'transitions': MemoryTransition.objects.count(),
        'audits': AuditEvent.objects.count(),
        'documents': RetrievalDocument.objects.count(),
        'embedding_work': WorkflowWork.objects.filter(work_type='memory_embedding').count(),
    }
    replay_again = importer.import_batch(f_core_scope.context, 'observations', [_observation_row()])
    after = {
        'candidate_sources': MemoryCandidateSource.objects.count(),
        'version_sources': MemoryVersionSource.objects.count(),
        'transitions': MemoryTransition.objects.count(),
        'audits': AuditEvent.objects.count(),
        'documents': RetrievalDocument.objects.count(),
        'embedding_work': WorkflowWork.objects.filter(work_type='memory_embedding').count(),
    }
    assert replay_again.created == 0
    assert replay_again.duplicates == 1
    assert after == before
