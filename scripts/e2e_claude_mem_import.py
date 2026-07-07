from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path

from e2e_golden_path import (
    COMPOSE_DIR,
    ROOT,
    SERVER_URL,
    E2EError,
    assert_equal,
    ensure_compose_env,
    progress,
    pythonpath_env,
    required_string,
    run,
    run_json,
)

ORG_SLUG = 'engram-import-e2e'
PROJECT_ONE = '/repo/one'
PROJECT_TWO = '/repo/two'
PROJECT_LEGACY = '/repo/legacy'
HEAD_STORE = 'e2e-head-store'
PREV17_STORE = 'e2e-prev17-store'
BATCH_SIZE = 50

HEAD_SESSIONS = [
    {'content': 'head-content-1', 'memory': 'head-memory-1', 'project': PROJECT_ONE},
    {'content': 'head-content-2', 'memory': 'head-memory-2', 'project': PROJECT_ONE},
    {'content': 'head-content-3', 'memory': 'head-memory-3', 'project': PROJECT_ONE},
    {'content': 'head-content-4', 'memory': 'head-memory-4', 'project': PROJECT_TWO},
    {'content': 'head-content-5', 'memory': 'head-memory-5', 'project': PROJECT_TWO},
]
PROJECT_ONE_SESSIONS = sum(1 for row in HEAD_SESSIONS if row['project'] == PROJECT_ONE)

LEGACY_SESSIONS = [
    {'content': 'legacy-content-1', 'memory': 'legacy-memory-1', 'project': PROJECT_LEGACY},
    {'content': 'legacy-content-2', 'memory': 'legacy-memory-2', 'project': PROJECT_LEGACY},
]


def main() -> int:
    api_key = f'egk_import_e2e_{secrets.token_urlsafe(32)}'
    failed = True
    try:
        ensure_compose_env()
        progress('Clearing Compose state')
        run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key)
        progress('Starting Compose services')
        run(
            ['docker', 'compose', 'up', '-d', '--build', '--wait'],
            cwd=COMPOSE_DIR,
            secret=api_key,
        )
        with tempfile.TemporaryDirectory(prefix='engram-import-e2e-') as workdir_name:
            workdir = Path(workdir_name)
            progress('Bootstrapping import-admin scope')
            bootstrap = run_json(
                [
                    'docker',
                    'compose',
                    'exec',
                    '-T',
                    'api',
                    'python',
                    'manage.py',
                    'engram_bootstrap_import_e2e',
                    '--api-key',
                    api_key,
                    '--json',
                ],
                cwd=COMPOSE_DIR,
                secret=api_key,
            )
            project_id = required_string(bootstrap, 'project_id')
            assert_equal(bootstrap.get('organization_slug'), ORG_SLUG, 'org slug')

            cli_env = pythonpath_env()
            config_dir = workdir / 'config'
            progress('Connecting host CLI with import-admin key')
            _connect_cli(config_dir, project_id, cli_env, api_key)

            head_dir = workdir / 'head'
            head_dir.mkdir()
            _write_head_db(head_dir / 'claude-mem.db', HEAD_SESSIONS)

            prev17_dir = workdir / 'prev17'
            prev17_dir.mkdir()
            _write_pre_v17_db(prev17_dir / 'claude-mem.db', LEGACY_SESSIONS)

            progress('Asserting multi-project store requires selection')
            _assert_multi_project_requires_selection(head_dir, config_dir, cli_env, api_key)

            progress('Importing head-schema project (first apply)')
            _run_import_apply(
                head_dir,
                config_dir,
                cli_env,
                api_key,
                project_name=PROJECT_ONE,
                store_id=HEAD_STORE,
            )
            head_stats = _memory_stats(HEAD_STORE, api_key)
            _assert_head_stats(head_stats, expected_sessions=PROJECT_ONE_SESSIONS)

            progress('Re-running head import (idempotent apply)')
            _run_import_apply(
                head_dir,
                config_dir,
                cli_env,
                api_key,
                project_name=PROJECT_ONE,
                store_id=HEAD_STORE,
            )
            replay_stats = _memory_stats(HEAD_STORE, api_key)
            _assert_idempotent(head_stats, replay_stats, expected_sessions=PROJECT_ONE_SESSIONS)

            progress('Importing pre-v17 store (aliased columns)')
            _run_import_apply(
                prev17_dir,
                config_dir,
                cli_env,
                api_key,
                project_name=None,
                store_id=PREV17_STORE,
            )
            prev17_stats = _memory_stats(PREV17_STORE, api_key)
            _assert_prev17_stats(prev17_stats, expected_sessions=len(LEGACY_SESSIONS))

        progress('Claude-mem import e2e passed')
        failed = False

        return 0
    finally:
        if failed:
            progress('Import e2e failed — dumping compose logs')
            run(
                ['docker', 'compose', 'logs', '--no-color', '--tail=150'],
                cwd=COMPOSE_DIR,
                secret=api_key,
                check=False,
            )
        progress('Stopping Compose services')
        run(
            ['docker', 'compose', 'down', '-v'],
            cwd=COMPOSE_DIR,
            secret=api_key,
            check=False,
        )


