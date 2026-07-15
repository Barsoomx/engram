from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from engram.core.domain.usecases.errors import DomainError
from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryStatus,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    RawEventNormalizationDisposition,
    RawEventNormalizationReason,
    Runtime,
    SessionStatus,
    Team,
    VisibilityScope,
)
from engram.core.redaction import RedactionResult, redact_value
from engram.memory.import_provenance import (
    ImportProvenanceError,
    import_candidate_content_hash,
    import_candidate_source_anchors,
    validated_import_candidate_source,
)
from engram.memory.observation_work import allocate_observation_sequence, lock_session_for_observation
from engram.memory.services import PromoteMemoryCandidate, PromoteMemoryCandidateInput
from engram.memory.workflow_work import canonical_json_bytes

ImportReport = dict[str, object]

_MAX_OBSERVATION_TEXT_CHARS = 16000
_MAX_OBSERVATION_LIST_ITEMS = 100
_MAX_SESSION_KEY_CHARS = 255
_SOURCE_ID_DIGEST_SUFFIX_CHARS = 33
_MAX_PLATFORM_SOURCE_CHARS = 80
_MAX_OBSERVATION_TYPE_CHARS = 80
_MAX_GENERATED_MODEL_CHARS = 120
_MAX_IMPORT_SESSION_CANDIDATES = 32
_IMPORT_SESSION_SOURCE = 'claude_mem_import'


def _capped_text(value: str, cap: int) -> str:
    return value[:cap]


def _capped_session_key(value: object) -> str:
    return str(value or '')[:_MAX_SESSION_KEY_CHARS]


def _capped_list(value: object, cap: int) -> list:
    if not isinstance(value, list):
        return []

    return value[:cap]


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


@dataclass(frozen=True)
class ImportContext:
    source_store_id: str
    organization: Organization
    project: Project
    team: Team | None = None


OBSERVATION_CONFIDENCE = Decimal('0.700')
SESSION_SUMMARY_CONFIDENCE = Decimal('0.800')

TABLE_ORDER = ('sdk_sessions', 'user_prompts', 'observations', 'session_summaries')
TABLE_PHASE = {table: index for index, table in enumerate(TABLE_ORDER)}


@dataclass(frozen=True)
class BatchImportResult:
    created: int
    duplicates: int
    skipped: int
    report: ImportReport


