from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from engram.core.models import (
    AgentSession,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    RetrievalDocument,
    Team,
)
from engram.imports.services import ClaudeMemImporter, ClaudeMemImportError, ClaudeMemImportInput


@dataclass(frozen=True)
class ImportScope:
    organization: Organization
    project: Project
    team: Team


@pytest.fixture
def f_import_scope() -> ImportScope:
    organization = Organization.objects.create(name='Fixture Org', slug='fixture-org')
    project = Project.objects.create(
        organization=organization,
        name='Fixture Project',
        slug='fixture-project',
        repository_root='/workspace/example-repo',
    )
    team = Team.objects.create(organization=organization, name='Fixture Team', slug='fixture-team')
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

    for artifact in [
        'settings.json',
        'transcript-watch.json',
        'transcript-watch-state.json',
        'corpora',
        'vector-db',
    ]:
        source = source_fixture / artifact
        target = source_root / artifact
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    return source_root


@pytest.mark.django_db
def test_claude_mem_importer_dry_run_reports_counts_without_writes(
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
            apply=False,
        ),
    )

    assert report['mode'] == 'dry_run'
    assert report['source']['kind'] == 'claude_mem'
    assert report['source']['source_store_id'] == 'fixture-store'
    assert report['source']['root'] == str(f_claude_mem_fixture)
    assert report['target'] == {
        'organization_id': str(f_import_scope.organization.id),
        'project_id': str(f_import_scope.project.id),
        'team_id': str(f_import_scope.team.id),
    }
    assert report['counts']['sdk_sessions']['seen'] == 1
    assert report['counts']['sdk_sessions']['importable'] == 1
    assert report['counts']['user_prompts']['seen'] == 1
    assert report['counts']['user_prompts']['importable_raw_events'] == 1
    assert report['counts']['observations']['seen'] == 1
    assert report['counts']['observations']['importable_memories'] == 1
    assert report['counts']['session_summaries']['seen'] == 1
    assert report['counts']['session_summaries']['importable_memories'] == 1
    assert report['counts']['pending_messages']['seen'] == 1
    assert report['counts']['pending_messages']['unsupported'] == 1
    assert report['created'] == {
        'agents': 0,
        'sessions': 0,
        'raw_events': 0,
        'observations': 0,
        'memory_candidates': 0,
        'memories': 0,
        'memory_versions': 0,
        'retrieval_documents': 0,
    }
    assert report['duplicates'] == {
        'sessions': 0,
        'raw_events': 0,
        'observations': 0,
        'memories': 0,
    }
    assert {
        'source_type': 'pending_messages',
        'source_id': 'pending_messages:1',
        'reason': 'transient_local_worker_queue',
    } in report['unsupported']
    assert report['warnings'] == []
    assert report['redactions'] == {'redacted': False}
    assert AgentSession.objects.count() == 0
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert Memory.objects.count() == 0


@pytest.mark.django_db
def test_claude_mem_importer_rejects_unlinked_team_before_source_reads(
    f_import_scope: ImportScope,
    tmp_path: Path,
) -> None:
    unlinked_team = Team.objects.create(
        organization=f_import_scope.organization,
        name='Unlinked Team',
        slug='unlinked-team',
    )

    with pytest.raises(ClaudeMemImportError, match='team is not linked to project'):
        ClaudeMemImporter().execute(
            ClaudeMemImportInput(
                source_root=tmp_path / 'missing-source',
                organization_id=f_import_scope.organization.id,
                project_id=f_import_scope.project.id,
                team_id=unlinked_team.id,
                source_store_id='fixture-store',
                apply=False,
            ),
        )