def _connect_cli(config_dir: Path, project_id: str, cli_env: dict[str, str], api_key: str) -> None:
    result = run(
        [
            sys.executable,
            '-m',
            'engram_cli',
            'connect',
            '--server',
            SERVER_URL,
            '--api-key',
            api_key,
            '--project',
            project_id,
            '--agent',
            'codex',
            '--agent-version',
            'import-e2e',
            '--config-dir',
            str(config_dir),
        ],
        cwd=ROOT,
        env=cli_env,
        secret=api_key,
    )
    if 'connected Engram CLI' not in result.stdout:
        raise E2EError(f'connect did not report success: {result.stdout[-400:]}')


def _run_import_apply(
    data_dir: Path,
    config_dir: Path,
    cli_env: dict[str, str],
    api_key: str,
    *,
    project_name: str | None,
    store_id: str,
) -> None:
    args = [
        sys.executable,
        '-m',
        'engram_cli',
        'import',
        'claude-mem',
        '--data-dir',
        str(data_dir),
        '--config-dir',
        str(config_dir),
        '--store-id',
        store_id,
        '--batch-size',
        str(BATCH_SIZE),
        '--apply',
    ]
    if project_name is not None:
        args.extend(['--project-name', project_name])

    result = run(args, cwd=ROOT, env=cli_env, secret=api_key)
    if 'status=succeeded' not in result.stdout:
        raise E2EError(f'import apply did not report success: {result.stdout[-400:]}')


def _assert_multi_project_requires_selection(
    data_dir: Path,
    config_dir: Path,
    cli_env: dict[str, str],
    api_key: str,
) -> None:
    result = run(
        [
            sys.executable,
            '-m',
            'engram_cli',
            'import',
            'claude-mem',
            '--data-dir',
            str(data_dir),
            '--config-dir',
            str(config_dir),
            '--apply',
        ],
        cwd=ROOT,
        env=cli_env,
        secret=api_key,
        check=False,
    )
    if result.returncode != 1:
        raise E2EError(f'multi-project apply should exit 1, got {result.returncode}: {result.stdout[-400:]}')

    if PROJECT_ONE not in result.stderr or PROJECT_TWO not in result.stderr:
        raise E2EError(f'multi-project apply did not list both projects: {result.stderr[-400:]}')


def _assert_head_stats(stats: dict[str, object], *, expected_sessions: int) -> None:
    assert_equal(stats.get('jobs_total'), 1, 'head jobs_total')
    assert_equal(stats.get('jobs_succeeded'), 1, 'head jobs_succeeded')
    assert_equal(stats.get('sessions'), expected_sessions, 'head sessions')
    assert_equal(stats.get('observations'), expected_sessions, 'head observation memories')
    assert_equal(stats.get('observations_conf_070'), expected_sessions, 'head observation confidence 0.700')
    assert_equal(stats.get('summaries'), expected_sessions, 'head summary memories')
    assert_equal(stats.get('summaries_conf_080'), expected_sessions, 'head summary confidence 0.800')
    assert_equal(stats.get('memory_total'), expected_sessions * 2, 'head memory total')


def _assert_idempotent(
    first: dict[str, object],
    replay: dict[str, object],
    *,
    expected_sessions: int,
) -> None:
    assert_equal(replay.get('memory_total'), first.get('memory_total'), 'idempotent memory total')
    assert_equal(replay.get('observations'), expected_sessions, 'idempotent observation memories')
    assert_equal(replay.get('summaries'), expected_sessions, 'idempotent summary memories')
    assert_equal(replay.get('jobs_total'), 2, 'idempotent jobs_total')
    assert_equal(replay.get('latest_job_rows_created'), 0, 'idempotent latest job created rows')
    if not isinstance(replay.get('latest_job_rows_duplicate'), int) or replay['latest_job_rows_duplicate'] <= 0:
        raise E2EError(f'idempotent replay expected duplicate rows, got {replay.get("latest_job_rows_duplicate")}')


