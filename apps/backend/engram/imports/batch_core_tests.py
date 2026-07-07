from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from engram.core.models import Memory, Observation, Organization, Project
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
def test_import_batch_row_replay_is_idempotent(f_core_scope: CoreScope) -> None:
    importer = ClaudeMemImporter()
    importer.import_batch(f_core_scope.context, 'sdk_sessions', [_session_row()])
    importer.import_batch(f_core_scope.context, 'observations', [_observation_row()], defer_embedding=True)

    replay = importer.import_batch(f_core_scope.context, 'observations', [_observation_row()], defer_embedding=True)

    assert replay.created == 0
    assert replay.duplicates == 1
    assert Memory.objects.filter(organization=f_core_scope.organization).count() == 1
