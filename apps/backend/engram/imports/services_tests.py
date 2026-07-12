from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Lock, local
from uuid import UUID

import pytest
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django_celery_outbox.models import CeleryOutbox

import engram.imports.services as import_services
from engram.core.models import (
    Agent,
    AgentSession,
    Memory,
    MemoryCandidate,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    RawEventNormalizationDisposition,
    RawEventNormalizationReason,
    Runtime,
    Team,
    WorkflowWork,
)
from engram.imports.services import (
    _MAX_OBSERVATION_LIST_ITEMS,
    _MAX_OBSERVATION_TEXT_CHARS,
    ClaudeMemImporter,
    ClaudeMemImportInput,
    ImportContext,
)


@dataclass(frozen=True)
class ImportScope:
    organization: Organization
    project: Project
    team: Team


@pytest.fixture
def f_import_scope() -> ImportScope:
    organization = Organization.objects.create(name='Services Fixture Org', slug='services-fixture-org')
    project = Project.objects.create(
        organization=organization,
        name='Services Fixture Project',
        slug='services-fixture-project',
        repository_root='/workspace/example-repo',
    )
    team = Team.objects.create(organization=organization, name='Services Fixture Team', slug='services-fixture-team')
    ProjectTeam.objects.create(organization=organization, project=project, team=team)

    return ImportScope(organization=organization, project=project, team=team)


@pytest.fixture
def f_claude_mem_fixture(tmp_path: Path) -> Path:
    source_fixture = Path(__file__).parent / 'fixtures' / 'claude_mem_minimal'
    source_root = tmp_path / 'claude_mem_source'
    source_root.mkdir()

    db_path = source_root / 'claude-mem.db'
    sql_path = source_fixture / 'claude_mem_minimal.sql'
    with sqlite3.connect(db_path) as connection:
        connection.executescript(sql_path.read_text())

    return source_root


def _import_context_and_observation(
    f_import_scope: ImportScope,
) -> tuple[ClaudeMemImporter, ImportContext, AgentSession, dict[str, object]]:
    importer = ClaudeMemImporter()
    context = ImportContext(
        source_store_id='fixture-store',
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
    )
    agent = Agent.objects.create(
        organization=f_import_scope.organization,
        runtime=Runtime.CODEX,
        external_id='collision-import-agent',
    )
    session = AgentSession.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        external_session_id='claude-mem:fixture-store:sdk_session:content-session-collision',
        content_session_id='content-session-collision',
        memory_session_id='memory-session-collision',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=0,
    )
    row = {
        'id': 41,
        'memory_session_id': 'memory-session-collision',
        'project': '/workspace/example-repo',
        'text': 'Collision observation body.',
        'type': 'discovery',
        'title': 'Collision observation',
        'created_at': '2026-06-25T09:02:00Z',
    }

    return importer, context, session, row


def _raw_import_identity(
    importer: ClaudeMemImporter,
    context: ImportContext,
    session: AgentSession,
    row: dict[str, object],
) -> dict[str, object]:
    source_id = importer._observation_source_id(context, row)
    payload = import_services.redact_value({**row, 'source_id': source_id}).value

    return {
        'organization': context.organization,
        'project': context.project,
        'team': context.team,
        'agent': session.agent,
        'session': session,
        'event_type': 'claude_mem.observation',
        'source_adapter': 'claude_mem',
        'client_event_id': source_id,
        'idempotency_key': source_id,
        'content_hash': importer._content_hash(source_id, payload),
        'runtime': session.runtime,
        'payload_schema_version': 'v1',
        'normalization_contract_version': 1,
        'normalization_disposition': RawEventNormalizationDisposition.OBSERVATION,
        'normalization_reason': None,
        'payload': payload,
        'metadata': {'source': 'claude_mem_import'},
    }


