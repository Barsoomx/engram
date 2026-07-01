from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from engram.core.domain.usecases.errors import DomainError
from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    Runtime,
    SessionStatus,
    Team,
    VisibilityScope,
)
from engram.core.redaction import RedactionResult, redact_value
from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput

ImportReport = dict[str, object]


class ClaudeMemImportError(DomainError):
    default_error_code = 'claude_mem_import_error'


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
        'settings.json': 'settings_secret_file_not_read',
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
    _project_queries = {
        'sdk_sessions': 'SELECT DISTINCT project FROM sdk_sessions WHERE project IS NOT NULL',
        'observations': 'SELECT DISTINCT project FROM observations WHERE project IS NOT NULL',
        'session_summaries': 'SELECT DISTINCT project FROM session_summaries WHERE project IS NOT NULL',
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
            import_rows = self._import_rows(connection, detected_tables)
            counts = self._count_tables(connection, detected_tables, import_rows)
            unsupported = self._unsupported_tables(connection, all_tables)
            self._validate_single_source_project(connection, detected_tables)

        report['source'] = {
            **report['source'],
            'detected_tables': detected_tables,
            'schema_versions': schema_versions,
        }
        report['counts'] = counts
        report['unsupported'] = unsupported + self._unsupported_artifacts(source_root)
        if import_input.apply:
            self._apply_import(import_input, import_rows, report)

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
        connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row

        return connection

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
        import_rows: dict[str, list[dict[str, object]]],
    ) -> dict[str, dict[str, int]]:
        counts = self._zero_counts()
        session_keys = self._session_keys(import_rows.get('sdk_sessions', []))
        for table, fields in self._expected_counts.items():
            if table not in detected_tables:
                continue

            row_count = self._table_row_count(connection, table)
            for field in fields:
                if field == 'seen' or field == 'unsupported':
                    counts[table][field] = row_count
                else:
                    counts[table][field] = self._importable_count(table, import_rows, session_keys)

        return counts

    def _session_keys(self, session_rows: list[dict[str, object]]) -> set[str]:
        keys: set[str] = set()
        for row in session_rows:
            content_session_id = str(row.get('content_session_id') or '')
            if content_session_id:
                keys.add(content_session_id)
            memory_session_id = str(row.get('memory_session_id') or '')
            if memory_session_id:
                keys.add(memory_session_id)

        return keys

    def _importable_count(
        self,
        table: str,
        import_rows: dict[str, list[dict[str, object]]],
        session_keys: set[str],
    ) -> int:
        if table == 'sdk_sessions':
            return len(import_rows.get('sdk_sessions', []))

        if table == 'user_prompts':
            return sum(
                1
                for row in import_rows.get('user_prompts', [])
                if str(row.get('content_session_id') or '') in session_keys
            )

        if table in ('observations', 'session_summaries'):
            return sum(
                1 for row in import_rows.get(table, []) if str(row.get('memory_session_id') or '') in session_keys
            )

        return 0

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

    def _validate_single_source_project(
        self,
        connection: sqlite3.Connection,
        detected_tables: list[str],
    ) -> None:
        projects: set[str] = set()
        for table in ('sdk_sessions', 'observations', 'session_summaries'):
            if table not in detected_tables:
                continue

            cursor = connection.execute(self._project_queries[table])
            projects.update(str(row[0]).strip() for row in cursor.fetchall() if str(row[0]).strip())

        if len(projects) > 1:
            raise ClaudeMemImportError('source contains multiple projects')

    def _import_rows(
        self,
        connection: sqlite3.Connection,
        detected_tables: list[str],
    ) -> dict[str, list[dict[str, object]]]:
        return {
            'sdk_sessions': self._rows(
                connection, detected_tables, 'sdk_sessions', 'SELECT * FROM sdk_sessions ORDER BY id'
            ),
            'user_prompts': self._rows(
                connection, detected_tables, 'user_prompts', 'SELECT * FROM user_prompts ORDER BY id'
            ),
            'observations': self._rows(
                connection, detected_tables, 'observations', 'SELECT * FROM observations ORDER BY id'
            ),
            'session_summaries': self._rows(
                connection,
                detected_tables,
                'session_summaries',
                'SELECT * FROM session_summaries ORDER BY id',
            ),
        }

    def _rows(
        self,
        connection: sqlite3.Connection,
        detected_tables: list[str],
        table: str,
        query: str,
    ) -> list[dict[str, object]]:
        if table not in detected_tables:
            return []

        cursor = connection.execute(query)

        return [dict(row) for row in cursor.fetchall()]

    def _apply_import(
        self,
        import_input: ClaudeMemImportInput,
        rows: dict[str, list[dict[str, object]]],
        report: ImportReport,
    ) -> None:
        organization = Organization.objects.get(id=import_input.organization_id)
        project = Project.objects.get(organization=organization, id=import_input.project_id)
        team = None
        if import_input.team_id is not None:
            team = Team.objects.get(organization=organization, id=import_input.team_id)

        redacted = False
        with transaction.atomic():
            sessions, agents_created, sessions_created, session_duplicates, sessions_redacted = self._import_sessions(
                import_input,
                organization,
                project,
                team,
                rows['sdk_sessions'],
                rows['observations'],
            )
            report['created']['agents'] = agents_created
            report['created']['sessions'] = sessions_created
            report['duplicates']['sessions'] = session_duplicates
            redacted = redacted or sessions_redacted

            for prompt in rows['user_prompts']:
                raw_event, created, prompt_result, prompt_unsupported = self._import_prompt(
                    import_input,
                    organization,
                    project,
                    team,
                    sessions,
                    prompt,
                )
                redacted = redacted or prompt_result.redacted
                if prompt_unsupported is not None:
                    report['unsupported'].append(prompt_unsupported)
                if raw_event is None:
                    continue
                if created:
                    report['created']['raw_events'] += 1
                else:
                    report['duplicates']['raw_events'] += 1

            for observation in rows['observations']:
                result = self._import_observation_memory(
                    import_input,
                    organization,
                    project,
                    team,
                    sessions,
                    observation,
                )
                redacted = redacted or result['redacted']
                self._record_memory_result(report, result)

            for summary in rows['session_summaries']:
                result = self._import_summary_memory(
                    import_input,
                    organization,
                    project,
                    team,
                    sessions,
                    summary,
                )
                redacted = redacted or result['redacted']
                self._record_memory_result(report, result)

        if redacted:
            report['redactions'] = {'redacted': True}

    def _import_sessions(
        self,
        import_input: ClaudeMemImportInput,
        organization: Organization,
        project: Project,
        team: Team | None,
        session_rows: list[dict[str, object]],
        observation_rows: list[dict[str, object]],
    ) -> tuple[dict[str, AgentSession], int, int, int, bool]:
        sessions = {}
        agents_created = 0
        sessions_created = 0
        session_duplicates = 0
        sessions_redacted = False
        for row in session_rows:
            runtime = self._runtime(row.get('platform_source'))
            agent_external_id_result = redact_value(self._agent_external_id(import_input, row, observation_rows))
            agent_external_id = str(agent_external_id_result.value)[:255]
            branch, branch_metadata = self._session_branch(row)
            session_metadata_result = redact_value(self._session_metadata(row, branch_metadata))
            sessions_redacted = (
                sessions_redacted or agent_external_id_result.redacted or session_metadata_result.redacted
            )
            agent, agent_created = Agent.objects.get_or_create(
                organization=organization,
                runtime=runtime,
                external_id=agent_external_id,
                defaults={
                    'display_name': agent_external_id,
                    'metadata': {'source': 'claude_mem_import', 'source_store_id': import_input.source_store_id},
                },
            )
            if agent_created:
                agents_created += 1

            external_session_id = self._session_source_id(import_input, row['content_session_id'])
            session, session_created = AgentSession.objects.get_or_create(
                organization=organization,
                project=project,
                external_session_id=external_session_id,
                defaults={
                    'team': team,
                    'agent': agent,
                    'content_session_id': str(row.get('content_session_id') or ''),
                    'memory_session_id': str(row.get('memory_session_id') or ''),
                    'runtime': runtime,
                    'platform_source': str(row.get('platform_source') or ''),
                    'repository_root': str(row.get('project') or project.repository_root),
                    'cwd': str(row.get('project') or project.repository_root),
                    'branch': branch,
                    'status': self._session_status(row.get('status')),
                    'prompt_counter': int(row.get('prompt_counter') or 0),
                    'metadata': session_metadata_result.value,
                    'started_at': self._datetime(row.get('started_at')),
                    'ended_at': self._datetime(row.get('completed_at')),
                },
            )
            if session_created:
                sessions_created += 1
            else:
                session_duplicates += 1
            sessions[str(row.get('content_session_id') or '')] = session
            sessions[str(row.get('memory_session_id') or '')] = session

        return sessions, agents_created, sessions_created, session_duplicates, sessions_redacted

    def _import_prompt(
        self,
        import_input: ClaudeMemImportInput,
        organization: Organization,
        project: Project,
        team: Team | None,
        sessions: dict[str, AgentSession],
        row: dict[str, object],
    ) -> tuple[RawEventEnvelope | None, bool, RedactionResult, dict[str, str] | None]:
        session = sessions.get(str(row.get('content_session_id') or ''))
        source_id = self._prompt_source_id(import_input, row)
        payload_result = redact_value(
            {
                'source_id': source_id,
                'content_session_id': row.get('content_session_id'),
                'prompt_number': row.get('prompt_number'),
                'prompt_text': row.get('prompt_text'),
                'created_at': row.get('created_at'),
            },
        )
        if session is None:
            return (
                None,
                False,
                payload_result,
                {
                    'source_type': 'user_prompts',
                    'source_id': source_id,
                    'reason': 'missing_source_session',
                },
            )

        raw_event, created = self._get_or_create_raw_event(
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type='claude_mem.user_prompt',
            occurred_at=self._datetime(row.get('created_at')),
            payload=payload_result.value,
            redacted=payload_result.redacted,
        )

        return raw_event, created, payload_result, None

    def _import_observation_memory(
        self,
        import_input: ClaudeMemImportInput,
        organization: Organization,
        project: Project,
        team: Team | None,
        sessions: dict[str, AgentSession],
        row: dict[str, object],
    ) -> dict[str, object]:
        source_id = self._observation_source_id(import_input, row)
        body = str(row.get('text') or '')
        title = str(row.get('title') or row.get('type') or 'Imported observation')
        source_metadata = {
            'source': 'claude_mem_import',
            'source_id': source_id,
            'upstream_row_id': row.get('id'),
            'memory_session_id': row.get('memory_session_id'),
            'event_type': 'claude_mem.observation',
            'metadata': self._json_value(row.get('metadata'), {}),
        }
        payload_result = redact_value({**row, 'source_id': source_id})
        observation_result = redact_value(
            {
                'title': title,
                'subtitle': row.get('subtitle') or '',
                'body': body,
                'facts': self._json_value(row.get('facts'), []),
                'narrative': row.get('narrative') or '',
                'concepts': self._json_value(row.get('concepts'), []),
                'files_read': self._file_paths(row.get('files_read')),
                'files_modified': self._file_paths(row.get('files_modified')),
                'source_metadata': source_metadata,
            },
        )
        session = sessions.get(str(row.get('memory_session_id') or ''))

        return self._import_memory_record(
            import_input=import_input,
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type='claude_mem.observation',
            observation_type=str(row.get('type') or 'observation'),
            occurred_at=self._datetime(row.get('created_at')),
            prompt_number=row.get('prompt_number'),
            generated_model=str(row.get('generated_by_model') or ''),
            payload=payload_result.value,
            payload_redacted=payload_result.redacted,
            observation_data=observation_result.value,
            observation_redacted=observation_result.redacted,
            unsupported_source_type='observations',
        )

    def _import_summary_memory(
        self,
        import_input: ClaudeMemImportInput,
        organization: Organization,
        project: Project,
        team: Team | None,
        sessions: dict[str, AgentSession],
        row: dict[str, object],
    ) -> dict[str, object]:
        source_id = self._summary_source_id(import_input, row)
        request = str(row.get('request') or '')
        title = f'Session summary: {request}'[:255] if request else 'Session summary'
        body = self._summary_body(row)
        payload_result = redact_value({**row, 'source_id': source_id})
        observation_result = redact_value(
            {
                'title': title,
                'subtitle': '',
                'body': body,
                'facts': [],
                'narrative': body,
                'concepts': ['session_summary'],
                'files_read': self._file_paths(row.get('files_read')),
                'files_modified': self._file_paths(row.get('files_edited')),
                'source_metadata': {
                    'source': 'claude_mem_import',
                    'source_id': source_id,
                    'upstream_row_id': row.get('id'),
                    'memory_session_id': row.get('memory_session_id'),
                    'event_type': 'claude_mem.session_summary',
                },
            },
        )
        session = sessions.get(str(row.get('memory_session_id') or ''))

        return self._import_memory_record(
            import_input=import_input,
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type='claude_mem.session_summary',
            observation_type='session_summary',
            occurred_at=self._datetime(row.get('created_at')),
            prompt_number=row.get('prompt_number'),
            generated_model='',
            payload=payload_result.value,
            payload_redacted=payload_result.redacted,
            observation_data=observation_result.value,
            observation_redacted=observation_result.redacted,
            unsupported_source_type='session_summaries',
        )

    def _import_memory_record(
        self,
        import_input: ClaudeMemImportInput,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession | None,
        source_id: str,
        event_type: str,
        observation_type: str,
        occurred_at: object | None,
        prompt_number: object,
        generated_model: str,
        payload: object,
        payload_redacted: bool,
        observation_data: object,
        observation_redacted: bool,
        unsupported_source_type: str,
    ) -> dict[str, object]:
        if session is None:
            return self._unsupported_memory_result(
                redacted=payload_redacted or observation_redacted,
                source_type=unsupported_source_type,
                source_id=source_id,
                reason='missing_source_session',
            )

        if not isinstance(observation_data, dict):
            return self._empty_memory_result(payload_redacted or observation_redacted)

        if ObservationSource.objects.filter(
            organization=organization,
            project=project,
            source_type='claude_mem',
            source_id=source_id,
        ).exists():
            return self._empty_memory_result(payload_redacted or observation_redacted)

        raw_event, raw_created = self._get_or_create_raw_event(
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
            redacted=payload_redacted,
        )
        content_hash = self._content_hash(source_id, observation_data.get('title'), observation_data.get('body'))
        observation, observation_created = Observation.objects.get_or_create(
            organization=organization,
            project=project,
            session=session,
            content_hash=content_hash,
            defaults={
                'team': team,
                'agent': session.agent,
                'raw_event': raw_event,
                'observation_type': observation_type,
                'title': str(observation_data.get('title') or '')[:255],
                'subtitle': str(observation_data.get('subtitle') or '')[:255],
                'body': str(observation_data.get('body') or ''),
                'facts': observation_data.get('facts') or [],
                'narrative': str(observation_data.get('narrative') or ''),
                'concepts': observation_data.get('concepts') or [],
                'files_read': observation_data.get('files_read') or [],
                'files_modified': observation_data.get('files_modified') or [],
                'prompt_number': int(prompt_number) if prompt_number is not None else None,
                'generation_key': source_id,
                'generated_model': generated_model,
                'redaction_metadata': {'redacted': observation_redacted},
                'source_metadata': observation_data.get('source_metadata') or {},
                'observed_at': occurred_at,
            },
        )
        ObservationSource.objects.get_or_create(
            organization=organization,
            project=project,
            observation=observation,
            raw_event=raw_event,
            source_type='claude_mem',
            source_id=source_id,
            defaults={
                'citation': self._source_citation('claude_mem', source_id),
                'metadata': {'event_type': event_type},
            },
        )
        memory_result = self._promote_imported_observation(
            import_input,
            observation,
            source_id,
            event_type,
        )

        return {
            'redacted': payload_redacted or observation_redacted,
            'raw_event_created': raw_created,
            'observation_created': observation_created,
            **memory_result,
        }

    def _empty_memory_result(self, redacted: bool) -> dict[str, object]:
        return {
            'redacted': redacted,
            'raw_event_created': False,
            'observation_created': False,
            'candidate_created': False,
            'memory_created': False,
            'version_created': False,
            'retrieval_document_created': False,
        }

    def _unsupported_memory_result(
        self,
        redacted: bool,
        source_type: str,
        source_id: str,
        reason: str,
    ) -> dict[str, object]:
        result = self._empty_memory_result(redacted)
        result['count_result'] = False
        result['unsupported'] = [
            {
                'source_type': source_type,
                'source_id': source_id,
                'reason': reason,
            },
        ]

        return result

    def _get_or_create_raw_event(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession,
        source_id: str,
        event_type: str,
        occurred_at: object | None,
        payload: object,
        redacted: bool,
    ) -> tuple[RawEventEnvelope, bool]:
        metadata = {'source': 'claude_mem_import'}
        if redacted:
            metadata['redaction'] = {'payload': True}
        raw_event, created = RawEventEnvelope.objects.get_or_create(
            organization=organization,
            project=project,
            idempotency_key=source_id,
            defaults={
                'team': team,
                'agent': session.agent,
                'session': session,
                'event_type': event_type,
                'source_adapter': 'claude_mem',
                'client_event_id': source_id,
                'content_hash': self._content_hash(source_id, payload),
                'runtime': session.runtime,
                'payload_schema_version': 'v1',
                'occurred_at': occurred_at,
                'payload': payload if isinstance(payload, dict) else {'value': payload},
                'headers': {},
                'metadata': metadata,
            },
        )

        return raw_event, created

    def _promote_imported_observation(
        self,
        import_input: ClaudeMemImportInput,
        observation: Observation,
        source_id: str,
        event_type: str,
    ) -> dict[str, bool]:
        candidate_hash = self._content_hash('memory-candidate', source_id, observation.content_hash)
        candidate, candidate_created = MemoryCandidate.objects.get_or_create(
            organization=observation.organization,
            project=observation.project,
            content_hash=candidate_hash,
            defaults={
                'team': observation.team,
                'source_observation': observation,
                'title': observation.title,
                'body': observation.body or observation.title,
                'status': CandidateStatus.PROPOSED,
                'visibility_scope': VisibilityScope.PROJECT,
                'evidence': [
                    {
                        'source': 'claude_mem_import',
                        'source_id': source_id,
                        'event_type': event_type,
                        'observation_id': str(observation.id),
                        'raw_event_id': str(observation.raw_event_id) if observation.raw_event_id else '',
                    },
                ],
            },
        )
        promoted = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(candidate_id=candidate.id))
        self._mark_imported_memory(promoted.memory, import_input, source_id, event_type)

        return {
            'candidate_created': candidate_created,
            'memory_created': not promoted.duplicate,
            'version_created': not promoted.duplicate,
            'retrieval_document_created': not promoted.duplicate,
        }

    def _mark_imported_memory(
        self,
        memory: Memory,
        import_input: ClaudeMemImportInput,
        source_id: str,
        event_type: str,
    ) -> None:
        metadata = dict(memory.metadata)
        metadata.update(
            {
                'source': 'claude_mem_import',
                'source_store_id': import_input.source_store_id,
                'source_id': source_id,
                'event_type': event_type,
            },
        )
        if metadata == memory.metadata:
            return

        memory.metadata = metadata
        memory.save(update_fields=['metadata', 'updated_at'])

    def _record_memory_result(self, report: ImportReport, result: dict[str, object]) -> None:
        unsupported = result.get('unsupported')
        if isinstance(unsupported, list):
            report['unsupported'].extend(unsupported)
        if result.get('count_result') is False:
            return

        if result['raw_event_created']:
            report['created']['raw_events'] += 1
        else:
            report['duplicates']['raw_events'] += 1
        if result['observation_created']:
            report['created']['observations'] += 1
        else:
            report['duplicates']['observations'] += 1
        if result['candidate_created']:
            report['created']['memory_candidates'] += 1
        if result['memory_created']:
            report['created']['memories'] += 1
            report['created']['memory_versions'] += 1
            report['created']['retrieval_documents'] += 1
        else:
            report['duplicates']['memories'] += 1

    def _session_source_id(self, import_input: ClaudeMemImportInput, content_session_id: object) -> str:
        return f'claude-mem:{import_input.source_store_id}:sdk_session:{content_session_id}'

    def _observation_source_id(self, import_input: ClaudeMemImportInput, row: dict[str, object]) -> str:
        return f'claude-mem:{import_input.source_store_id}:observation:{row.get("memory_session_id")}:{row.get("id")}'

    def _summary_source_id(self, import_input: ClaudeMemImportInput, row: dict[str, object]) -> str:
        return (
            f'claude-mem:{import_input.source_store_id}:session_summary:{row.get("memory_session_id")}:{row.get("id")}'
        )

    def _prompt_source_id(self, import_input: ClaudeMemImportInput, row: dict[str, object]) -> str:
        return (
            f'claude-mem:{import_input.source_store_id}:user_prompt:'
            f'{row.get("content_session_id")}:{row.get("prompt_number")}:{row.get("id")}'
        )

    def _runtime(self, value: object) -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'codex', Runtime.CODEX}:
            return Runtime.CODEX
        if normalized in {'claude', 'claude_code', 'claude-code', Runtime.CLAUDE_CODE}:
            return Runtime.CLAUDE_CODE

        return Runtime.UNKNOWN

    def _agent_external_id(
        self,
        import_input: ClaudeMemImportInput,
        session_row: dict[str, object],
        observation_rows: list[dict[str, object]],
    ) -> str:
        memory_session_id = session_row.get('memory_session_id')
        for row in observation_rows:
            if row.get('memory_session_id') == memory_session_id and row.get('agent_id'):
                return str(row['agent_id'])

        return f'claude_mem:{import_input.source_store_id}'

    def _session_status(self, value: object) -> str:
        status = str(value or '').strip().lower()
        if status == 'completed':
            return SessionStatus.ENDED
        if status == 'failed':
            return SessionStatus.ERRORED

        return SessionStatus.ACTIVE

    def _session_branch(self, row: dict[str, object]) -> tuple[str, dict[str, object]]:
        upstream_metadata = self._upstream_session_metadata(row)
        for key in ('branch', 'git_branch', 'repository_branch'):
            branch = str(upstream_metadata.get(key) or '').strip()
            if branch:
                return branch[:255], {'upstream_branch_source': key}

        return '', {'upstream_branch_unavailable': True}

    def _session_metadata(self, row: dict[str, object], branch_metadata: dict[str, object]) -> dict[str, object]:
        metadata = {
            'source': 'claude_mem_import',
            'upstream_id': row.get('id'),
            'custom_title': row.get('custom_title') or '',
            'user_prompt': row.get('user_prompt') or '',
            **branch_metadata,
        }
        upstream_metadata = self._upstream_session_metadata(row)
        if upstream_metadata:
            metadata['upstream_metadata'] = upstream_metadata

        return metadata

    def _upstream_session_metadata(self, row: dict[str, object]) -> dict[str, object]:
        metadata = self._json_value(row.get('metadata'), {})
        if not isinstance(metadata, dict):
            return {}

        return metadata

    def _datetime(self, value: object) -> object | None:
        if not value:
            return None
        parsed = parse_datetime(str(value))
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed, UTC)

        return parsed

    def _json_value(self, value: object, default: object) -> object:
        if value in (None, ''):
            return default
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            return default

    def _file_paths(self, value: object) -> list[str]:
        raw_items = self._json_value(value, [])
        if not isinstance(raw_items, list):
            return []

        paths = []
        for item in raw_items:
            if isinstance(item, dict):
                path = str(item.get('path') or '').strip()
            else:
                path = str(item).strip()
            if path:
                paths.append(path)

        return paths

    def _summary_body(self, row: dict[str, object]) -> str:
        sections = [
            ('Request', row.get('request')),
            ('Investigated', row.get('investigated')),
            ('Learned', row.get('learned')),
            ('Completed', row.get('completed')),
            ('Next steps', row.get('next_steps')),
            ('Notes', row.get('notes')),
        ]

        return '\n\n'.join(f'{label}: {value}' for label, value in sections if value)

    def _content_hash(self, *values: object) -> str:
        serialized = json.dumps(values, sort_keys=True, default=str, separators=(',', ':'))

        return hashlib.sha256(serialized.encode()).hexdigest()

    def _source_citation(self, source_type: str, source_id: str) -> str:
        return f'{source_type}:{hashlib.sha256(source_id.encode()).hexdigest()[:16]}'

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
