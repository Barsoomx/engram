from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
from argparse import Namespace
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from engram_cli.commands import (
    CliError,
    _ladder_project_id,
    emit_error,
    normalize_server_url,
    remediation_for,
)
from engram_cli.config import as_string, local_paths, read_json
from engram_cli.http import Transport, post_json, urllib_transport


_TABLE_ORDER = ('sdk_sessions', 'user_prompts', 'observations', 'session_summaries')
_PROJECT_TABLES = ('sdk_sessions', 'observations', 'session_summaries')
_V17_ALIASES = {
    'claude_session_id': 'content_session_id',
    'sdk_session_id': 'memory_session_id',
}
_MAX_BATCH_ROWS = 200
_MAX_BATCH_BYTES = 1_500_000
_SERVER_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_BATCH_ENVELOPE_BYTES = 256
_PAYLOAD_TOO_LARGE_STATUS = 413
_RETRYABLE_STATUS = (500, 502, 503, 504)
_BATCH_MAX_ATTEMPTS = 5
_BATCH_BACKOFF_SECONDS = 0.5


class ClaudeMemImportError(CliError):
    pass


class ImportBatchTooLargeError(ClaudeMemImportError):
    pass


class ImportConflictError(ClaudeMemImportError):
    def __init__(
        self,
        code: str,
        detail: str,
        remediation: str,
        active_import_id: str = '',
    ) -> None:
        super().__init__(code, detail, remediation)
        self.active_import_id = active_import_id


_IMPORT_REMEDIATION: dict[str, str] = {
    'missing_capability': (
        'Use an API key with memories:admin for imports '
        '(mint one on the console API Keys page).'
    ),
    'import_job_conflict': (
        'An active import exists for this store. Re-run with --replace to cancel '
        'it and start over (already-imported rows deduplicate).'
    ),
}


def _import_remediation(code: str) -> str:
    return _IMPORT_REMEDIATION.get(code, remediation_for(code))


def _read_optional_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}

    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ClaudeMemImportError(
            'invalid_response',
            f'Could not read {path.name}: {error}',
            _import_remediation('invalid_response'),
        ) from error


def default_store_id(db_path: str, hostname: str) -> str:
    digest = hashlib.sha256(f'{hostname}{db_path}'.encode()).hexdigest()

    return f'cli:{digest[:16]}'


def apply_v17_aliases(row: dict[str, object]) -> dict[str, object]:
    aliased: dict[str, object] = {}
    for key, value in row.items():
        if key in _V17_ALIASES and _V17_ALIASES[key] in row:
            continue

        aliased[_V17_ALIASES.get(key, key)] = value

    return aliased


def _row_size_bytes(row: object) -> int:
    return len(json.dumps(row)) + 1


def iter_batches(
    rows: list[dict[str, object]],
    size: int,
    max_bytes: int = _MAX_BATCH_BYTES,
) -> Iterator[list[dict[str, object]]]:
    step = max(1, size)
    batch: list[dict[str, object]] = []
    batch_bytes = _BATCH_ENVELOPE_BYTES
    for row in rows:
        row_bytes = _row_size_bytes(row)
        if batch and (len(batch) >= step or batch_bytes + row_bytes > max_bytes):
            yield batch
            batch = []
            batch_bytes = _BATCH_ENVELOPE_BYTES

        batch.append(row)
        batch_bytes += row_bytes

    if batch:
        yield batch


class ClaudeMemReader:
    def __init__(self, connection: sqlite3.Connection, db_path: str) -> None:
        self._connection = connection
        self._db_path = db_path

    @classmethod
    def open(cls, data_dir: str | Path) -> 'ClaudeMemReader':
        db_path = Path(data_dir).expanduser() / 'claude-mem.db'
        if not db_path.exists():
            raise ClaudeMemImportError(
                'missing_claude_mem_db',
                f'claude-mem.db was not found under {data_dir}',
                _import_remediation('missing_claude_mem_db'),
            )

        try:
            connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute('SELECT name FROM sqlite_master LIMIT 1')
        except sqlite3.DatabaseError as error:
            raise ClaudeMemImportError(
                'corrupt_claude_mem_db',
                'claude-mem.db could not be opened as a valid SQLite database',
                _import_remediation('corrupt_claude_mem_db'),
            ) from error

        return cls(connection, os.path.abspath(str(db_path)))

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> 'ClaudeMemReader':
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def table_exists(self, table: str) -> bool:
        cursor = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        )

        return cursor.fetchone() is not None

    def column_names(self, table: str) -> list[str]:
        if table not in _TABLE_ORDER or not self.table_exists(table):
            return []

        cursor = self._connection.execute(f'PRAGMA table_info({table})')

        return [str(row['name']) for row in cursor.fetchall()]

    def read_table(self, table: str) -> list[dict[str, object]]:
        if table not in _TABLE_ORDER or not self.table_exists(table):
            return []

        cursor = self._connection.execute(f'SELECT * FROM {table} ORDER BY rowid')

        return [apply_v17_aliases(dict(row)) for row in cursor.fetchall()]

    def distinct_projects(self) -> list[str]:
        projects: list[str] = []
        for table in _PROJECT_TABLES:
            if 'project' not in self.column_names(table):
                continue

            cursor = self._connection.execute(
                f'SELECT DISTINCT project FROM {table} WHERE project IS NOT NULL',
            )
            for row in cursor.fetchall():
                value = str(row['project'])
                if value not in projects:
                    projects.append(value)

        return sorted(projects)

    def schema_version_head(self) -> int:
        if not self.table_exists('schema_versions'):
            return 0

        cursor = self._connection.execute('SELECT MAX(version) FROM schema_versions')
        head = cursor.fetchone()[0]

        return int(head) if head is not None else 0