@pytest.mark.django_db
def test_import_rejects_hook_owned_raw_identity_collision_without_mutation(
    f_import_scope: ImportScope,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    raw_identity = _raw_import_identity(importer, context, session, row)
    raw_identity['source_adapter'] = Runtime.CODEX
    raw_identity['metadata'] = {'repository_root': '/workspace/example-repo'}
    raw_event = RawEventEnvelope.objects.create(**raw_identity)
    raw_snapshot = {
        'source_adapter': raw_event.source_adapter,
        'sequence_number': raw_event.sequence_number,
        'normalization_contract_version': raw_event.normalization_contract_version,
        'normalization_disposition': raw_event.normalization_disposition,
        'normalization_reason': raw_event.normalization_reason,
    }

    with pytest.raises(ValueError, match='^import raw event identity collision$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event.refresh_from_db()
    session.refresh_from_db()
    assert {
        'source_adapter': raw_event.source_adapter,
        'sequence_number': raw_event.sequence_number,
        'normalization_contract_version': raw_event.normalization_contract_version,
        'normalization_disposition': raw_event.normalization_disposition,
        'normalization_reason': raw_event.normalization_reason,
    } == raw_snapshot
    assert session.observation_sequence_cursor == 0
    assert RawEventEnvelope.objects.count() == 1
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not MemoryCandidate.objects.exists()
    assert not Memory.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mismatch',
    ['session', 'team', 'agent', 'event_type', 'client_event_id'],
)
def test_raw_import_reuse_rejects_same_producer_identity_mismatch(
    f_import_scope: ImportScope,
    mismatch: str,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    raw_identity = _raw_import_identity(importer, context, session, row)
    other_agent = Agent.objects.create(
        organization=context.organization,
        runtime=Runtime.CODEX,
        external_id='collision-other-agent',
    )
    other_session = AgentSession.objects.create(
        organization=context.organization,
        project=context.project,
        team=context.team,
        agent=other_agent,
        external_session_id='collision-other-session',
        runtime=Runtime.CODEX,
    )
    mismatches = {
        'session': other_session,
        'team': None,
        'agent': other_agent,
        'event_type': 'claude_mem.user_prompt',
        'client_event_id': 'collision-other-client-event',
    }
    raw_identity[mismatch] = mismatches[mismatch]
    raw_event = RawEventEnvelope.objects.create(**raw_identity)
    raw_snapshot = {
        'team_id': raw_event.team_id,
        'agent_id': raw_event.agent_id,
        'session_id': raw_event.session_id,
        'event_type': raw_event.event_type,
        'client_event_id': raw_event.client_event_id,
        'sequence_number': raw_event.sequence_number,
    }

    with pytest.raises(ValueError, match='^import raw event identity collision$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event.refresh_from_db()
    session.refresh_from_db()
    assert {
        'team_id': raw_event.team_id,
        'agent_id': raw_event.agent_id,
        'session_id': raw_event.session_id,
        'event_type': raw_event.event_type,
        'client_event_id': raw_event.client_event_id,
        'sequence_number': raw_event.sequence_number,
    } == raw_snapshot
    assert session.observation_sequence_cursor == 0
    assert RawEventEnvelope.objects.count() == 1
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not MemoryCandidate.objects.exists()
    assert not Memory.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('contract_mismatch', ['payload_schema', 'normalization'])
def test_import_rejects_reused_raw_with_wrong_typed_contract(
    f_import_scope: ImportScope,
    contract_mismatch: str,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    raw_identity = _raw_import_identity(importer, context, session, row)
    if contract_mismatch == 'payload_schema':
        raw_identity['payload_schema_version'] = 'v2'
    else:
        raw_identity['normalization_disposition'] = RawEventNormalizationDisposition.NO_OP
        raw_identity['normalization_reason'] = RawEventNormalizationReason.EVIDENCE_ONLY
    raw_event = RawEventEnvelope.objects.create(**raw_identity)

    with pytest.raises(ValueError, match='^import raw event identity collision$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event.refresh_from_db()
    session.refresh_from_db()
    assert raw_event.payload_schema_version == ('v2' if contract_mismatch == 'payload_schema' else 'v1')
    assert raw_event.normalization_disposition == (
        RawEventNormalizationDisposition.NO_OP
        if contract_mismatch == 'normalization'
        else RawEventNormalizationDisposition.OBSERVATION
    )
    assert raw_event.sequence_number is None
    assert session.observation_sequence_cursor == 0
    assert RawEventEnvelope.objects.count() == 1
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not MemoryCandidate.objects.exists()
    assert not Memory.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'corruption',
    [
        'source_metadata',
        'observation_team',
        'observation_raw_event',
        'extra_direct_source',
        'ambiguous_source_id',
        'second_generation_orphan',
        'raw_sequence_null',
        'raw_sequence_mismatch',
        'observation_sequence_null',
        'cursor_behind',
    ],
)
def test_import_rejects_corrupt_existing_typed_source_without_mutation(
    f_import_scope: ImportScope,
    corruption: str,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    raw_identity = _raw_import_identity(importer, context, session, row)
    raw_identity.update(
        normalization_contract_version=1,
        normalization_disposition=RawEventNormalizationDisposition.OBSERVATION,
        normalization_reason=None,
        sequence_number=1,
    )
    raw_event = RawEventEnvelope.objects.create(**raw_identity)
    source_id = importer._observation_source_id(context, row)
    observation = Observation.objects.create(
        organization=context.organization,
        project=context.project,
        team=context.team,
        agent=session.agent,
        session=session,
        raw_event=raw_event,
        observation_type='discovery',
        title=str(row['title']),
        body=str(row['text']),
        content_hash='existing-corrupt-replay-observation',
        generation_key=source_id,
        source_metadata={'source_id': source_id, 'event_type': 'claude_mem.observation'},
        session_sequence=1,
    )
    source = ObservationSource.objects.create(
        organization=context.organization,
        project=context.project,
        observation=observation,
        raw_event=raw_event,
        source_type='claude_mem',
        source_id=source_id,
        metadata={'event_type': 'claude_mem.observation'},
    )
    session.observation_sequence_cursor = 1
    session.save(update_fields=['observation_sequence_cursor'])
    if corruption == 'source_metadata':
        source.metadata = {'event_type': 'claude_mem.session_summary'}
        source.save(update_fields=['metadata'])
    elif corruption == 'observation_team':
        observation.team = None
        observation.save(update_fields=['team'])
    elif corruption == 'observation_raw_event':
        other_raw_identity = {**raw_identity}
        other_raw_identity.update(
            client_event_id='other-replay-raw',
            idempotency_key='other-replay-raw',
            content_hash='other-replay-raw-hash',
        )
        observation.raw_event = RawEventEnvelope.objects.create(**other_raw_identity)
        observation.save(update_fields=['raw_event'])
    elif corruption == 'extra_direct_source':
        ObservationSource.objects.create(
            organization=context.organization,
            project=context.project,
            observation=observation,
            raw_event=raw_event,
            source_type='hook_event',
            source_id='extra-direct-source',
        )
    elif corruption == 'ambiguous_source_id':
        second_observation = Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=session,
            raw_event=raw_event,
            observation_type='discovery',
            title='Ambiguous replay observation',
            content_hash='ambiguous-replay-observation',
            generation_key=source_id,
            source_metadata={'source_id': source_id, 'event_type': 'claude_mem.observation'},
            session_sequence=2,
        )
        ObservationSource.objects.create(
            organization=context.organization,
            project=context.project,
            observation=second_observation,
            raw_event=raw_event,
            source_type='claude_mem',
            source_id=source_id,
            metadata={'event_type': 'claude_mem.observation'},
        )
    elif corruption == 'second_generation_orphan':
        Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=session,
            observation_type='discovery',
            title='Second generation-key orphan',
            content_hash='second-generation-key-orphan',
            generation_key=source_id,
            source_metadata={'source_id': source_id, 'event_type': 'claude_mem.observation'},
        )
    elif corruption in {'raw_sequence_null', 'raw_sequence_mismatch'}:
        raw_event.sequence_number = {'raw_sequence_null': None, 'raw_sequence_mismatch': 2}[corruption]
        raw_event.save(update_fields=['sequence_number'])
    elif corruption == 'observation_sequence_null':
        observation.session_sequence = None
        observation.save(update_fields=['session_sequence'])
    else:
        session.observation_sequence_cursor = 0
        session.save(update_fields=['observation_sequence_cursor'])

    def replay_state() -> dict[str, object]:
        return {
            'cursor': AgentSession.objects.get(id=session.id).observation_sequence_cursor,
            'raws': list(
                RawEventEnvelope.objects.order_by('id').values_list(
                    'id',
                    'sequence_number',
                    'source_adapter',
                    'payload_schema_version',
                    'normalization_disposition',
                    'normalization_reason',
                ),
            ),
            'observations': list(
                Observation.objects.order_by('id').values_list(
                    'id',
                    'team_id',
                    'raw_event_id',
                    'session_sequence',
                    'generation_key',
                    'source_metadata',
                ),
            ),
            'sources': list(
                ObservationSource.objects.order_by('id').values_list(
                    'id',
                    'observation_id',
                    'raw_event_id',
                    'source_type',
                    'source_id',
                    'metadata',
                ),
            ),
            'candidates': MemoryCandidate.objects.count(),
            'memories': Memory.objects.count(),
            'work': WorkflowWork.objects.count(),
            'outbox': CeleryOutbox.objects.count(),
        }

    before = replay_state()

    with pytest.raises(ValueError, match='^import observation source identity collision$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    assert replay_state() == before


@pytest.mark.django_db
@pytest.mark.parametrize('collision', ['raw_source', 'observation_source', 'falsy_source_metadata'])
def test_import_rejects_noncanonical_first_source_without_mutation(
    f_import_scope: ImportScope,
    collision: str,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    source_id = importer._observation_source_id(context, row)
    raw_identity = _raw_import_identity(importer, context, session, row)
    if collision == 'raw_source':
        raw_event = RawEventEnvelope.objects.create(**raw_identity)
        observation = Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=session,
            raw_event=raw_event,
            observation_type='discovery',
            title='Unrelated sourced observation',
            content_hash='unrelated-first-link-observation',
            generation_key='unrelated-first-link-observation',
        )
        ObservationSource.objects.create(
            organization=context.organization,
            project=context.project,
            observation=observation,
            raw_event=raw_event,
            source_type='hook_event',
            source_id='unrelated-first-link-source',
        )
    else:
        observation = Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=session,
            observation_type='discovery',
            title=str(row['title']),
            body=str(row['text']),
            content_hash=importer._content_hash(source_id, row['title'], row['text']),
            generation_key=source_id,
            source_metadata=[] if collision == 'falsy_source_metadata' else {},
        )
        if collision == 'observation_source':
            unrelated_raw_identity = {**raw_identity}
            unrelated_raw_identity.update(
                client_event_id='unrelated-observation-raw',
                idempotency_key='unrelated-observation-raw',
                content_hash='unrelated-observation-raw-hash',
            )
            unrelated_raw = RawEventEnvelope.objects.create(**unrelated_raw_identity)
            ObservationSource.objects.create(
                organization=context.organization,
                project=context.project,
                observation=observation,
                raw_event=unrelated_raw,
                source_type='hook_event',
                source_id='unrelated-observation-source',
            )

    def first_link_state() -> dict[str, object]:
        return {
            'cursor': AgentSession.objects.get(id=session.id).observation_sequence_cursor,
            'raws': list(
                RawEventEnvelope.objects.order_by('id').values_list(
                    'id',
                    'sequence_number',
                    'client_event_id',
                    'idempotency_key',
                ),
            ),
            'observations': list(
                Observation.objects.order_by('id').values_list(
                    'id',
                    'raw_event_id',
                    'session_sequence',
                    'generation_key',
                    'source_metadata',
                ),
            ),
            'sources': list(
                ObservationSource.objects.order_by('id').values_list(
                    'id',
                    'observation_id',
                    'raw_event_id',
                    'source_type',
                    'source_id',
                ),
            ),
            'candidates': MemoryCandidate.objects.count(),
            'memories': Memory.objects.count(),
            'work': WorkflowWork.objects.count(),
            'outbox': CeleryOutbox.objects.count(),
        }

    before = first_link_state()

    with pytest.raises(ValueError, match='^import observation source identity collision$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    assert first_link_state() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    'partial_evidence',
    ['changed_raw_payload', 'orphan_generation_content', 'cursor_behind_positive_sequence', 'zero_sequence'],
)
def test_import_rejects_partial_evidence_identity_mismatch_without_mutation(
    f_import_scope: ImportScope,
    monkeypatch: pytest.MonkeyPatch,
    partial_evidence: str,
) -> None:
    importer, context, session, row = _import_context_and_observation(f_import_scope)
    source_id = importer._observation_source_id(context, row)
    if partial_evidence == 'changed_raw_payload':
        raw_identity = _raw_import_identity(importer, context, session, row)
        raw_identity['payload'] = {'source_id': source_id, 'text': 'Earlier raw payload.'}
        raw_identity['content_hash'] = importer._content_hash(source_id, raw_identity['payload'])
        RawEventEnvelope.objects.create(**raw_identity)
        error = 'import raw event identity collision'
    elif partial_evidence == 'orphan_generation_content':
        conflicting_session = AgentSession.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            external_session_id='orphan-generation-conflicting-session',
            runtime=Runtime.CODEX,
            observation_sequence_cursor=0,
        )
        Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=conflicting_session,
            observation_type='discovery',
            title='Orphan source-identity observation',
            content_hash=importer._content_hash(source_id, row['title'], row['text']),
            generation_key=source_id,
            source_metadata={},
        )
        error = 'import observation source identity collision'
    else:
        observation = Observation.objects.create(
            organization=context.organization,
            project=context.project,
            team=context.team,
            agent=session.agent,
            session=session,
            observation_type='discovery',
            title=str(row['title']),
            body=str(row['text']),
            content_hash=importer._content_hash(source_id, row['title'], row['text']),
            generation_key=source_id,
            source_metadata={},
            session_sequence=7 if partial_evidence == 'cursor_behind_positive_sequence' else None,
        )
        if partial_evidence == 'cursor_behind_positive_sequence':
            session.observation_sequence_cursor = 3
            session.save(update_fields=['observation_sequence_cursor'])
        else:
            observation.session_sequence = 0
            monkeypatch.setattr(importer, '_existing_import_observation', lambda **_kwargs: observation)
        error = 'import observation source identity collision'

    def partial_state() -> dict[str, object]:
        return {
            'session_cursors': list(
                AgentSession.objects.order_by('id').values_list('id', 'observation_sequence_cursor'),
            ),
            'raws': list(
                RawEventEnvelope.objects.order_by('id').values_list(
                    'id',
                    'sequence_number',
                    'content_hash',
                    'payload',
                ),
            ),
            'observations': list(
                Observation.objects.order_by('id').values_list(
                    'id',
                    'raw_event_id',
                    'session_sequence',
                    'content_hash',
                    'generation_key',
                    'source_metadata',
                ),
            ),
            'sources': list(
                ObservationSource.objects.order_by('id').values_list('id', 'observation_id', 'raw_event_id')
            ),
            'candidates': MemoryCandidate.objects.count(),
            'memories': Memory.objects.count(),
            'work': WorkflowWork.objects.count(),
            'outbox': CeleryOutbox.objects.count(),
        }

    before = partial_state()

    with pytest.raises(ValueError, match=f'^{error}$'):
        importer.import_batch(context, 'observations', [row], defer_embedding=True)

    assert partial_state() == before


@pytest.mark.django_db
def test_prompt_replay_keeps_first_evidence_when_payload_and_metadata_change(
    f_import_scope: ImportScope,
) -> None:
    importer, context, session, _row = _import_context_and_observation(f_import_scope)
    row = {
        'id': 42,
        'content_session_id': session.content_session_id,
        'prompt_number': 3,
        'prompt_text': 'First prompt evidence.',
        'created_at': '2026-06-25T09:02:00Z',
    }

    first_result = importer.import_batch(context, 'user_prompts', [row])
    raw_event = RawEventEnvelope.objects.get(idempotency_key=importer._prompt_source_id(context, row))
    first_payload = raw_event.payload
    first_content_hash = raw_event.content_hash
    raw_event.metadata = {'source': 'claude_mem_import', 'enriched': True}
    raw_event.save(update_fields=['metadata'])

    changed_row = {**row, 'prompt_text': 'Changed upstream prompt evidence.'}
    replay_result = importer.import_batch(context, 'user_prompts', [changed_row])

    raw_event.refresh_from_db()
    assert first_result.created == 1
    assert replay_result.created == 0
    assert replay_result.duplicates == 1
    assert RawEventEnvelope.objects.count() == 1
    assert raw_event.payload == first_payload
    assert raw_event.content_hash == first_content_hash
    assert raw_event.metadata == {'source': 'claude_mem_import', 'enriched': True}
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
def test_prompt_replay_rejects_non_null_sequence_without_mutation(
    f_import_scope: ImportScope,
) -> None:
    importer, context, session, _row = _import_context_and_observation(f_import_scope)
    row = {
        'id': 46,
        'content_session_id': session.content_session_id,
        'prompt_number': 4,
        'prompt_text': 'Prompt sequence invariant.',
        'created_at': '2026-06-25T09:02:00Z',
    }
    importer.import_batch(context, 'user_prompts', [row])
    raw_event = RawEventEnvelope.objects.get(idempotency_key=importer._prompt_source_id(context, row))
    raw_event.sequence_number = 1
    raw_event.save(update_fields=['sequence_number'])
    before = list(
        RawEventEnvelope.objects.values_list(
            'id',
            'sequence_number',
            'payload',
            'content_hash',
            'metadata',
        ),
    )

    with pytest.raises(ValueError, match='^import raw event identity collision$'):
        importer.import_batch(context, 'user_prompts', [row])

    assert (
        list(
            RawEventEnvelope.objects.values_list(
                'id',
                'sequence_number',
                'payload',
                'content_hash',
                'metadata',
            ),
        )
        == before
    )
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('team_case', ['different', 'expected_null', 'session_null'])
def test_full_import_rejects_session_team_mismatch_without_partial_writes(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
    team_case: str,
) -> None:
    other_team = Team.objects.create(
        organization=f_import_scope.organization,
        name=f'Other Import Team {team_case}',
        slug=f'other-import-team-{team_case}',
    )
    expected_team = None if team_case == 'expected_null' else f_import_scope.team
    session_team = None if team_case == 'session_null' else other_team
    agent = Agent.objects.create(
        organization=f_import_scope.organization,
        runtime=Runtime.CODEX,
        external_id=f'team-mismatch-agent-{team_case}',
    )
    session = AgentSession.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=session_team,
        agent=agent,
        external_session_id='claude-mem:fixture-store:sdk_session:content-session-fixture-001',
        content_session_id='content-session-fixture-001',
        memory_session_id='memory-session-fixture-001',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=7,
    )
    initial_agent_ids = set(Agent.objects.values_list('id', flat=True))
    initial_session_ids = set(AgentSession.objects.values_list('id', flat=True))

    with pytest.raises(ValueError, match='^import session team mismatch$'):
        ClaudeMemImporter().execute(
            ClaudeMemImportInput(
                source_root=f_claude_mem_fixture,
                organization_id=f_import_scope.organization.id,
                project_id=f_import_scope.project.id,
                team_id=expected_team.id if expected_team is not None else None,
                source_store_id='fixture-store',
                apply=True,
            ),
        )

    session.refresh_from_db()
    assert session.team_id == (session_team.id if session_team is not None else None)
    assert session.observation_sequence_cursor == 7
    assert set(Agent.objects.values_list('id', flat=True)) == initial_agent_ids
    assert set(AgentSession.objects.values_list('id', flat=True)) == initial_session_ids
    assert not RawEventEnvelope.objects.exists()
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not MemoryCandidate.objects.exists()
    assert not Memory.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('table', ['sdk_sessions', 'user_prompts'])