def _assert_prev17_stats(stats: dict[str, object], *, expected_sessions: int) -> None:
    assert_equal(stats.get('jobs_succeeded'), 1, 'prev17 jobs_succeeded')
    assert_equal(stats.get('sessions'), expected_sessions, 'prev17 sessions')
    assert_equal(stats.get('observations'), expected_sessions, 'prev17 observation memories')
    assert_equal(stats.get('observations_conf_070'), expected_sessions, 'prev17 observation confidence 0.700')
    assert_equal(stats.get('summaries'), expected_sessions, 'prev17 summary memories')
    assert_equal(stats.get('summaries_conf_080'), expected_sessions, 'prev17 summary confidence 0.800')
    assert_equal(stats.get('memory_total'), expected_sessions * 2, 'prev17 memory total')


def _memory_stats(store_id: str, secret: str) -> dict[str, object]:
    return run_json(
        [
            'docker',
            'compose',
            'exec',
            '-T',
            'api',
            'python',
            'manage.py',
            'shell',
            '-c',
            _memory_stats_query(store_id),
        ],
        cwd=COMPOSE_DIR,
        secret=secret,
    )


def _memory_stats_query(store_id: str) -> str:
    slug = json.dumps(ORG_SLUG)
    store = json.dumps(store_id)
    session_prefix = json.dumps(f'claude-mem:{store_id}:sdk_session:')

    return f"""
import json
from decimal import Decimal
from engram.core.models import AgentSession, Memory
from engram.imports.models import ImportJob

memories = Memory.objects.filter(organization__slug={slug}, metadata__source_store_id={store})
observations = memories.filter(metadata__event_type='claude_mem.observation')
summaries = memories.filter(metadata__event_type='claude_mem.session_summary')
jobs = ImportJob.objects.filter(organization__slug={slug}, source_store_id={store})
latest = jobs.order_by('-created_at').first()

print(json.dumps({{
    'memory_total': memories.count(),
    'observations': observations.count(),
    'observations_conf_070': observations.filter(confidence=Decimal('0.700')).count(),
    'summaries': summaries.count(),
    'summaries_conf_080': summaries.filter(confidence=Decimal('0.800')).count(),
    'sessions': AgentSession.objects.filter(
        organization__slug={slug}, external_session_id__startswith={session_prefix}
    ).count(),
    'jobs_total': jobs.count(),
    'jobs_succeeded': jobs.filter(status='succeeded').count(),
    'latest_job_rows_created': latest.rows_created if latest is not None else -1,
    'latest_job_rows_duplicate': latest.rows_duplicate if latest is not None else -1,
}}))
"""


def _write_head_db(db_path: Path, sessions: list[dict[str, str]]) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            '''
            CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version INTEGER, applied_at TEXT);
            CREATE TABLE sdk_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_session_id TEXT,
              memory_session_id TEXT,
              project TEXT,
              platform_source TEXT,
              user_prompt TEXT,
              started_at TEXT,
              completed_at TEXT,
              status TEXT,
              prompt_counter INTEGER,
              custom_title TEXT,
              metadata TEXT
            );
            CREATE TABLE user_prompts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_session_id TEXT,
              prompt_number INTEGER,
              prompt_text TEXT,
              created_at TEXT
            );
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_session_id TEXT,
              project TEXT,
              text TEXT,
              type TEXT,
              title TEXT,
              subtitle TEXT,
              facts TEXT,
              narrative TEXT,
              concepts TEXT,
              files_read TEXT,
              files_modified TEXT,
              prompt_number INTEGER,
              agent_id TEXT,
              generated_by_model TEXT,
              metadata TEXT,
              created_at TEXT
            );
            CREATE TABLE session_summaries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_session_id TEXT,
              project TEXT,
              request TEXT,
              investigated TEXT,
              learned TEXT,
              completed TEXT,
              next_steps TEXT,
              files_read TEXT,
              files_edited TEXT,
              notes TEXT,
              prompt_number INTEGER,
              created_at TEXT
            );
            '''
        )
        connection.execute(
            'INSERT INTO schema_versions (id, version, applied_at) VALUES (1, 17, ?)',
            ('2026-07-01T00:00:00Z',),
        )
        for index, session in enumerate(sessions, start=1):
            _insert_head_session(connection, index, session)