@dataclass(frozen=True)
class ImportPlan:
    projects: list[str]
    tables: 'OrderedDict[str, list[dict[str, object]]]'
    project_name: str | None

    @property
    def counts(self) -> dict[str, int]:
        return {table: len(rows) for table, rows in self.tables.items()}


def build_plan(
    reader: ClaudeMemReader,
    *,
    project_name: str | None,
    skip_observations: bool,
) -> ImportPlan:
    projects = reader.distinct_projects()
    sessions = reader.read_table('sdk_sessions')
    prompts = reader.read_table('user_prompts')
    observations = [] if skip_observations else reader.read_table('observations')
    summaries = reader.read_table('session_summaries')
    if project_name is not None:
        sessions = [row for row in sessions if str(row.get('project') or '') == project_name]
        content_ids = {
            str(row.get('content_session_id') or '')
            for row in sessions
            if row.get('content_session_id')
        }
        prompts = [
            row for row in prompts if str(row.get('content_session_id') or '') in content_ids
        ]
        observations = [
            row for row in observations if str(row.get('project') or '') == project_name
        ]
        summaries = [row for row in summaries if str(row.get('project') or '') == project_name]

    tables: OrderedDict[str, list[dict[str, object]]] = OrderedDict(
        (
            ('sdk_sessions', sessions),
            ('user_prompts', prompts),
            ('observations', observations),
            ('session_summaries', summaries),
        ),
    )

    return ImportPlan(projects=projects, tables=tables, project_name=project_name)


def _error_from_body(body: dict[str, object], fallback: str) -> ClaudeMemImportError:
    code = as_string(body.get('code')) or fallback
    detail = as_string(body.get('detail')) or code
    if code == 'import_job_conflict':
        return ImportConflictError(
            code,
            detail,
            _import_remediation(code),
            as_string(body.get('active_import_id')),
        )

    return ClaudeMemImportError(code, detail, _import_remediation(code))


def _too_large_from_body(body: dict[str, object]) -> ImportBatchTooLargeError:
    code = as_string(body.get('code')) or 'import_payload_too_large'
    detail = as_string(body.get('detail')) or 'import batch exceeds the maximum request size'

    return ImportBatchTooLargeError(code, detail, _import_remediation(code))


def _oversized_row_error(table: str, row: dict[str, object]) -> ClaudeMemImportError:
    row_id = row.get('id', '?') if isinstance(row, dict) else '?'
    detail = (
        f'claude-mem row id={row_id} in table {table} exceeds the server '
        f'{_SERVER_MAX_REQUEST_BYTES}-byte import limit'
    )

    return ClaudeMemImportError(
        'import_row_too_large',
        detail,
        _import_remediation('import_row_too_large'),
    )


def _post_with_retry(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    path: str,
    payload: dict[str, object],
    fallback: str,
    sleep: Callable[[float], None],
) -> dict[str, object]:
    last_body: dict[str, object] = {}
    for attempt in range(_BATCH_MAX_ATTEMPTS):
        status, body = post_json(
            transport=transport,
            server_url=server_url,
            path=path,
            api_key=api_key,
            payload=payload,
        )
        if 200 <= status < 300:
            return body

        last_body = body
        if status not in _RETRYABLE_STATUS or attempt == _BATCH_MAX_ATTEMPTS - 1:
            raise _error_from_body(body, fallback=fallback)

        sleep(_BATCH_BACKOFF_SECONDS * (attempt + 1))

    raise _error_from_body(last_body, fallback=fallback)