def test_session_touching_batch_rejects_wrong_team_without_partial_writes(
    f_import_scope: ImportScope,
    table: str,
) -> None:
    importer, context, session, _row = _import_context_and_observation(f_import_scope)
    other_team = Team.objects.create(
        organization=context.organization,
        name=f'Wrong Batch Team {table}',
        slug=f'wrong-batch-team-{table.replace("_", "-")}',
    )
    session.team = other_team
    session.save(update_fields=['team'])
    session_row = {
        'id': 44,
        'content_session_id': session.content_session_id,
        'memory_session_id': session.memory_session_id,
        'project': context.project.repository_root,
        'platform_source': 'codex',
        'started_at': '2026-06-25T09:00:00Z',
    }
    prompt_row = {
        'id': 44,
        'content_session_id': session.content_session_id,
        'prompt_number': 1,
        'prompt_text': 'Wrong-team prompt evidence.',
        'created_at': '2026-06-25T09:01:00Z',
    }
    initial_agent_ids = set(Agent.objects.values_list('id', flat=True))
    initial_session_ids = set(AgentSession.objects.values_list('id', flat=True))

    with pytest.raises(ValueError, match='^import session team mismatch$'):
        importer.import_batch(context, table, [session_row if table == 'sdk_sessions' else prompt_row])

    session.refresh_from_db()
    assert session.team_id == other_team.id
    assert session.observation_sequence_cursor == 0
    assert set(Agent.objects.values_list('id', flat=True)) == initial_agent_ids
    assert set(AgentSession.objects.values_list('id', flat=True)) == initial_session_ids
    assert not RawEventEnvelope.objects.exists()
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not MemoryCandidate.objects.exists()
    assert not Memory.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
