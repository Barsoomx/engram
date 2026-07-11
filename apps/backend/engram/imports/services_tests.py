from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    Agent,
    AgentSession,
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
def test_import_reuses_existing_observation_sequence_for_new_raw_source(
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
        generation_key=source_id,
        session_sequence=7,
    )

    def fail_allocate(_session: AgentSession) -> int:
        raise AssertionError('existing observation sequence must be reused')

    monkeypatch.setattr('engram.imports.services.allocate_observation_sequence', fail_allocate)

    importer.import_batch(context, 'observations', [row], defer_embedding=True)

    raw_event = RawEventEnvelope.objects.get(client_event_id=source_id)
    source = ObservationSource.objects.get(source_id=source_id)
    session.refresh_from_db()
    assert raw_event.sequence_number == 7
    assert raw_event.normalization_contract_version == 1
    assert raw_event.normalization_disposition == RawEventNormalizationDisposition.OBSERVATION
    assert raw_event.normalization_reason is None
    assert source.observation_id == observation.id
    assert source.raw_event_id == raw_event.id
    assert ObservationSource.objects.filter(observation=observation).count() == 1
    assert session.observation_sequence_cursor == 7
    assert Observation.objects.filter(id=observation.id, session_sequence=7).count() == 1
    assert not WorkflowWork.objects.exists()
    assert not CeleryOutbox.objects.exists()


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