def create_import(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    project_id: str,
    source_store_id: str,
    manifest: dict[str, object],
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    payload: dict[str, object] = {
        'project_id': project_id,
        'source_store_id': source_store_id,
        'manifest': manifest,
    }
    body = _post_with_retry(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        path='/v1/imports/claude-mem',
        payload=payload,
        fallback='import_create_failed',
        sleep=sleep,
    )
    import_id = as_string(body.get('import_id'))
    if not import_id:
        raise ClaudeMemImportError(
            'import_create_failed',
            'Server did not return an import id',
            _import_remediation('import_create_failed'),
        )

    return import_id


def cancel_import(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    import_id: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    return _post_with_retry(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        path=f'/v1/imports/claude-mem/{import_id}/cancel',
        payload={},
        fallback='import_cancel_failed',
        sleep=sleep,
    )


def _create_import_with_replace(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    project_id: str,
    source_store_id: str,
    manifest: dict[str, object],
    replace: bool,
    stdout: TextIO,
    sleep: Callable[[float], None],
) -> str:
    try:
        return create_import(
            transport=transport,
            server_url=server_url,
            api_key=api_key,
            project_id=project_id,
            source_store_id=source_store_id,
            manifest=manifest,
            sleep=sleep,
        )
    except ImportConflictError as conflict:
        if not replace or not conflict.active_import_id:
            raise

        stdout.write(
            f'--replace: cancelling active import {conflict.active_import_id} '
            'before starting over.\n',
        )
        cancel_import(
            transport=transport,
            server_url=server_url,
            api_key=api_key,
            import_id=conflict.active_import_id,
            sleep=sleep,
        )

        return create_import(
            transport=transport,
            server_url=server_url,
            api_key=api_key,
            project_id=project_id,
            source_store_id=source_store_id,
            manifest=manifest,
            sleep=sleep,
        )


def send_batch(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    import_id: str,
    seq: int,
    table: str,
    rows: list[dict[str, object]],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    payload: dict[str, object] = {'seq': seq, 'table': table, 'rows': rows}
    last_body: dict[str, object] = {}
    for attempt in range(_BATCH_MAX_ATTEMPTS):
        status, body = post_json(
            transport=transport,
            server_url=server_url,
            path=f'/v1/imports/claude-mem/{import_id}/batches',
            api_key=api_key,
            payload=payload,
        )
        if 200 <= status < 300:
            return body

        last_body = body
        if status == _PAYLOAD_TOO_LARGE_STATUS:
            raise _too_large_from_body(body)

        if status not in _RETRYABLE_STATUS or attempt == _BATCH_MAX_ATTEMPTS - 1:
            raise _error_from_body(body, fallback='import_batch_failed')

        sleep(_BATCH_BACKOFF_SECONDS * (attempt + 1))

    raise _error_from_body(last_body, fallback='import_batch_failed')


def _send_with_split(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    import_id: str,
    seq: int,
    table: str,
    rows: list[dict[str, object]],
    stdout: TextIO,
    sleep: Callable[[float], None],
) -> int:
    pending: list[list[dict[str, object]]] = [rows]
    while pending:
        current = pending.pop(0)
        try:
            result = send_batch(
                transport=transport,
                server_url=server_url,
                api_key=api_key,
                import_id=import_id,
                seq=seq,
                table=table,
                rows=current,
                sleep=sleep,
            )
        except ImportBatchTooLargeError:
            if len(current) <= 1:
                raise _oversized_row_error(table, current[0]) from None

            mid = len(current) // 2
            pending.insert(0, current[mid:])
            pending.insert(0, current[:mid])
            continue

        stdout.write(
            f'{table}: seq={seq} rows={len(current)} '
            f'created={result.get("created")} duplicates={result.get("duplicates")}\n',
        )
        seq += 1

    return seq


def finalize_import(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    import_id: str,
    client_row_counts: dict[str, int],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    return _post_with_retry(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        path=f'/v1/imports/claude-mem/{import_id}/finalize',
        payload={'client_row_counts': client_row_counts},
        fallback='import_finalize_failed',
        sleep=sleep,
    )


def stream_plan(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    plan: ImportPlan,
    project_id: str,
    source_store_id: str,
    schema_version_head: int,
    batch_size: int,
    stdout: TextIO,
    replace: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    effective_size = min(max(1, batch_size), _MAX_BATCH_ROWS)
    manifest: dict[str, object] = {
        'schema_version_head': schema_version_head,
        'tables': dict(plan.counts),
    }
    import_id = _create_import_with_replace(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        project_id=project_id,
        source_store_id=source_store_id,
        manifest=manifest,
        replace=replace,
        stdout=stdout,
        sleep=sleep,
    )
    stdout.write(f'import_id={import_id}\n')
    seq = 0
    for table, rows in plan.tables.items():
        applied = 0
        for chunk in iter_batches(rows, effective_size):
            seq = _send_with_split(
                transport=transport,
                server_url=server_url,
                api_key=api_key,
                import_id=import_id,
                seq=seq,
                table=table,
                rows=chunk,
                stdout=stdout,
                sleep=sleep,
            )
            applied += len(chunk)

        if rows:
            stdout.write(f'{table}: streamed {applied}/{len(rows)} rows\n')

    return finalize_import(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        import_id=import_id,
        client_row_counts=dict(plan.counts),
        sleep=sleep,
    )


def resolve_data_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()

    env_dir = os.environ.get('CLAUDE_MEM_DATA_DIR')
    if env_dir:
        return Path(env_dir).expanduser()

    return Path.home() / '.claude-mem'


def run_import_claude_mem(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ''
    try:
        reader = ClaudeMemReader.open(resolve_data_dir(args.data_dir))
        with reader:
            projects = reader.distinct_projects()
            is_apply = bool(getattr(args, 'apply', False)) and not bool(
                getattr(args, 'dry_run', False),
            )
            if not is_apply:
                _print_dry_run(reader, projects, stdout)

                return 0

            project_name = (as_string(getattr(args, 'project_name', '')) or '').strip() or None
            if len(projects) > 1 and not project_name:
                _print_project_choices(projects, stderr)

                return 1

            if project_name and project_name not in projects:
                raise ClaudeMemImportError(
                    'unknown_upstream_project',
                    f'project not found in claude-mem.db: {project_name}',
                    _import_remediation('unknown_upstream_project'),
                )

            selected = project_name if project_name else (projects[0] if projects else None)
            plan = build_plan(
                reader,
                project_name=selected,
                skip_observations=bool(getattr(args, 'skip_observations', False)),
            )
            paths = local_paths(args.config_dir)
            config = _read_optional_json(paths.config)
            credentials = _read_optional_json(paths.credentials)
            api_key = (
                os.environ.get('ENGRAM_API_KEY') or as_string(credentials.get('api_key'))
            ).strip()
            if not api_key:
                raise ClaudeMemImportError(
                    'missing_credential',
                    'Set ENGRAM_API_KEY or run `engram connect` to provide an import key',
                    _import_remediation('missing_credential'),
                )

            server_url = normalize_server_url(
                os.environ.get('ENGRAM_SERVER_URL') or as_string(config.get('server_url')),
            )
            project_id = _ladder_project_id(args, config).strip()
            if not project_id:
                raise ClaudeMemImportError(
                    'missing_project',
                    'Set --project or ENGRAM_PROJECT_ID for claude-mem import',
                    _import_remediation('missing_project'),
                )

            store_id = (as_string(getattr(args, 'store_id', '')) or '').strip() or default_store_id(
                reader.db_path, socket.gethostname(),
            )
            report = stream_plan(
                transport=transport or urllib_transport,
                server_url=server_url,
                api_key=api_key,
                plan=plan,
                project_id=project_id,
                source_store_id=store_id,
                schema_version_head=reader.schema_version_head(),
                batch_size=int(getattr(args, 'batch_size', _MAX_BATCH_ROWS)),
                stdout=stdout,
                replace=bool(getattr(args, 'replace', False)),
            )
            _print_report(report, stdout)

        return 0
    except ImportConflictError as conflict:
        emit_error(stderr, conflict, api_key)
        if conflict.active_import_id:
            stderr.write(f'active_import_id: {conflict.active_import_id}\n')

        return 1
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def _print_dry_run(reader: ClaudeMemReader, projects: list[str], stdout: TextIO) -> None:
    if not projects:
        stdout.write('No claude-mem projects found in this store.\n')

        return

    stdout.write(f'Detected {len(projects)} project(s) in claude-mem store:\n')
    for project in projects:
        plan = build_plan(reader, project_name=project, skip_observations=False)
        counts = plan.counts
        stdout.write(
            f'  {project}: sdk_sessions={counts["sdk_sessions"]} '
            f'user_prompts={counts["user_prompts"]} '
            f'observations={counts["observations"]} '
            f'session_summaries={counts["session_summaries"]}\n',
        )

    if len(projects) > 1:
        stdout.write('Run with --apply --project-name <project> to import one project.\n')
    else:
        stdout.write('Run with --apply to import.\n')


def _print_project_choices(projects: list[str], stderr: TextIO) -> None:
    stderr.write('multiple_projects: claude-mem store spans more than one project.\n')
    for project in projects:
        stderr.write(f'  {project}\n')

    stderr.write('remediation: choose one with --project-name <project>.\n')


def _print_report(body: dict[str, object], stdout: TextIO) -> None:
    status = body.get('status')
    if status:
        stdout.write(f'status={status}\n')

    report = body.get('report')
    if not isinstance(report, dict):
        return

    for field in ('counts', 'created', 'duplicates', 'unsupported', 'warnings', 'redactions', 'truncations'):
        if field in report:
            stdout.write(f'{field}={report[field]}\n')
