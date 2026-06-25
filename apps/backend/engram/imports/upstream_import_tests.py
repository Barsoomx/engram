from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from engram.core.models import (
    AgentSession,
    Memory,
    Observation,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
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