def test_project_only_session_and_prompt_batches_accept_null_team(
    f_import_scope: ImportScope,
) -> None:
    importer, team_context, session, _row = _import_context_and_observation(f_import_scope)
    session.team = None
    session.save(update_fields=['team'])
    context = ImportContext(
        source_store_id=team_context.source_store_id,
        organization=team_context.organization,
        project=team_context.project,
        team=None,
    )
    session_result = importer.import_batch(
        context,
        'sdk_sessions',
        [
            {
                'id': 45,
                'content_session_id': session.content_session_id,
                'memory_session_id': session.memory_session_id,
                'project': context.project.repository_root,
                'platform_source': 'codex',
                'started_at': '2026-06-25T09:00:00Z',
            },
        ],
    )
    prompt_result = importer.import_batch(
        context,
        'user_prompts',
        [
            {
                'id': 45,
                'content_session_id': session.content_session_id,
                'prompt_number': 1,
                'prompt_text': 'Project-only prompt evidence.',
                'created_at': '2026-06-25T09:01:00Z',
            },
        ],
    )

    raw_event = RawEventEnvelope.objects.get(event_type='claude_mem.user_prompt')
    assert session_result.duplicates == 1
    assert prompt_result.created == 1
    assert raw_event.team_id is None
    assert raw_event.session_id == session.id
    assert not Observation.objects.exists()
    assert not ObservationSource.objects.exists()
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
def test_existing_sdk_session_is_locked_once(
    f_import_scope: ImportScope,
) -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL row-lock SQL')

    importer, context, session, _row = _import_context_and_observation(f_import_scope)
    with CaptureQueriesContext(connection) as queries:
        result = importer.import_batch(
            context,
            'sdk_sessions',
            [
                {
                    'id': 47,
                    'content_session_id': session.content_session_id,
                    'memory_session_id': session.memory_session_id,
                    'project': context.project.repository_root,
                    'platform_source': 'codex',
                    'started_at': '2026-06-25T09:00:00Z',
                },
            ],
        )

    row_lock_queries = [query['sql'] for query in queries if 'FOR UPDATE OF "core_agentsession"' in query['sql']]
    assert result.duplicates == 1
    assert len(row_lock_queries) == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_imports_lock_sessions_in_stable_order_without_deadlock(
    f_import_scope: ImportScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL row locks')

    importer, context, first_session, first_row = _import_context_and_observation(f_import_scope)
    second_session = AgentSession.objects.create(
        organization=context.organization,
        project=context.project,
        team=context.team,
        agent=first_session.agent,
        external_session_id='claude-mem:fixture-store:sdk_session:content-session-concurrent-2',
        content_session_id='content-session-concurrent-2',
        memory_session_id='memory-session-concurrent-2',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=0,
    )
    second_row = {
        **first_row,
        'id': 43,
        'memory_session_id': second_session.memory_session_id,
        'text': 'Second concurrent observation body.',
        'title': 'Second concurrent observation',
    }
    rows = [first_row, second_row]
    barrier = Barrier(2)
    thread_state = local()
    pid_lock = Lock()
    backend_pids: list[int] = []
    real_lock = ClaudeMemImporter._lock_import_sessions

    def synchronized_lock(
        self: ClaudeMemImporter,
        sessions: list[AgentSession],
        lock_context: ImportContext,
    ) -> dict[UUID, AgentSession]:
        if not getattr(thread_state, 'synchronized', False):
            thread_state.synchronized = True
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                backend_pid = int(cursor.fetchone()[0])
            with pid_lock:
                backend_pids.append(backend_pid)
            barrier.wait(timeout=10)

        return real_lock(self, sessions, lock_context)

    monkeypatch.setattr(ClaudeMemImporter, '_lock_import_sessions', synchronized_lock)

    def import_rows(import_rows: list[dict[str, object]]) -> import_services.BatchImportResult:
        close_old_connections()
        try:
            thread_context = ImportContext(
                source_store_id=context.source_store_id,
                organization=Organization.objects.get(id=context.organization.id),
                project=Project.objects.get(id=context.project.id),
                team=Team.objects.get(id=context.team.id),
            )
            return importer.import_batch(thread_context, 'observations', import_rows, defer_embedding=True)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(import_rows, rows), executor.submit(import_rows, list(reversed(rows)))]
        results = [future.result(timeout=20) for future in futures]

    assert len(set(backend_pids)) == 2
    assert sorted((result.created, result.duplicates, result.skipped) for result in results) == [
        (0, 2, 0),
        (2, 0, 0),
    ]
    expected_source_ids = {importer._observation_source_id(context, row) for row in rows}
    assert set(RawEventEnvelope.objects.values_list('client_event_id', flat=True)) == expected_source_ids
    assert set(Observation.objects.values_list('generation_key', flat=True)) == expected_source_ids
    assert set(ObservationSource.objects.values_list('source_id', flat=True)) == expected_source_ids
    assert RawEventEnvelope.objects.count() == 2
    assert Observation.objects.count() == 2
    assert ObservationSource.objects.count() == 2
    for session in (first_session, second_session):
        session.refresh_from_db()
        assert session.observation_sequence_cursor == 1
        assert list(Observation.objects.filter(session=session).values_list('session_sequence', flat=True)) == [1]
        assert list(RawEventEnvelope.objects.filter(session=session).values_list('sequence_number', flat=True)) == [1]
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
def test_import_reports_prompt_with_missing_source_session_as_unsupported(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO user_prompts (id, content_session_id, prompt_number, prompt_text, created_at, '
            'created_at_epoch) VALUES (?, ?, ?, ?, ?, ?)',
            (
                2,
                'missing-content-session',
                1,
                'This prompt points at a missing upstream session.',
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    report = ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    unsupported = {(entry['source_type'], entry['source_id'], entry['reason']) for entry in report['unsupported']}
    assert (
        'user_prompts',
        'claude-mem:fixture-store:user_prompt:missing-content-session:1:2',
        'missing_source_session',
    ) in unsupported
    assert report['created']['raw_events'] == 3
    assert report['duplicates']['raw_events'] == 0


@pytest.mark.django_db
def test_import_materializes_v1_observation_sequences_and_prompt_no_op(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    session = AgentSession.objects.get(content_session_id='content-session-fixture-001')
    observations = list(Observation.objects.filter(session=session).order_by('session_sequence'))
    assert [observation.session_sequence for observation in observations] == [1, 2]
    assert session.observation_sequence_cursor == 2

    observation_raw_events = RawEventEnvelope.objects.filter(
        session=session,
        event_type__in=['claude_mem.observation', 'claude_mem.session_summary'],
    )
    assert observation_raw_events.count() == 2
    assert set(observation_raw_events.values_list('normalization_contract_version', flat=True)) == {1}
    assert set(observation_raw_events.values_list('normalization_disposition', flat=True)) == {
        RawEventNormalizationDisposition.OBSERVATION,
    }
    assert observation_raw_events.filter(normalization_reason__isnull=True).count() == 2
    assert observation_raw_events.filter(sequence_number__in=[1, 2]).count() == 2
    assert ObservationSource.objects.filter(observation__session=session).count() == 2
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()

    prompt_raw_event = RawEventEnvelope.objects.get(event_type='claude_mem.user_prompt')
    assert prompt_raw_event.normalization_contract_version == 1
    assert prompt_raw_event.normalization_disposition == RawEventNormalizationDisposition.NO_OP
    assert prompt_raw_event.normalization_reason == RawEventNormalizationReason.EVIDENCE_ONLY
    assert prompt_raw_event.sequence_number is None
    assert not ObservationSource.objects.filter(raw_event=prompt_raw_event).exists()


@pytest.mark.django_db
def test_import_binds_session_scoped_legacy_observation_and_reuses_sequence(
    f_import_scope: ImportScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importer = ClaudeMemImporter()
    context = ImportContext(
        source_store_id='fixture-store',
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
    )
    row = {
        'id': 1,
        'memory_session_id': 'memory-session-fixture-001',
        'project': '/workspace/example-repo',
        'text': 'Existing imported observation body.',
        'type': 'discovery',
        'title': 'Existing imported observation',
        'created_at': '2026-06-25T09:02:00Z',
    }
    agent = Agent.objects.create(
        organization=f_import_scope.organization,
        runtime=Runtime.CODEX,
        external_id='existing-import-agent',
    )
    session = AgentSession.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        external_session_id='claude-mem:fixture-store:sdk_session:content-session-fixture-001',
        content_session_id='content-session-fixture-001',
        memory_session_id='memory-session-fixture-001',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=7,
    )
    source_id = importer._observation_source_id(context, row)
    observation = Observation.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        session=session,
        observation_type='discovery',
        title=str(row['title']),
        body=str(row['text']),
        content_hash=importer._content_hash(source_id, row['title'], row['text']),
        generation_key='',
        source_metadata={},
        session_sequence=7,
    )

    def fail_allocate(_session: AgentSession) -> int:
        raise AssertionError('existing observation sequence must be reused')

    monkeypatch.setattr('engram.imports.services.allocate_observation_sequence', fail_allocate)

    importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event = RawEventEnvelope.objects.get(client_event_id=source_id)
    source = ObservationSource.objects.get(source_id=source_id)
    session.refresh_from_db()
    observation.refresh_from_db()
    assert raw_event.sequence_number == 7
    assert raw_event.normalization_contract_version == 1
    assert raw_event.normalization_disposition == RawEventNormalizationDisposition.OBSERVATION
    assert raw_event.normalization_reason is None
    assert source.observation_id == observation.id
    assert source.raw_event_id == raw_event.id
    assert observation.raw_event_id == raw_event.id
    assert observation.generation_key == source_id
    assert observation.source_metadata == {'source_id': source_id, 'event_type': 'claude_mem.observation'}
    assert ObservationSource.objects.filter(observation=observation).count() == 1
    assert Observation.objects.count() == 1
    assert session.observation_sequence_cursor == 7
    assert Observation.objects.filter(id=observation.id, session_sequence=7).count() == 1
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


@pytest.mark.django_db
def test_import_assigns_sequence_to_legacy_observation_without_sequence(
    f_import_scope: ImportScope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importer = ClaudeMemImporter()
    context = ImportContext(
        source_store_id='fixture-store',
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
    )
    row = {
        'id': 1,
        'memory_session_id': 'memory-session-fixture-001',
        'project': '/workspace/example-repo',
        'text': 'Legacy observation body without a sequence.',
        'type': 'discovery',
        'title': 'Legacy observation without a sequence',
        'created_at': '2026-06-25T09:02:00Z',
    }
    agent = Agent.objects.create(
        organization=f_import_scope.organization,
        runtime=Runtime.CODEX,
        external_id='legacy-import-agent',
    )
    session = AgentSession.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        external_session_id='claude-mem:fixture-store:sdk_session:content-session-fixture-001',
        content_session_id='content-session-fixture-001',
        memory_session_id='memory-session-fixture-001',
        runtime=Runtime.CODEX,
        observation_sequence_cursor=None,
    )
    Observation.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        session=session,
        observation_type='discovery',
        title='Existing positive observation',
        body='Existing positive observation body.',
        content_hash='legacy-positive-observation',
        generation_key='legacy-positive-observation',
        session_sequence=4,
    )
    source_id = importer._observation_source_id(context, row)
    observation = Observation.objects.create(
        organization=f_import_scope.organization,
        project=f_import_scope.project,
        team=f_import_scope.team,
        agent=agent,
        session=session,
        observation_type='discovery',
        title=str(row['title']),
        body=str(row['text']),
        content_hash=importer._content_hash(source_id, row['title'], row['text']),
        generation_key=source_id,
        session_sequence=None,
    )

    real_allocate = import_services.allocate_observation_sequence
    allocated_sessions: list[object] = []

    def track_allocate(locked_session: AgentSession) -> int:
        allocated_sessions.append(locked_session.id)
        return real_allocate(locked_session)

    monkeypatch.setattr('engram.imports.services.allocate_observation_sequence', track_allocate)

    importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event = RawEventEnvelope.objects.get(client_event_id=source_id)
    source = ObservationSource.objects.get(source_id=source_id)
    observation.refresh_from_db()
    session.refresh_from_db()
    assert allocated_sessions == [session.id]
    assert raw_event.sequence_number == 5
    assert source.observation_id == observation.id
    assert observation.session_sequence == 5
    assert session.observation_sequence_cursor == 5
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()

    importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event.refresh_from_db()
    observation.refresh_from_db()
    session.refresh_from_db()
    assert allocated_sessions == [session.id]
    assert raw_event.sequence_number == 5
    assert observation.session_sequence == 5
    assert session.observation_sequence_cursor == 5


@pytest.mark.django_db
def test_repeated_import_reuses_import_sequences_and_cursor(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    import_input = ClaudeMemImportInput(
        source_root=f_claude_mem_fixture,
        organization_id=f_import_scope.organization.id,
        project_id=f_import_scope.project.id,
        team_id=f_import_scope.team.id,
        source_store_id='fixture-store',
        apply=True,
    )
    ClaudeMemImporter().execute(import_input)
    session = AgentSession.objects.get(content_session_id='content-session-fixture-001')
    first_sequences = list(Observation.objects.filter(session=session).values_list('session_sequence', flat=True))
    first_cursor = session.observation_sequence_cursor

    ClaudeMemImporter().execute(import_input)
    session.refresh_from_db()

    assert (
        list(Observation.objects.filter(session=session).values_list('session_sequence', flat=True)) == first_sequences
    )
    assert session.observation_sequence_cursor == first_cursor


@pytest.mark.django_db
def test_dry_run_importable_counts_apply_real_predicates(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO observations '
            '(id, memory_session_id, project, text, type, title, created_at, created_at_epoch) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                2,
                'missing-memory-session',
                '/workspace/example-repo',
                'This observation points at a missing upstream session.',
                'discovery',
                'Missing session observation',
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )
        connection.execute(
            'INSERT INTO user_prompts (id, content_session_id, prompt_number, prompt_text, created_at, '
            'created_at_epoch) VALUES (?, ?, ?, ?, ?, ?)',
            (
                2,
                'missing-content-session',
                1,
                'This prompt points at a missing upstream session.',
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    report = ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=False,
        ),
    )

    assert report['counts']['observations']['seen'] == 2
    assert report['counts']['observations']['importable_memories'] == 1
    assert report['counts']['user_prompts']['seen'] == 2
    assert report['counts']['user_prompts']['importable_raw_events'] == 1


@pytest.mark.django_db
def test_import_caps_oversized_observation_body_and_stays_idempotent_on_rerun(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    oversized_body = 'x' * (_MAX_OBSERVATION_TEXT_CHARS + 4000)
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO observations '
            '(id, memory_session_id, project, text, type, title, created_at, created_at_epoch) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                2,
                'memory-session-fixture-001',
                '/workspace/example-repo',
                oversized_body,
                'discovery',
                'Oversized body observation',
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    import_input = ClaudeMemImportInput(
        source_root=f_claude_mem_fixture,
        organization_id=f_import_scope.organization.id,
        project_id=f_import_scope.project.id,
        team_id=f_import_scope.team.id,
        source_store_id='fixture-store',
        apply=True,
    )

    first_report = ClaudeMemImporter().execute(import_input)
    observation = Observation.objects.get(title='Oversized body observation')

    assert len(observation.body) == _MAX_OBSERVATION_TEXT_CHARS
    assert first_report['created']['observations'] == 3

    second_report = ClaudeMemImporter().execute(import_input)

    assert second_report['created']['observations'] == 0
    assert Observation.objects.filter(title='Oversized body observation').count() == 1


@pytest.mark.django_db
def test_import_caps_oversized_observation_narrative(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    oversized_narrative = 'y' * (_MAX_OBSERVATION_TEXT_CHARS + 4000)
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO observations '
            '(id, memory_session_id, project, text, type, title, narrative, created_at, created_at_epoch) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                2,
                'memory-session-fixture-001',
                '/workspace/example-repo',
                'Observation with an oversized narrative field.',
                'discovery',
                'Oversized narrative observation',
                oversized_narrative,
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    observation = Observation.objects.get(title='Oversized narrative observation')
    assert len(observation.narrative) == _MAX_OBSERVATION_TEXT_CHARS


@pytest.mark.django_db
def test_import_caps_oversized_facts_and_concepts_lists(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    facts = json.dumps([f'fact-{index}' for index in range(150)])
    concepts = json.dumps([f'concept-{index}' for index in range(150)])
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO observations '
            '(id, memory_session_id, project, text, type, title, facts, concepts, created_at, created_at_epoch) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                2,
                'memory-session-fixture-001',
                '/workspace/example-repo',
                'Observation with oversized facts and concepts lists.',
                'discovery',
                'Oversized lists observation',
                facts,
                concepts,
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    observation = Observation.objects.get(title='Oversized lists observation')
    assert len(observation.facts) == _MAX_OBSERVATION_LIST_ITEMS
    assert len(observation.concepts) == _MAX_OBSERVATION_LIST_ITEMS


@pytest.mark.django_db
def test_import_report_flags_truncation_when_a_field_was_capped(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'INSERT INTO observations '
            '(id, memory_session_id, project, text, type, title, created_at, created_at_epoch) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                2,
                'memory-session-fixture-001',
                '/workspace/example-repo',
                'z' * (_MAX_OBSERVATION_TEXT_CHARS + 1),
                'discovery',
                'Truncation flag observation',
                '2026-06-25T09:03:00Z',
                1782378180000,
            ),
        )

    report = ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    assert report['truncations'] == {'truncated': True}


@pytest.mark.django_db
def test_import_leaves_normal_sized_observation_fields_untouched(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    report = ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id='fixture-store',
            apply=True,
        ),
    )

    observation = Observation.objects.get(observation_type='discovery')
    assert observation.body == 'Importer fixture records a generated observation with file citation metadata.'
    assert observation.narrative == 'The agent reviewed a fixture file and captured import mapping notes.'
    assert observation.facts == [
        'Fixture data is sanitized',
        'File paths use /workspace/example-repo',
    ]
    assert observation.concepts == ['migration', 'fixture', 'redaction']
    assert report['truncations'] == {'truncated': False}