@pytest.mark.django_db
def test_claude_mem_importer_reports_deferred_artifacts_and_housekeeping_sources(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    transcript_dir = f_claude_mem_fixture / 'transcripts'
    transcript_dir.mkdir()
    (transcript_dir / 'content-session-fixture-001.jsonl').write_text(
        '{"type":"message","text":"sanitized"}\n',
    )
    (f_claude_mem_fixture / '.env').write_text('OPENAI_API_KEY=sk-test_secret_not_reported\n')
    (f_claude_mem_fixture / 'chroma').mkdir()
    (f_claude_mem_fixture / 'chroma-db').mkdir()

    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute('CREATE VIRTUAL TABLE observations_fts USING fts5(text)')
        connection.execute("INSERT INTO observations_fts(rowid, text) VALUES (1, 'sanitized index row')")

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

    unsupported = {(entry['source_type'], entry['source_id'], entry['reason']) for entry in report['unsupported']}
    assert (
        'source_artifact',
        'transcripts/content-session-fixture-001.jsonl',
        'raw_jsonl_transcript_replay_deferred',
    ) in unsupported
    assert ('source_artifact', '.env', 'source_secret_file_not_read') in unsupported
    assert ('source_artifact', 'vector-db', 'vector_store_import_deferred') in unsupported
    assert ('source_artifact', 'chroma', 'vector_store_import_deferred') in unsupported
    assert ('source_artifact', 'chroma-db', 'vector_store_import_deferred') in unsupported
    assert ('schema_versions', 'schema_versions:1', 'schema_housekeeping_table') in unsupported
    assert ('observations_fts', 'observations_fts', 'sqlite_fts_housekeeping_table') in unsupported
    assert 'sk-test_secret_not_reported' not in str(report)


@pytest.mark.django_db
def test_claude_mem_importer_imports_observations_and_summaries_as_approved_memory_documents(
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

    assert report['mode'] == 'apply'
    assert report['created'] == {
        'agents': 1,
        'sessions': 1,
        'raw_events': 3,
        'observations': 2,
        'memory_candidates': 2,
        'memories': 2,
        'memory_versions': 2,
        'retrieval_documents': 2,
    }
    assert report['duplicates'] == {
        'sessions': 0,
        'raw_events': 0,
        'observations': 0,
        'memories': 0,
    }

    session = AgentSession.objects.get()
    assert session.organization_id == f_import_scope.organization.id
    assert session.project_id == f_import_scope.project.id
    assert session.team_id == f_import_scope.team.id
    assert session.external_session_id == 'claude-mem:fixture-store:sdk_session:content-session-fixture-001'
    assert session.content_session_id == 'content-session-fixture-001'
    assert session.memory_session_id == 'memory-session-fixture-001'
    assert session.repository_root == '/workspace/example-repo'
    assert session.cwd == '/workspace/example-repo'
    assert session.metadata['upstream_branch_unavailable'] is True

    assert Observation.objects.count() == 2
    assert ObservationSource.objects.count() == 2
    assert MemoryCandidate.objects.filter(status=CandidateStatus.PROMOTED).count() == 2
    assert Memory.objects.filter(status=MemoryStatus.APPROVED).count() == 2
    assert MemoryVersion.objects.count() == 2
    assert RetrievalDocument.objects.count() == 2
    assert set(RawEventEnvelope.objects.values_list('event_type', flat=True)) == {
        'claude_mem.observation',
        'claude_mem.session_summary',
        'claude_mem.user_prompt',
    }
    assert set(ObservationSource.objects.values_list('source_id', flat=True)) == {
        'claude-mem:fixture-store:observation:memory-session-fixture-001:1',
        'claude-mem:fixture-store:session_summary:memory-session-fixture-001:1',
    }
    assert set(Memory.objects.values_list('metadata__source', flat=True)) == {'claude_mem_import'}
    assert all(document.full_text for document in RetrievalDocument.objects.all())


@pytest.mark.django_db
def test_claude_mem_importer_preserves_full_source_ids_with_bounded_citations(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    source_store_id = 'fixture-store-' + ('x' * 70)
    expected_source_ids = {
        f'claude-mem:{source_store_id}:observation:memory-session-fixture-001:1',
        f'claude-mem:{source_store_id}:session_summary:memory-session-fixture-001:1',
    }

    report = ClaudeMemImporter().execute(
        ClaudeMemImportInput(
            source_root=f_claude_mem_fixture,
            organization_id=f_import_scope.organization.id,
            project_id=f_import_scope.project.id,
            team_id=f_import_scope.team.id,
            source_store_id=source_store_id,
            apply=True,
        ),
    )

    assert report['created']['observations'] == 2
    assert set(ObservationSource.objects.values_list('source_id', flat=True)) == expected_source_ids
    assert all(len(source.source_id) > 80 for source in ObservationSource.objects.all())
    assert all(0 < len(source.citation) <= 80 for source in ObservationSource.objects.all())
    assert list(ObservationSource.objects.order_by('source_id').values_list('citation', flat=True)) == [
        'claude_mem:19c9f3291eef6cca',
        'claude_mem:9d3847d6cf989503',
    ]


@pytest.mark.django_db
def test_claude_mem_importer_preserves_session_branch_from_metadata(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute('ALTER TABLE sdk_sessions ADD COLUMN metadata TEXT')
        connection.execute(
            'UPDATE sdk_sessions SET metadata = ? WHERE id = 1',
            ('{"git_branch": "feature/import-branch"}',),
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

    session = AgentSession.objects.get()
    assert session.branch == 'feature/import-branch'
    assert session.metadata['upstream_branch_source'] == 'git_branch'
    assert 'upstream_branch_unavailable' not in session.metadata


@pytest.mark.django_db
def test_claude_mem_importer_is_idempotent_for_rerun(
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

    first_report = ClaudeMemImporter().execute(import_input)
    second_report = ClaudeMemImporter().execute(import_input)

    assert first_report['created']['memories'] == 2
    assert second_report['created'] == {
        'agents': 0,
        'sessions': 0,
        'raw_events': 0,
        'observations': 0,
        'memory_candidates': 0,
        'memories': 0,
        'memory_versions': 0,
        'retrieval_documents': 0,
    }
    assert second_report['duplicates'] == {
        'sessions': 1,
        'raw_events': 3,
        'observations': 2,
        'memories': 2,
    }
    assert AgentSession.objects.count() == 1
    assert RawEventEnvelope.objects.count() == 3
    assert Observation.objects.count() == 2
    assert MemoryCandidate.objects.count() == 2
    assert Memory.objects.count() == 2
    assert MemoryVersion.objects.count() == 2
    assert RetrievalDocument.objects.count() == 2


@pytest.mark.django_db
def test_claude_mem_importer_preserves_prompt_rows_as_raw_events_without_promoting_them(
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

    source_id = 'claude-mem:fixture-store:user_prompt:content-session-fixture-001:1:1'
    raw_event = RawEventEnvelope.objects.get(client_event_id=source_id)
    assert raw_event.event_type == 'claude_mem.user_prompt'
    assert raw_event.idempotency_key == source_id
    assert raw_event.payload['prompt_text'] == 'Please verify redaction of [REDACTED] in fixture import.'
    assert Observation.objects.filter(raw_event=raw_event).count() == 0
    assert ObservationSource.objects.filter(raw_event=raw_event).count() == 0
    assert MemoryVersion.objects.filter(source_observation__raw_event=raw_event).count() == 0


@pytest.mark.django_db
def test_claude_mem_importer_reports_unsupported_records_with_source_ids_and_reasons(
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

    unsupported = {(entry['source_type'], entry['source_id'], entry['reason']) for entry in report['unsupported']}
    assert ('pending_messages', 'pending_messages:1', 'transient_local_worker_queue') in unsupported
    assert ('observation_feedback', 'observation_feedback:1', 'observation_feedback_deferred') in unsupported
    assert ('source_artifact', 'transcript-watch.json', 'transcript_watcher_config_deferred') in unsupported
    assert ('source_artifact', 'corpora', 'corpora_import_deferred') in unsupported


@pytest.mark.django_db
def test_claude_mem_importer_redacts_token_shaped_values_before_persisting_or_reporting(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    fake_token = 'sk-test_fake_import_token_1234567890'
    with sqlite3.connect(f_claude_mem_fixture / 'claude-mem.db') as connection:
        connection.execute(
            'UPDATE sdk_sessions SET user_prompt = user_prompt || ? WHERE id = 1',
            (f' {fake_token}',),
        )
        connection.execute(
            'UPDATE observations SET text = text || ?, title = title || ? WHERE id = 1',
            (f' {fake_token}', f' {fake_token}'),
        )
        connection.execute(
            'UPDATE session_summaries SET learned = learned || ? WHERE id = 1',
            (f' {fake_token}',),
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

    assert report['redactions'] == {'redacted': True}
    assert fake_token not in str(report)
    assert fake_token not in str(list(AgentSession.objects.values('metadata')))
    assert fake_token not in str(list(RawEventEnvelope.objects.values('payload', 'metadata')))
    assert fake_token not in str(list(Observation.objects.values('title', 'body', 'source_metadata')))
    assert fake_token not in str(list(Memory.objects.values('title', 'body', 'metadata')))
    assert '[REDACTED]' in RawEventEnvelope.objects.get(event_type='claude_mem.user_prompt').payload['prompt_text']
    assert '[REDACTED]' in Observation.objects.get(observation_type='discovery').title
    assert '[REDACTED]' in Memory.objects.get(title__startswith='Fixture import mapping').body


@pytest.mark.django_db
def test_claude_mem_importer_rejects_cross_scope_team_before_writes(
    f_import_scope: ImportScope,
    f_claude_mem_fixture: Path,
) -> None:
    other_organization = Organization.objects.create(name='Other Org', slug='other-org')
    other_team = Team.objects.create(organization=other_organization, name='Other Team', slug='other-team')

    with pytest.raises(ClaudeMemImportError, match='team does not exist in organization'):
        ClaudeMemImporter().execute(
            ClaudeMemImportInput(
                source_root=f_claude_mem_fixture,
                organization_id=f_import_scope.organization.id,
                project_id=f_import_scope.project.id,
                team_id=other_team.id,
                source_store_id='fixture-store',
                apply=True,
            ),
        )

    assert AgentSession.objects.count() == 0
    assert RawEventEnvelope.objects.count() == 0
    assert Observation.objects.count() == 0
    assert Memory.objects.count() == 0
