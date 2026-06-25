from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from engram.core.models import Organization, Project, ProjectTeam, Team

ImportReport = dict[str, object]


class ClaudeMemImportError(ValueError):
    pass


@dataclass(frozen=True)
class ClaudeMemImportInput:
    source_root: Path
    organization_id: UUID
    project_id: UUID
    team_id: UUID | None = None
    source_store_id: str = ''
    apply: bool = False


class ClaudeMemImporter:
    _expected_counts = {
        'sdk_sessions': ('seen', 'importable'),
        'user_prompts': ('seen', 'importable_raw_events'),
        'observations': ('seen', 'importable_memories'),
        'session_summaries': ('seen', 'importable_memories'),
        'pending_messages': ('seen', 'unsupported'),
        'observation_feedback': ('seen', 'unsupported'),
    }
    _server_owned_tables = {
        'projects',
        'server_sessions',
        'agent_events',
        'memory_items',
        'memory_sources',
        'teams',
        'team_members',
        'api_keys',
        'audit_log',
    }
    _unsupported_table_reasons = {
        'pending_messages': 'transient_local_worker_queue',
        'observation_feedback': 'observation_feedback_deferred',
        'schema_versions': 'schema_housekeeping_table',
        'sqlite_sequence': 'schema_housekeeping_table',
    }
    _unsupported_artifact_reasons = {
        'transcript-watch.json': 'transcript_watcher_config_deferred',
        'transcript-watch-state.json': 'transcript_watcher_state_deferred',
        'corpora': 'corpora_import_deferred',
        'vector-db': 'vector_store_import_deferred',
        'chroma': 'vector_store_import_deferred',
        'chroma-db': 'vector_store_import_deferred',
        '.chroma': 'vector_store_import_deferred',
        '.env': 'source_secret_file_not_read',
    }
    _count_queries = {
        'sdk_sessions': 'SELECT COUNT(*) FROM sdk_sessions',
        'user_prompts': 'SELECT COUNT(*) FROM user_prompts',
        'observations': 'SELECT COUNT(*) FROM observations',
        'session_summaries': 'SELECT COUNT(*) FROM session_summaries',
        'pending_messages': 'SELECT COUNT(*) FROM pending_messages',
        'observation_feedback': 'SELECT COUNT(*) FROM observation_feedback',
        'projects': 'SELECT COUNT(*) FROM projects',
        'server_sessions': 'SELECT COUNT(*) FROM server_sessions',
        'agent_events': 'SELECT COUNT(*) FROM agent_events',
        'memory_items': 'SELECT COUNT(*) FROM memory_items',
        'memory_sources': 'SELECT COUNT(*) FROM memory_sources',
        'teams': 'SELECT COUNT(*) FROM teams',
        'team_members': 'SELECT COUNT(*) FROM team_members',
        'api_keys': 'SELECT COUNT(*) FROM api_keys',
        'audit_log': 'SELECT COUNT(*) FROM audit_log',
    }
    _id_queries = {
        'pending_messages': 'SELECT id FROM pending_messages ORDER BY id',
        'observation_feedback': 'SELECT id FROM observation_feedback ORDER BY id',
        'projects': 'SELECT id FROM projects ORDER BY id',
        'server_sessions': 'SELECT id FROM server_sessions ORDER BY id',
        'agent_events': 'SELECT id FROM agent_events ORDER BY id',
        'memory_items': 'SELECT id FROM memory_items ORDER BY id',
        'memory_sources': 'SELECT id FROM memory_sources ORDER BY id',
        'teams': 'SELECT id FROM teams ORDER BY id',
        'team_members': 'SELECT id FROM team_members ORDER BY id',
        'api_keys': 'SELECT id FROM api_keys ORDER BY id',
        'audit_log': 'SELECT id FROM audit_log ORDER BY id',
        'schema_versions': 'SELECT id FROM schema_versions ORDER BY id',
    }

    def execute(self, import_input: ClaudeMemImportInput) -> ImportReport:
        self._validate_target(import_input)

        source_root = Path(import_input.source_root)
        db_path = source_root / 'claude-mem.db'
        report = self._empty_report(import_input, source_root)
        if not db_path.exists():
            report['warnings'] = [
                {
                    'code': 'missing_claude_mem_db',
                    'message': f'claude-mem.db was not found under {source_root}',
                },
            ]

            return report

        with self._connect_readonly(db_path) as connection:
            detected_tables = self._detected_tables(connection)
            all_tables = self._all_tables(connection)
            schema_versions = self._schema_versions(connection, detected_tables)
            counts = self._count_tables(connection, detected_tables)
            unsupported = self._unsupported_tables(connection, all_tables)

        report['source'] = {
            **report['source'],
            'detected_tables': detected_tables,
            'schema_versions': schema_versions,
        }
        report['counts'] = counts
        report['unsupported'] = unsupported + self._unsupported_artifacts(source_root)

        return report

    def _validate_target(self, import_input: ClaudeMemImportInput) -> None:
        if not Organization.objects.filter(id=import_input.organization_id).exists():
            raise ClaudeMemImportError(f'organization does not exist: {import_input.organization_id}')

        if not Project.objects.filter(
            id=import_input.project_id,
            organization_id=import_input.organization_id,
        ).exists():
            raise ClaudeMemImportError(
                f'project does not exist in organization: {import_input.project_id}',
            )

        if import_input.team_id is None:
            return

        if not Team.objects.filter(id=import_input.team_id, organization_id=import_input.organization_id).exists():
            raise ClaudeMemImportError(f'team does not exist in organization: {import_input.team_id}')

        if not ProjectTeam.objects.filter(
            organization_id=import_input.organization_id,
            project_id=import_input.project_id,
            team_id=import_input.team_id,
        ).exists():
            raise ClaudeMemImportError(f'team is not linked to project: {import_input.team_id}')

    def _empty_report(self, import_input: ClaudeMemImportInput, source_root: Path) -> ImportReport:
        return {
            'mode': 'apply' if import_input.apply else 'dry_run',
            'source': {
                'kind': 'claude_mem',
                'source_store_id': import_input.source_store_id,
                'root': str(source_root),
                'detected_tables': [],
                'schema_versions': [],
            },
            'target': {
                'organization_id': str(import_input.organization_id),
                'project_id': str(import_input.project_id),
                'team_id': str(import_input.team_id) if import_input.team_id else None,
            },
            'counts': self._zero_counts(),
            'created': {
                'agents': 0,
                'sessions': 0,
                'raw_events': 0,
                'observations': 0,
                'memory_candidates': 0,
                'memories': 0,
                'memory_versions': 0,
                'retrieval_documents': 0,
            },
            'duplicates': {
                'sessions': 0,
                'raw_events': 0,
                'observations': 0,
                'memories': 0,
            },
            'unsupported': [],
            'warnings': [],
            'redactions': {'redacted': False},
        }

    def _zero_counts(self) -> dict[str, dict[str, int]]:
        return {table: dict.fromkeys(fields, 0) for table, fields in self._expected_counts.items()}

    def _connect_readonly(self, db_path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)

    def _detected_tables(self, connection: sqlite3.Connection) -> list[str]:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )

        return [str(row[0]) for row in cursor.fetchall()]

    def _all_tables(self, connection: sqlite3.Connection) -> list[str]:
        cursor = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")

        return [str(row[0]) for row in cursor.fetchall()]

    def _schema_versions(self, connection: sqlite3.Connection, detected_tables: list[str]) -> list[int]:
        if 'schema_versions' not in detected_tables:
            return []

        cursor = connection.execute('SELECT version FROM schema_versions ORDER BY version')

        return [int(row[0]) for row in cursor.fetchall()]

    def _count_tables(
        self,
        connection: sqlite3.Connection,
        detected_tables: list[str],
    ) -> dict[str, dict[str, int]]:
        counts = self._zero_counts()
        for table, fields in self._expected_counts.items():
            if table not in detected_tables:
                continue

            row_count = self._table_row_count(connection, table)
            for field in fields:
                counts[table][field] = row_count

        return counts

    def _unsupported_tables(
        self,
        connection: sqlite3.Connection,
        detected_tables: list[str],
    ) -> list[dict[str, str]]:
        unsupported: list[dict[str, str]] = []
        unsupported_tables = self._server_owned_tables.intersection(detected_tables)
        unsupported_tables.update(self._unsupported_table_reasons.keys() & set(detected_tables))
        unsupported_tables.update(table for table in detected_tables if self._is_fts_housekeeping_table(table))
        for table in sorted(unsupported_tables):
            reason = self._unsupported_table_reason(table)
            unsupported.extend(self._unsupported_rows(connection, table, reason))

        return unsupported

    def _unsupported_table_reason(self, table: str) -> str:
        if self._is_fts_housekeeping_table(table):
            return 'sqlite_fts_housekeeping_table'

        return self._unsupported_table_reasons.get(table, 'upstream_server_owned_table')

    def _is_fts_housekeeping_table(self, table: str) -> bool:
        return table.endswith('_fts') or '_fts_' in table

    def _unsupported_rows(self, connection: sqlite3.Connection, table: str, reason: str) -> list[dict[str, str]]:
        ids = self._table_ids(connection, table)
        if not ids:
            return [
                {
                    'source_type': table,
                    'source_id': table,
                    'reason': reason,
                },
            ]

        return [
            {
                'source_type': table,
                'source_id': f'{table}:{row_id}',
                'reason': reason,
            }
            for row_id in ids
        ]

    def _table_ids(self, connection: sqlite3.Connection, table: str) -> list[str]:
        query = self._id_queries.get(table)
        if query is None:
            return []

        cursor = connection.execute(query)

        return [str(row[0]) for row in cursor.fetchall()]

    def _table_row_count(self, connection: sqlite3.Connection, table: str) -> int:
        query = self._count_queries[table]
        cursor = connection.execute(query)

        return int(cursor.fetchone()[0])

    def _unsupported_artifacts(self, source_root: Path) -> list[dict[str, str]]:
        unsupported = []
        for relative_path, reason in self._unsupported_artifact_reasons.items():
            if not (source_root / relative_path).exists():
                continue

            unsupported.append(
                {
                    'source_type': 'source_artifact',
                    'source_id': relative_path,
                    'reason': reason,
                },
            )

        for jsonl_path in sorted(source_root.rglob('*.jsonl')):
            if not jsonl_path.is_file():
                continue

            unsupported.append(
                {
                    'source_type': 'source_artifact',
                    'source_id': jsonl_path.relative_to(source_root).as_posix(),
                    'reason': 'raw_jsonl_transcript_replay_deferred',
                },
            )

        return unsupported