def empty_batch_report() -> ImportReport:
    return {
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
        'truncations': {'truncated': False},
    }


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

    def import_batch(
        self,
        context: ImportContext,
        table: str,
        rows: list[dict[str, object]],
        *,
        defer_embedding: bool = True,
    ) -> BatchImportResult:
        report = self._empty_batch_report()
        with transaction.atomic():
            if table == 'sdk_sessions':
                created, duplicates, skipped = self._apply_session_batch(context, rows, report)
            elif table == 'user_prompts':
                created, duplicates, skipped = self._apply_prompt_batch(context, rows, report)
            elif table == 'observations':
                created, duplicates, skipped = self._apply_memory_batch(
                    context,
                    rows,
                    report,
                    defer_embedding,
                    kind='observation',
                )
            elif table == 'session_summaries':
                created, duplicates, skipped = self._apply_memory_batch(
                    context,
                    rows,
                    report,
                    defer_embedding,
                    kind='session_summary',
                )
            else:
                raise ClaudeMemImportError('unsupported import table')

        return BatchImportResult(created=created, duplicates=duplicates, skipped=skipped, report=report)

    def _apply_session_batch(
        self,
        context: ImportContext,
        rows: list[dict[str, object]],
        report: ImportReport,
    ) -> tuple[int, int, int]:
        sessions, agents_created, sessions_created, session_duplicates, redacted, unsupported = self._import_sessions(
            context,
            rows,
            [],
        )
        self._lock_import_sessions(list(sessions.values()), context)
        report['created']['agents'] += agents_created
        report['created']['sessions'] += sessions_created
        report['duplicates']['sessions'] += session_duplicates
        report['unsupported'].extend(unsupported)
        if redacted:
            report['redactions'] = {'redacted': True}

        return sessions_created, session_duplicates, len(unsupported)

    def _apply_prompt_batch(
        self,
        context: ImportContext,
        rows: list[dict[str, object]],
        report: ImportReport,
    ) -> tuple[int, int, int]:
        created = 0
        duplicates = 0
        skipped = 0
        sessions = [self._session_for_prompt(context, row) for row in rows]
        locked_sessions = self._lock_import_sessions(
            [session for session in sessions if session is not None],
            context,
        )
        for row, session in zip(rows, sessions, strict=True):
            if session is not None:
                session = locked_sessions[session.id]
            raw_event, was_created, prompt_result, unsupported = self._import_prompt(context, session, row)
            if prompt_result.redacted:
                report['redactions'] = {'redacted': True}
            if unsupported is not None:
                report['unsupported'].append(unsupported)
            if raw_event is None:
                skipped += 1
                continue

            if was_created:
                report['created']['raw_events'] += 1
                created += 1
            else:
                report['duplicates']['raw_events'] += 1
                duplicates += 1

        return created, duplicates, skipped

    def _apply_memory_batch(
        self,
        context: ImportContext,
        rows: list[dict[str, object]],
        report: ImportReport,
        defer_embedding: bool,
        *,
        kind: str,
    ) -> tuple[int, int, int]:
        created = 0
        duplicates = 0
        skipped = 0
        confidence = OBSERVATION_CONFIDENCE if kind == 'observation' else SESSION_SUMMARY_CONFIDENCE
        team_id = context.team.id if context.team is not None else None
        session_cache: dict[tuple[str, UUID | None, str], AgentSession | None] = {}
        sessions = []
        for row in rows:
            memory_session_id = _capped_session_key(row.get('memory_session_id'))
            cache_key = (context.source_store_id, team_id, memory_session_id)
            if cache_key not in session_cache:
                session_cache[cache_key] = self._session_for_memory(context, row)
            sessions.append(session_cache[cache_key])
        locked_sessions = self._lock_import_sessions(
            [session for session in sessions if session is not None],
            context,
        )

        for row, session in zip(rows, sessions, strict=True):
            if session is not None:
                session = locked_sessions[session.id]
            if kind == 'observation':
                result = self._import_observation_memory(
                    context,
                    session,
                    row,
                    confidence,
                    defer_embedding=defer_embedding,
                )
            else:
                result = self._import_summary_memory(
                    context,
                    session,
                    row,
                    confidence,
                    defer_embedding=defer_embedding,
                )
            if result['redacted']:
                report['redactions'] = {'redacted': True}
            if result['truncated']:
                report['truncations'] = {'truncated': True}
            self._record_memory_result(report, result)
            if result.get('count_result') is False:
                skipped += 1
            elif result.get('memory_created'):
                created += 1
            else:
                duplicates += 1

        return created, duplicates, skipped

    def _session_for_prompt(self, context: ImportContext, row: dict[str, object]) -> AgentSession | None:
        content_session_id = _capped_session_key(row.get('content_session_id'))
        if not content_session_id:
            return None

        external_session_id = self._session_source_id(context, content_session_id)
        session = (
            AgentSession.objects.select_related('agent')
            .filter(
                organization=context.organization,
                project=context.project,
                external_session_id=external_session_id,
            )
            .first()
        )
        if session is not None:
            self._validate_import_session_identity(
                session,
                context,
                external_session_id,
                content_session_id,
                None,
            )

        return session

    def _session_for_memory(self, context: ImportContext, row: dict[str, object]) -> AgentSession | None:
        memory_session_id = _capped_session_key(row.get('memory_session_id'))
        if not memory_session_id:
            return None

        namespace = f'claude-mem:{context.source_store_id}:sdk_session:'
        namespace_prefix = namespace[: _MAX_SESSION_KEY_CHARS - _SOURCE_ID_DIGEST_SUFFIX_CHARS]
        expected_team_id = context.team.id if context.team is not None else None
        scoped_sessions = AgentSession.objects.select_related('agent').filter(
            organization=context.organization,
            project=context.project,
            team_id=expected_team_id,
            memory_session_id=memory_session_id,
            external_session_id__startswith=namespace_prefix,
        )
        modern_sessions = list(
            scoped_sessions.filter(
                metadata__source=_IMPORT_SESSION_SOURCE,
                metadata__source_store_id=context.source_store_id,
            ).order_by('created_at', 'id')[: _MAX_IMPORT_SESSION_CANDIDATES + 1],
        )
        if modern_sessions:
            return self._resolve_memory_session_candidates(context, modern_sessions)

        legacy_sessions = list(
            scoped_sessions.filter(metadata__source=_IMPORT_SESSION_SOURCE)
            .exclude(metadata__has_key='source_store_id')
            .order_by('created_at', 'id')[: _MAX_IMPORT_SESSION_CANDIDATES + 1],
        )

        return self._resolve_memory_session_candidates(context, legacy_sessions)

    def _resolve_memory_session_candidates(
        self,
        context: ImportContext,
        sessions: list[AgentSession],
    ) -> AgentSession | None:
        if len(sessions) > _MAX_IMPORT_SESSION_CANDIDATES:
            raise ValueError('import session identity collision')

        matches = [
            session
            for session in sessions
            if session.external_session_id == self._session_source_id(context, session.content_session_id)
        ]
        if len(matches) > 1:
            raise ValueError('import session identity collision')

        return matches[0] if matches else None

    def _lock_import_sessions(
        self,
        sessions: list[AgentSession],
        context: ImportContext,
    ) -> dict[UUID, AgentSession]:
        locked_sessions: dict[UUID, AgentSession] = {}
        expected_team_id = context.team.id if context.team is not None else None
        session_ids = {session.id for session in sessions}
        for session_id in sorted(session_ids, key=str):
            locked = lock_session_for_observation(
                organization_id=context.organization.id,
                project_id=context.project.id,
                session_id=session_id,
            )
            if locked.team_id != expected_team_id:
                raise ValueError('import session team mismatch')

            locked_sessions[locked.id] = locked

        return locked_sessions

    def _empty_batch_report(self) -> ImportReport:
        return empty_batch_report()

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
            'truncations': {'truncated': False},
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
        context = ImportContext(
            source_store_id=import_input.source_store_id,
            organization=organization,
            project=project,
            team=team,
        )

        redacted = False
        truncated = False
        with transaction.atomic():
            sessions, agents_created, sessions_created, session_duplicates, sessions_redacted, sessions_unsupported = (
                self._import_sessions(
                    context,
                    rows['sdk_sessions'],
                    rows['observations'],
                )
            )
            report['created']['agents'] = agents_created
            report['created']['sessions'] = sessions_created
            report['duplicates']['sessions'] = session_duplicates
            report['unsupported'].extend(sessions_unsupported)
            redacted = redacted or sessions_redacted

            locked_sessions = self._lock_import_sessions(list(sessions.values()), context)
            sessions = {key: locked_sessions.get(session.id, session) for key, session in sessions.items()}

            for prompt in rows['user_prompts']:
                session = sessions.get(_capped_session_key(prompt.get('content_session_id')))
                raw_event, created, prompt_result, prompt_unsupported = self._import_prompt(context, session, prompt)
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
                session = sessions.get(_capped_session_key(observation.get('memory_session_id')))
                result = self._import_observation_memory(
                    context,
                    session,
                    observation,
                    OBSERVATION_CONFIDENCE,
                    defer_embedding=False,
                )
                redacted = redacted or result['redacted']
                truncated = truncated or result['truncated']
                self._record_memory_result(report, result)

            for summary in rows['session_summaries']:
                session = sessions.get(_capped_session_key(summary.get('memory_session_id')))
                result = self._import_summary_memory(
                    context,
                    session,
                    summary,
                    SESSION_SUMMARY_CONFIDENCE,
                    defer_embedding=False,
                )
                redacted = redacted or result['redacted']
                truncated = truncated or result['truncated']
                self._record_memory_result(report, result)

        if redacted:
            report['redactions'] = {'redacted': True}
        if truncated:
            report['truncations'] = {'truncated': True}

    def _import_sessions(
        self,
        context: ImportContext,
        session_rows: list[dict[str, object]],
        observation_rows: list[dict[str, object]],
    ) -> tuple[dict[str, AgentSession], int, int, int, bool, list[dict[str, str]]]:
        organization = context.organization
        project = context.project
        team = context.team
        sessions = {}
        agents_created = 0
        sessions_created = 0
        session_duplicates = 0
        sessions_redacted = False
        unsupported: list[dict[str, str]] = []
        ordered_rows = sorted(
            session_rows,
            key=lambda row: self._session_source_id(context, _capped_session_key(row.get('content_session_id'))),
        )
        for row in ordered_rows:
            content_session_id = _capped_session_key(row.get('content_session_id'))
            if not content_session_id:
                unsupported.append(
                    {
                        'source_type': 'sdk_sessions',
                        'source_id': f'sdk_sessions:{row.get("id")}',
                        'reason': 'missing_content_session_id',
                    },
                )
                continue

            memory_session_id = _capped_session_key(row.get('memory_session_id'))
            runtime = self._runtime(row.get('platform_source'))
            agent_external_id_result = redact_value(self._agent_external_id(context, row, observation_rows))
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
                    'metadata': {'source': 'claude_mem_import', 'source_store_id': context.source_store_id},
                },
            )
            if agent_created:
                agents_created += 1

            external_session_id = self._session_source_id(context, content_session_id)
            session_metadata = dict(session_metadata_result.value)
            session_metadata['source_store_id'] = context.source_store_id
            session, session_created = AgentSession.objects.get_or_create(
                organization=organization,
                project=project,
                external_session_id=external_session_id,
                defaults={
                    'team': team,
                    'agent': agent,
                    'content_session_id': content_session_id,
                    'memory_session_id': memory_session_id,
                    'runtime': runtime,
                    'platform_source': str(row.get('platform_source') or '')[:_MAX_PLATFORM_SOURCE_CHARS],
                    'repository_root': str(row.get('project') or project.repository_root),
                    'cwd': str(row.get('project') or project.repository_root),
                    'branch': branch,
                    'status': self._session_status(row.get('status')),
                    'prompt_counter': int(row.get('prompt_counter') or 0),
                    'observation_sequence_cursor': 0,
                    'metadata': session_metadata,
                    'started_at': self._datetime(row.get('started_at')),
                    'ended_at': self._datetime(row.get('completed_at')),
                },
            )
            if session_created:
                sessions_created += 1
            else:
                self._validate_import_session_identity(
                    session,
                    context,
                    external_session_id,
                    content_session_id,
                    memory_session_id,
                )
                session_duplicates += 1
            sessions[content_session_id] = session
            if memory_session_id:
                sessions[memory_session_id] = session

        return sessions, agents_created, sessions_created, session_duplicates, sessions_redacted, unsupported

    def _validate_import_session_identity(
        self,
        session: AgentSession,
        context: ImportContext,
        external_session_id: str,
        content_session_id: str,
        memory_session_id: str | None,
    ) -> None:
        expected_team_id = context.team.id if context.team is not None else None
        if session.team_id != expected_team_id:
            raise ValueError('import session team mismatch')

        metadata = session.metadata
        if (
            session.external_session_id != external_session_id
            or session.content_session_id != content_session_id
            or (memory_session_id is not None and session.memory_session_id != memory_session_id)
            or not isinstance(metadata, dict)
            or metadata.get('source') != _IMPORT_SESSION_SOURCE
            or ('source_store_id' in metadata and metadata.get('source_store_id') != context.source_store_id)
        ):
            raise ValueError('import session identity collision')

    def _import_prompt(
        self,
        context: ImportContext,
        session: AgentSession | None,
        row: dict[str, object],
    ) -> tuple[RawEventEnvelope | None, bool, RedactionResult, dict[str, str] | None]:
        source_id = self._prompt_source_id(context, row)
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
            organization=context.organization,
            project=context.project,
            team=context.team,
            session=session,
            source_id=source_id,
            event_type='claude_mem.user_prompt',
            occurred_at=self._datetime(row.get('created_at')),
            payload=payload_result.value,
            redacted=payload_result.redacted,
            normalization_disposition=RawEventNormalizationDisposition.NO_OP,
            normalization_reason=RawEventNormalizationReason.EVIDENCE_ONLY,
        )

        return raw_event, created, payload_result, None

    def _import_observation_memory(
        self,
        context: ImportContext,
        session: AgentSession | None,
        row: dict[str, object],
        confidence: Decimal,
        defer_embedding: bool,
    ) -> dict[str, object]:
        source_id = self._observation_source_id(context, row)
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
        return self._import_memory_record(
            context=context,
            session=session,
            source_id=source_id,
            event_type='claude_mem.observation',
            observation_type=str(row.get('type') or 'observation')[:_MAX_OBSERVATION_TYPE_CHARS],
            occurred_at=self._datetime(row.get('created_at')),
            prompt_number=row.get('prompt_number'),
            generated_model=str(row.get('generated_by_model') or '')[:_MAX_GENERATED_MODEL_CHARS],
            payload=payload_result.value,
            payload_redacted=payload_result.redacted,
            observation_data=observation_result.value,
            observation_redacted=observation_result.redacted,
            unsupported_source_type='observations',
            confidence=confidence,
            defer_embedding=defer_embedding,
        )

    def _import_summary_memory(
        self,
        context: ImportContext,
        session: AgentSession | None,
        row: dict[str, object],
        confidence: Decimal,
        defer_embedding: bool,
    ) -> dict[str, object]:
        source_id = self._summary_source_id(context, row)
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
        return self._import_memory_record(
            context=context,
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
            confidence=confidence,
            defer_embedding=defer_embedding,
        )

    def _import_memory_record(
        self,
        context: ImportContext,
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
        confidence: Decimal,
        defer_embedding: bool,
    ) -> dict[str, object]:
        organization = context.organization
        project = context.project
        team = context.team
        if session is None:
            return self._unsupported_memory_result(
                redacted=payload_redacted or observation_redacted,
                source_type=unsupported_source_type,
                source_id=source_id,
                reason='missing_source_session',
            )

        if not isinstance(observation_data, dict):
            return self._empty_memory_result(payload_redacted or observation_redacted)

        raw_content_hash = self._content_hash(source_id, payload)
        content_hash = self._content_hash(source_id, observation_data.get('title'), observation_data.get('body'))
        if self._validate_existing_import_replay(
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type=event_type,
            source_store_id=context.source_store_id,
            confidence=confidence,
        ):
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
            normalization_disposition=RawEventNormalizationDisposition.OBSERVATION,
            normalization_reason=None,
        )
        if not raw_created and raw_event.content_hash != raw_content_hash:
            raise ValueError('import raw event identity collision')

        raw_body = str(observation_data.get('body') or '')
        raw_narrative = str(observation_data.get('narrative') or '')
        raw_facts = observation_data.get('facts') or []
        raw_concepts = observation_data.get('concepts') or []
        capped_body = _capped_text(raw_body, _MAX_OBSERVATION_TEXT_CHARS)
        capped_narrative = _capped_text(raw_narrative, _MAX_OBSERVATION_TEXT_CHARS)
        capped_facts = _capped_list(raw_facts, _MAX_OBSERVATION_LIST_ITEMS)
        capped_concepts = _capped_list(raw_concepts, _MAX_OBSERVATION_LIST_ITEMS)
        truncated = (
            capped_body != raw_body
            or capped_narrative != raw_narrative
            or capped_facts != raw_facts
            or capped_concepts != raw_concepts
        )
        existing_observation = self._existing_import_observation(
            organization=organization,
            project=project,
            session=session,
            source_id=source_id,
            content_hash=content_hash,
        )

        sequence_number = self._reused_observation_sequence(existing_observation, session)
        if sequence_number is None:
            sequence_number = allocate_observation_sequence(session)
            if existing_observation is not None:
                existing_observation.session_sequence = sequence_number
                existing_observation.save(update_fields=['session_sequence'])
        if raw_event.sequence_number != sequence_number:
            raw_event.sequence_number = sequence_number
            raw_event.save(update_fields=['sequence_number'])
        if existing_observation is None:
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
                    'body': capped_body,
                    'facts': capped_facts,
                    'narrative': capped_narrative,
                    'concepts': capped_concepts,
                    'files_read': observation_data.get('files_read') or [],
                    'files_modified': observation_data.get('files_modified') or [],
                    'prompt_number': int(prompt_number) if prompt_number is not None else None,
                    'generation_key': source_id,
                    'generated_model': generated_model,
                    'redaction_metadata': {'redacted': observation_redacted},
                    'source_metadata': observation_data.get('source_metadata') or {},
                    'observed_at': occurred_at,
                    'session_sequence': sequence_number,
                },
            )
        else:
            observation = existing_observation
            observation_created = False
        if not observation_created:
            self._bind_existing_import_observation(
                observation=observation,
                raw_event=raw_event,
                team=team,
                session=session,
                source_id=source_id,
                event_type=event_type,
            )
        observation_source = self._capture_import_observation_source(
            context=context,
            observation=observation,
            raw_event=raw_event,
            source_id=source_id,
            event_type=event_type,
        )
        memory_result = self._promote_imported_observation(
            context,
            observation,
            source_id,
            event_type,
            confidence,
            defer_embedding,
            observation_source,
        )

        return {
            'redacted': payload_redacted or observation_redacted,
            'truncated': truncated,
            'raw_event_created': raw_created,
            'observation_created': observation_created,
            **memory_result,
        }

    def _capture_import_observation_source(
        self,
        *,
        context: ImportContext,
        observation: Observation,
        raw_event: RawEventEnvelope,
        source_id: str,
        event_type: str,
    ) -> ObservationSource:
        observation_source, _source_created = ObservationSource.objects.get_or_create(
            organization=context.organization,
            project=context.project,
            observation=observation,
            raw_event=raw_event,
            source_type='claude_mem',
            source_id=source_id,
            defaults={
                'citation': self._source_citation('claude_mem', source_id),
                'metadata': {
                    'source_store_id': context.source_store_id,
                    'event_type': event_type,
                },
            },
        )
        if (
            observation_source.observation_id != observation.id
            or observation_source.source_type != 'claude_mem'
            or observation_source.source_id != source_id
            or not isinstance(observation_source.metadata, dict)
            or observation_source.metadata.get('event_type') != event_type
        ):
            raise ValueError('import observation source identity collision')
        if observation_source.metadata.get('source_store_id') != context.source_store_id:
            source_metadata = dict(observation_source.metadata)
            source_metadata['source_store_id'] = context.source_store_id
            observation_source.metadata = source_metadata
            observation_source.save(update_fields=['metadata', 'updated_at'])
        return observation_source

    def _validate_existing_import_candidate_replay(
        self,
        *,
        organization: Organization,
        project: Project,
        team: Team | None,
        observation: Observation,
        source_id: str,
        source_store_id: str,
        event_type: str,
        confidence: Decimal,
    ) -> MemoryCandidate | None:
        candidate_hash = import_candidate_content_hash(source_id, observation.content_hash)
        candidates = list(
            MemoryCandidate.objects.filter(
                organization=organization,
                project=project,
                content_hash=candidate_hash,
                decision_work_contract_version=0,
            ).select_related('promoted_memory')
        )
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError('legacy import candidate identity collision')

        candidate = candidates[0]
        memory = candidate.promoted_memory
        expected_team_id = team.id if team is not None else None
        expected_body = observation.body or observation.title
        expected_metadata = {
            'source': _IMPORT_SESSION_SOURCE,
            'source_store_id': source_store_id,
            'source_id': source_id,
            'event_type': event_type,
        }
        if (
            candidate.organization_id != organization.id
            or candidate.project_id != project.id
            or observation.organization_id != organization.id
            or observation.project_id != project.id
            or observation.team_id != expected_team_id
            or candidate.team_id != expected_team_id
            or candidate.source_observation_id != observation.id
            or candidate.title != observation.title
            or candidate.body != expected_body
            or candidate.status != CandidateStatus.PROMOTED
            or candidate.visibility_scope != VisibilityScope.PROJECT
            or candidate.confidence != confidence
            or memory is None
            or memory.organization_id != organization.id
            or memory.project_id != project.id
            or memory.team_id != expected_team_id
            or memory.title != observation.title
            or memory.body != expected_body
            or memory.status != MemoryStatus.APPROVED
            or memory.visibility_scope != VisibilityScope.PROJECT
            or memory.confidence != confidence
            or memory.transition_contract_version != 0
            or memory.metadata != expected_metadata
        ):
            raise ValueError('legacy import candidate identity collision')

        return candidate

    def _existing_import_observation(
        self,
        organization: Organization,
        project: Project,
        session: AgentSession,
        source_id: str,
        content_hash: str,
    ) -> Observation | None:
        source_observations = self._source_identity_observations(organization, project, source_id)
        if len(source_observations) > 1:
            raise ValueError('import observation source identity collision')

        observation = source_observations[0] if source_observations else None
        if observation is not None and observation.content_hash != content_hash:
            raise ValueError('import observation source identity collision')
        if observation is None:
            observation = (
                Observation.objects.filter(
                    organization=organization,
                    project=project,
                    session=session,
                    content_hash=content_hash,
                    generation_key='',
                )
                .annotate(source_count=Count('sources'))
                .first()
            )
        if observation is not None and observation.source_count:
            raise ValueError('import observation source identity collision')

        return observation

    def _reused_observation_sequence(
        self,
        observation: Observation | None,
        session: AgentSession,
    ) -> int | None:
        sequence_number = observation.session_sequence if observation is not None else None
        if sequence_number is not None and sequence_number <= 0:
            raise ValueError('import observation source identity collision')
        if sequence_number is not None and session.observation_sequence_cursor < sequence_number:
            raise ValueError('import observation source identity collision')

        return sequence_number

    def _source_identity_observations(
        self,
        organization: Organization,
        project: Project,
        source_id: str,
    ) -> list[Observation]:
        return list(
            Observation.objects.filter(
                organization=organization,
                project=project,
                generation_key=source_id,
            )
            .annotate(source_count=Count('sources'))
            .order_by('id')[:2],
        )

    def _bind_existing_import_observation(
        self,
        observation: Observation,
        raw_event: RawEventEnvelope,
        team: Team | None,
        session: AgentSession,
        source_id: str,
        event_type: str,
    ) -> None:
        expected_team_id = team.id if team is not None else None
        source_metadata = observation.source_metadata
        if (
            observation.team_id != expected_team_id
            or observation.agent_id != session.agent_id
            or observation.session_id != session.id
            or observation.raw_event_id not in (None, raw_event.id)
            or observation.generation_key not in ('', source_id)
            or not isinstance(source_metadata, dict)
            or (
                source_metadata != {}
                and (source_metadata.get('source_id') != source_id or source_metadata.get('event_type') != event_type)
            )
        ):
            raise ValueError('import observation source identity collision')

        update_fields = []
        if observation.raw_event_id is None:
            observation.raw_event = raw_event
            update_fields.append('raw_event')
        if not observation.generation_key:
            observation.generation_key = source_id
            update_fields.append('generation_key')
        if source_metadata == {}:
            observation.source_metadata = {'source_id': source_id, 'event_type': event_type}
            update_fields.append('source_metadata')
        if update_fields:
            observation.save(update_fields=update_fields)

    def _empty_memory_result(self, redacted: bool) -> dict[str, object]:
        return {
            'redacted': redacted,
            'truncated': False,
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

    def _validate_existing_import_replay(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession,
        source_id: str,
        event_type: str,
        source_store_id: str,
        confidence: Decimal,
    ) -> bool:
        sources = list(
            ObservationSource.objects.select_related('observation', 'raw_event')
            .filter(
                organization=organization,
                project=project,
                source_type='claude_mem',
                source_id=source_id,
            )
            .order_by('id')[:2],
        )
        if not sources:
            return False
        if len(sources) != 1:
            raise ValueError('import observation source identity collision')

        source = sources[0]
        observation = source.observation
        raw_event = source.raw_event
        if raw_event is None:
            raise ValueError('import observation source identity collision')

        source_observations = self._source_identity_observations(organization, project, source_id)
        if len(source_observations) != 1 or source_observations[0].id != observation.id:
            raise ValueError('import observation source identity collision')

        legacy_shape = self._validate_existing_replay_identity(
            organization=organization,
            project=project,
            team=team,
            session=session,
            source=source,
            observation=observation,
            raw_event=raw_event,
            source_id=source_id,
            event_type=event_type,
        )

        self._validate_existing_import_candidate_replay(
            organization=organization,
            project=project,
            team=team,
            observation=observation,
            source_id=source_id,
            source_store_id=source_store_id,
            event_type=event_type,
            confidence=confidence,
        )

        if legacy_shape:
            self._repair_legacy_import_replay(
                raw_event=raw_event,
                observation=observation,
                session=session,
                source_id=source_id,
                event_type=event_type,
                source_store_id=source_store_id,
            )
        else:
            self._validate_existing_replay_sequence(raw_event, observation, session)

        return True

    def _validate_existing_replay_identity(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession,
        source: ObservationSource,
        observation: Observation,
        raw_event: RawEventEnvelope,
        source_id: str,
        event_type: str,
    ) -> bool:
        expected_team_id = team.id if team is not None else None
        self._validate_import_raw_immutable_identity(
            raw_event=raw_event,
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type=event_type,
        )
        if raw_event.content_hash != self._content_hash(source_id, raw_event.payload):
            raise ValueError('import observation source identity collision')

        legacy_shape = (
            raw_event.normalization_contract_version == 0
            and raw_event.normalization_disposition is None
            and raw_event.normalization_reason is None
            and raw_event.sequence_number is None
        )
        if not legacy_shape:
            self._validate_import_raw_normalization_state(
                raw_event=raw_event,
                normalization_disposition=RawEventNormalizationDisposition.OBSERVATION,
                normalization_reason=None,
            )
        if (
            source.organization_id != organization.id
            or source.project_id != project.id
            or not isinstance(source.metadata, dict)
            or source.metadata.get('event_type') != event_type
            or observation.organization_id != organization.id
            or observation.project_id != project.id
            or observation.team_id != expected_team_id
            or observation.agent_id != session.agent_id
            or observation.session_id != session.id
            or observation.raw_event_id != source.raw_event_id
            or observation.generation_key != source_id
            or not isinstance(observation.source_metadata, dict)
            or observation.source_metadata.get('source_id') != source_id
            or observation.source_metadata.get('event_type') != event_type
            or ObservationSource.objects.filter(observation=observation).count() != 1
            or ObservationSource.objects.filter(raw_event=raw_event).count() != 1
        ):
            raise ValueError('import observation source identity collision')

        if legacy_shape and (
            observation.session_sequence <= 0 or session.observation_sequence_cursor < observation.session_sequence
        ):
            raise ValueError('import observation source identity collision')

        return legacy_shape

    def _validate_existing_replay_sequence(
        self,
        raw_event: RawEventEnvelope,
        observation: Observation,
        session: AgentSession,
    ) -> None:
        if (
            observation.session_sequence <= 0
            or raw_event.sequence_number != observation.session_sequence
            or session.observation_sequence_cursor < observation.session_sequence
        ):
            raise ValueError('import observation source identity collision')

    def _repair_legacy_import_replay(
        self,
        raw_event: RawEventEnvelope,
        observation: Observation,
        session: AgentSession,
        source_id: str,
        event_type: str,
        source_store_id: str,
    ) -> None:
        sequence_number = observation.session_sequence

        metadata = dict(raw_event.metadata) if isinstance(raw_event.metadata, dict) else {}
        metadata.update(
            {
                'source': 'claude_mem_import',
                'source_store_id': source_store_id,
                'source_id': source_id,
                'event_type': event_type,
            },
        )
        raw_event.normalization_contract_version = 1
        raw_event.normalization_disposition = RawEventNormalizationDisposition.OBSERVATION
        raw_event.normalization_reason = None
        raw_event.sequence_number = sequence_number
        raw_event.metadata = metadata
        raw_event.save(
            update_fields=[
                'normalization_contract_version',
                'normalization_disposition',
                'normalization_reason',
                'sequence_number',
                'metadata',
            ],
        )

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
        normalization_disposition: RawEventNormalizationDisposition,
        normalization_reason: RawEventNormalizationReason | None,
        sequence_number: int | None = None,
    ) -> tuple[RawEventEnvelope, bool]:
        metadata = {'source': 'claude_mem_import'}
        if redacted:
            metadata['redaction'] = {'payload': True}
        content_hash = self._content_hash(source_id, payload)
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
                'content_hash': content_hash,
                'runtime': session.runtime,
                'payload_schema_version': 'v1',
                'normalization_contract_version': 1,
                'normalization_disposition': normalization_disposition,
                'normalization_reason': normalization_reason,
                'sequence_number': sequence_number,
                'occurred_at': occurred_at,
                'payload': payload if isinstance(payload, dict) else {'value': payload},
                'headers': {},
                'metadata': metadata,
            },
        )
        if not created:
            self._validate_import_raw_identity(
                raw_event=raw_event,
                organization=organization,
                project=project,
                team=team,
                session=session,
                source_id=source_id,
                event_type=event_type,
                normalization_disposition=normalization_disposition,
                normalization_reason=normalization_reason,
            )
            if ObservationSource.objects.filter(raw_event=raw_event).exists():
                raise ValueError('import observation source identity collision')

        return raw_event, created

    def _validate_import_raw_identity(
        self,
        raw_event: RawEventEnvelope,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession,
        source_id: str,
        event_type: str,
        normalization_disposition: RawEventNormalizationDisposition,
        normalization_reason: RawEventNormalizationReason | None,
    ) -> None:
        self._validate_import_raw_immutable_identity(
            raw_event=raw_event,
            organization=organization,
            project=project,
            team=team,
            session=session,
            source_id=source_id,
            event_type=event_type,
        )
        self._validate_import_raw_normalization_state(
            raw_event=raw_event,
            normalization_disposition=normalization_disposition,
            normalization_reason=normalization_reason,
        )

    def _validate_import_raw_immutable_identity(
        self,
        raw_event: RawEventEnvelope,
        organization: Organization,
        project: Project,
        team: Team | None,
        session: AgentSession,
        source_id: str,
        event_type: str,
    ) -> None:
        if (
            raw_event.organization_id != organization.id
            or raw_event.project_id != project.id
            or raw_event.source_adapter != 'claude_mem'
            or raw_event.team_id != (team.id if team is not None else None)
            or raw_event.agent_id != session.agent_id
            or raw_event.session_id != session.id
            or raw_event.event_type != event_type
            or raw_event.client_event_id != source_id
            or raw_event.idempotency_key != source_id
            or raw_event.payload_schema_version != 'v1'
            or not isinstance(raw_event.metadata, dict)
            or raw_event.metadata.get('source') != 'claude_mem_import'
        ):
            raise ValueError('import raw event identity collision')

    def _validate_import_raw_normalization_state(
        self,
        raw_event: RawEventEnvelope,
        normalization_disposition: RawEventNormalizationDisposition,
        normalization_reason: RawEventNormalizationReason | None,
    ) -> None:
        if (
            raw_event.normalization_contract_version != 1
            or raw_event.normalization_disposition != normalization_disposition
            or raw_event.normalization_reason != normalization_reason
            or (
                normalization_disposition == RawEventNormalizationDisposition.NO_OP
                and raw_event.sequence_number is not None
            )
        ):
            raise ValueError('import raw event identity collision')

    def _promote_imported_observation(
        self,
        context: ImportContext,
        observation: Observation,
        source_id: str,
        event_type: str,
        confidence: Decimal,
        defer_embedding: bool,
        observation_source: ObservationSource,
    ) -> dict[str, bool]:
        candidate_hash = import_candidate_content_hash(source_id, observation.content_hash)
        legacy_candidate = self._validate_existing_import_candidate_replay(
            organization=observation.organization,
            project=observation.project,
            team=context.team,
            observation=observation,
            source_id=source_id,
            source_store_id=context.source_store_id,
            event_type=event_type,
            confidence=confidence,
        )
        if legacy_candidate is not None:
            return {
                'candidate_created': False,
                'memory_created': False,
                'version_created': False,
                'retrieval_document_created': False,
            }
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
                'confidence': confidence,
                'evidence': [
                    {
                        'source': 'claude_mem_import',
                        'source_id': source_id,
                        'event_type': event_type,
                        'observation_id': str(observation.id),
                        'raw_event_id': str(observation.raw_event_id) if observation.raw_event_id else '',
                    },
                ],
                'decision_work_contract_version': 1,
            },
        )
        if not candidate_created and candidate.decision_work_contract_version == 0:
            raise ValueError('legacy import candidate identity collision')
        anchors = import_candidate_source_anchors(
            observation=observation,
            import_source=observation_source,
            source_store_id=context.source_store_id,
            event_type=event_type,
        )
        anchors_hash = hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()
        candidate_source, _candidate_source_created = MemoryCandidateSource.objects.get_or_create(
            organization=observation.organization,
            project=observation.project,
            team=observation.team,
            candidate=candidate,
            observation=observation,
            source_kind='import',
            import_source=observation_source,
            defaults={
                'anchors': anchors,
                'anchors_hash': anchors_hash,
            },
        )
        try:
            validated_import_candidate_source(candidate, sources=[candidate_source])
        except (ImportProvenanceError, ValueError, TypeError, AttributeError) as error:
            raise ValueError('import candidate source identity collision') from error
        promoted = PromoteMemoryCandidate().execute(
            PromoteMemoryCandidateInput(candidate_id=candidate.id, defer_embedding=defer_embedding),
        )

        return {
            'candidate_created': candidate_created,
            'memory_created': not promoted.duplicate,
            'version_created': not promoted.duplicate,
            'retrieval_document_created': not promoted.duplicate,
        }

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

    def _session_source_id(self, context: ImportContext, content_session_id: object) -> str:
        return self._bounded_source_id(
            f'claude-mem:{context.source_store_id}:sdk_session:{_capped_session_key(content_session_id)}',
        )

    def _observation_source_id(self, context: ImportContext, row: dict[str, object]) -> str:
        return self._bounded_source_id(
            f'claude-mem:{context.source_store_id}:observation:{row.get("memory_session_id")}:{row.get("id")}',
        )

    def _summary_source_id(self, context: ImportContext, row: dict[str, object]) -> str:
        return self._bounded_source_id(
            f'claude-mem:{context.source_store_id}:session_summary:{row.get("memory_session_id")}:{row.get("id")}',
        )

    def _prompt_source_id(self, context: ImportContext, row: dict[str, object]) -> str:
        return self._bounded_source_id(
            f'claude-mem:{context.source_store_id}:user_prompt:'
            f'{row.get("content_session_id")}:{row.get("prompt_number")}:{row.get("id")}',
        )

    def _bounded_source_id(self, source_id: str) -> str:
        if len(source_id) <= _MAX_SESSION_KEY_CHARS:
            return source_id

        digest = hashlib.sha256(source_id.encode()).hexdigest()[:32]

        return f'{source_id[: _MAX_SESSION_KEY_CHARS - _SOURCE_ID_DIGEST_SUFFIX_CHARS]}:{digest}'

    def _runtime(self, value: object) -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'codex', Runtime.CODEX}:
            return Runtime.CODEX
        if normalized in {'claude', 'claude_code', 'claude-code', Runtime.CLAUDE_CODE}:
            return Runtime.CLAUDE_CODE

        return Runtime.UNKNOWN

    def _agent_external_id(
        self,
        context: ImportContext,
        session_row: dict[str, object],
        observation_rows: list[dict[str, object]],
    ) -> str:
        memory_session_id = session_row.get('memory_session_id')
        for row in observation_rows:
            if row.get('memory_session_id') == memory_session_id and row.get('agent_id'):
                return str(row['agent_id'])

        return f'claude_mem:{context.source_store_id}'

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