def _insert_head_session(connection: sqlite3.Connection, index: int, session: dict[str, str]) -> None:
    content_id = session['content']
    memory_id = session['memory']
    project = session['project']
    connection.execute(
        'INSERT INTO sdk_sessions '
        '(content_session_id, memory_session_id, project, platform_source, user_prompt, '
        'started_at, completed_at, status, prompt_counter, custom_title, metadata) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            content_id,
            memory_id,
            project,
            'codex',
            f'prompt {index}',
            '2026-07-01T09:00:00Z',
            '2026-07-01T09:10:00Z',
            'completed',
            1,
            f'Session {index}',
            '{"branch":"master"}',
        ),
    )
    connection.execute(
        'INSERT INTO user_prompts (content_session_id, prompt_number, prompt_text, created_at) '
        'VALUES (?, ?, ?, ?)',
        (content_id, 1, f'prompt text {index}', '2026-07-01T09:01:00Z'),
    )
    connection.execute(
        'INSERT INTO observations '
        '(memory_session_id, project, text, type, title, subtitle, facts, narrative, concepts, '
        'files_read, files_modified, prompt_number, agent_id, generated_by_model, metadata, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            memory_id,
            project,
            f'Observation body {index} describing an imported discovery.',
            'discovery',
            f'Imported observation {index}',
            'imported subtitle',
            '["fact-a","fact-b"]',
            'The agent recorded an imported observation narrative.',
            '["migration","import"]',
            '["src/example.py"]',
            '[]',
            1,
            'fixture-agent',
            'fake-provider/fake-model',
            '{"imported":true}',
            '2026-07-01T09:02:00Z',
        ),
    )
    connection.execute(
        'INSERT INTO session_summaries '
        '(memory_session_id, project, request, investigated, learned, completed, next_steps, '
        'files_read, files_edited, notes, prompt_number, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            memory_id,
            project,
            f'Summarize imported session {index}',
            'Reviewed the imported fixture tables.',
            'The importer promotes summaries at higher confidence.',
            'Recorded a session summary fixture.',
            'Use the fixture in the import e2e.',
            '["src/example.py"]',
            '[]',
            'Synthetic summary fixture.',
            1,
            '2026-07-01T09:08:00Z',
        ),
    )


def _write_pre_v17_db(db_path: Path, sessions: list[dict[str, str]]) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            '''
            CREATE TABLE sdk_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              claude_session_id TEXT,
              sdk_session_id TEXT,
              project TEXT,
              started_at TEXT
            );
            CREATE TABLE user_prompts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              claude_session_id TEXT,
              prompt_number INTEGER,
              prompt_text TEXT,
              created_at TEXT
            );
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sdk_session_id TEXT,
              project TEXT,
              text TEXT,
              type TEXT,
              title TEXT,
              created_at TEXT
            );
            CREATE TABLE session_summaries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sdk_session_id TEXT,
              project TEXT,
              request TEXT,
              created_at TEXT
            );
            '''
        )
        for index, session in enumerate(sessions, start=1):
            _insert_pre_v17_session(connection, index, session)


def _insert_pre_v17_session(connection: sqlite3.Connection, index: int, session: dict[str, str]) -> None:
    content_id = session['content']
    memory_id = session['memory']
    project = session['project']
    connection.execute(
        'INSERT INTO sdk_sessions (claude_session_id, sdk_session_id, project, started_at) '
        'VALUES (?, ?, ?, ?)',
        (content_id, memory_id, project, '2026-01-01T00:00:00Z'),
    )
    connection.execute(
        'INSERT INTO user_prompts (claude_session_id, prompt_number, prompt_text, created_at) '
        'VALUES (?, ?, ?, ?)',
        (content_id, 1, f'legacy prompt {index}', '2026-01-01T00:01:00Z'),
    )
    connection.execute(
        'INSERT INTO observations (sdk_session_id, project, text, type, title, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (
            memory_id,
            project,
            f'Legacy observation body {index}.',
            'discovery',
            f'Legacy observation {index}',
            '2026-01-01T00:02:00Z',
        ),
    )
    connection.execute(
        'INSERT INTO session_summaries (sdk_session_id, project, request, created_at) '
        'VALUES (?, ?, ?, ?)',
        (memory_id, project, f'Legacy summary request {index}', '2026-01-01T00:08:00Z'),
    )


if __name__ == '__main__':
    raise SystemExit(main())
