from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engram_cli import main
from engram_cli.import_claude_mem import (
    ClaudeMemImportError,
    ClaudeMemReader,
    apply_v17_aliases,
    build_plan,
    create_import,
    default_store_id,
    finalize_import,
    iter_batches,
    send_batch,
    stream_plan,
)


API_KEY = 'egk_test_import_0123456789abcdefghijklmnop'
PROJECT_ID = '11111111-1111-1111-1111-111111111111'


class RecordingTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append({'method': method, 'url': url, 'payload': payload})
        if not self._responses:
            raise AssertionError('unexpected transport call')

        return self._responses.pop(0)


def _write_head_db(db_path: Path, sessions: list[dict[str, object]]) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            '''
            CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version INTEGER, applied_at TEXT);
            CREATE TABLE sdk_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_session_id TEXT,
              memory_session_id TEXT,
              project TEXT,
              user_prompt TEXT,
              started_at TEXT
            );
            CREATE TABLE user_prompts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_session_id TEXT,
              prompt_number INTEGER,
              prompt_text TEXT
            );
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_session_id TEXT,
              project TEXT,
              type TEXT,
              title TEXT,
              facts TEXT
            );
            CREATE TABLE session_summaries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_session_id TEXT,
              project TEXT,
              request TEXT
            );
            '''
        )
        connection.execute(
            'INSERT INTO schema_versions (id, version, applied_at) VALUES (1, 17, ?)',
            ('2026-07-01T00:00:00Z',),
        )
        for index, session in enumerate(sessions, start=1):
            content_id = str(session['content_session_id'])
            memory_id = str(session['memory_session_id'])
            project = str(session['project'])
            connection.execute(
                'INSERT INTO sdk_sessions '
                '(content_session_id, memory_session_id, project, user_prompt, started_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (content_id, memory_id, project, f'prompt-{index}', '2026-07-01T09:00:00Z'),
            )
            connection.execute(
                'INSERT INTO user_prompts (content_session_id, prompt_number, prompt_text) '
                'VALUES (?, ?, ?)',
                (content_id, 1, f'prompt text {index}'),
            )
            connection.execute(
                'INSERT INTO observations (memory_session_id, project, type, title, facts) '
                'VALUES (?, ?, ?, ?, ?)',
                (memory_id, project, 'discovery', f'obs {index}', '["fact-a","fact-b"]'),
            )
            connection.execute(
                'INSERT INTO session_summaries (memory_session_id, project, request) '
                'VALUES (?, ?, ?)',
                (memory_id, project, f'summary {index}'),
            )


def _write_pre_v17_db(db_path: Path) -> None:
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
              prompt_text TEXT
            );
            CREATE TABLE observations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sdk_session_id TEXT,
              project TEXT,
              type TEXT,
              title TEXT
            );
            CREATE TABLE session_summaries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sdk_session_id TEXT,
              project TEXT,
              request TEXT
            );
            '''
        )
        connection.execute(
            'INSERT INTO sdk_sessions (claude_session_id, sdk_session_id, project, started_at) '
            'VALUES (?, ?, ?, ?)',
            ('content-1', 'memory-1', '/repo/one', '2026-01-01T00:00:00Z'),
        )
        connection.execute(
            'INSERT INTO user_prompts (claude_session_id, prompt_number, prompt_text) '
            'VALUES (?, ?, ?)',
            ('content-1', 1, 'old prompt'),
        )
        connection.execute(
            'INSERT INTO observations (sdk_session_id, project, type, title) VALUES (?, ?, ?, ?)',
            ('memory-1', '/repo/one', 'discovery', 'old obs'),
        )
        connection.execute(
            'INSERT INTO session_summaries (sdk_session_id, project, request) VALUES (?, ?, ?)',
            ('memory-1', '/repo/one', 'old summary'),
        )


class StoreIdTests(unittest.TestCase):
    def test_default_store_id_is_deterministic_and_prefixed(self) -> None:
        first = default_store_id('/abs/claude-mem.db', 'host-a')
        second = default_store_id('/abs/claude-mem.db', 'host-a')

        self.assertEqual(first, second)
        self.assertTrue(first.startswith('cli:'))
        self.assertEqual(len('cli:') + 16, len(first))

    def test_default_store_id_differs_by_path_and_host(self) -> None:
        base = default_store_id('/abs/claude-mem.db', 'host-a')

        self.assertNotEqual(base, default_store_id('/other/claude-mem.db', 'host-a'))
        self.assertNotEqual(base, default_store_id('/abs/claude-mem.db', 'host-b'))


class AliasTests(unittest.TestCase):
    def test_renames_old_session_columns(self) -> None:
        aliased = apply_v17_aliases(
            {'claude_session_id': 'c', 'sdk_session_id': 'm', 'project': '/x'},
        )

        self.assertEqual(
            {'content_session_id': 'c', 'memory_session_id': 'm', 'project': '/x'},
            aliased,
        )

    def test_leaves_new_names_untouched(self) -> None:
        row = {'content_session_id': 'c', 'memory_session_id': 'm'}

        self.assertEqual(row, apply_v17_aliases(row))

    def test_new_name_wins_when_both_present(self) -> None:
        aliased = apply_v17_aliases(
            {'claude_session_id': 'old', 'content_session_id': 'new'},
        )

        self.assertEqual({'content_session_id': 'new'}, aliased)


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.data_dir = Path(self._dir.name)

    def _head_reader(self) -> ClaudeMemReader:
        _write_head_db(
            self.data_dir / 'claude-mem.db',
            [
                {'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'},
                {'content_session_id': 'c2', 'memory_session_id': 'm2', 'project': '/repo/two'},
            ],
        )
        reader = ClaudeMemReader.open(self.data_dir)
        self.addCleanup(reader.close)

        return reader

    def test_missing_db_raises_clean_error(self) -> None:
        with self.assertRaises(ClaudeMemImportError):
            ClaudeMemReader.open(self.data_dir)

    def test_column_names_read_from_pragma(self) -> None:
        reader = self._head_reader()

        columns = reader.column_names('sdk_sessions')

        self.assertIn('content_session_id', columns)
        self.assertIn('memory_session_id', columns)
        self.assertIn('project', columns)

    def test_reads_rows_as_is_without_json_parsing(self) -> None:
        reader = self._head_reader()

        rows = reader.read_table('observations')

        self.assertEqual('["fact-a","fact-b"]', rows[0]['facts'])
        self.assertIsInstance(rows[0]['facts'], str)

    def test_distinct_projects_across_tables(self) -> None:
        reader = self._head_reader()

        self.assertEqual(['/repo/one', '/repo/two'], reader.distinct_projects())

    def test_schema_version_head(self) -> None:
        reader = self._head_reader()

        self.assertEqual(17, reader.schema_version_head())

    def test_pre_v17_columns_aliased_on_read(self) -> None:
        _write_pre_v17_db(self.data_dir / 'claude-mem.db')
        reader = ClaudeMemReader.open(self.data_dir)
        self.addCleanup(reader.close)

        session = reader.read_table('sdk_sessions')[0]
        prompt = reader.read_table('user_prompts')[0]
        observation = reader.read_table('observations')[0]

        self.assertEqual('content-1', session['content_session_id'])
        self.assertEqual('memory-1', session['memory_session_id'])
        self.assertNotIn('claude_session_id', session)
        self.assertNotIn('sdk_session_id', session)
        self.assertEqual('content-1', prompt['content_session_id'])
        self.assertEqual('memory-1', observation['memory_session_id'])

    def test_missing_table_reads_empty(self) -> None:
        reader = self._head_reader()

        with sqlite3.connect(self.data_dir / 'claude-mem.db') as connection:
            connection.execute('DROP TABLE session_summaries')

        self.assertEqual([], reader.read_table('session_summaries'))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        data_dir = Path(self._dir.name)
        _write_head_db(
            data_dir / 'claude-mem.db',
            [
                {'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'},
                {'content_session_id': 'c2', 'memory_session_id': 'm2', 'project': '/repo/two'},
            ],
        )
        self.reader = ClaudeMemReader.open(data_dir)
        self.addCleanup(self.reader.close)

    def test_single_scope_counts_all_rows(self) -> None:
        plan = build_plan(self.reader, project_name=None, skip_observations=False)

        self.assertEqual(
            {'sdk_sessions': 2, 'user_prompts': 2, 'observations': 2, 'session_summaries': 2},
            plan.counts,
        )

    def test_filters_by_project_name_including_prompt_membership(self) -> None:
        plan = build_plan(self.reader, project_name='/repo/one', skip_observations=False)

        self.assertEqual(
            {'sdk_sessions': 1, 'user_prompts': 1, 'observations': 1, 'session_summaries': 1},
            plan.counts,
        )
        self.assertEqual('c1', plan.tables['user_prompts'][0]['content_session_id'])
        self.assertEqual('/repo/one', plan.tables['observations'][0]['project'])

    def test_skip_observations_empties_that_table(self) -> None:
        plan = build_plan(self.reader, project_name=None, skip_observations=True)

        self.assertEqual(0, plan.counts['observations'])
        self.assertEqual(2, plan.counts['sdk_sessions'])

    def test_tables_are_ordered_for_streaming(self) -> None:
        plan = build_plan(self.reader, project_name=None, skip_observations=False)

        self.assertEqual(
            ['sdk_sessions', 'user_prompts', 'observations', 'session_summaries'],
            list(plan.tables.keys()),
        )


class BatchTests(unittest.TestCase):
    def test_iter_batches_splits_by_size(self) -> None:
        chunks = list(iter_batches([1, 2, 3, 4, 5], 2))

        self.assertEqual([[1, 2], [3, 4], [5]], chunks)

    def test_iter_batches_empty(self) -> None:
        self.assertEqual([], list(iter_batches([], 2)))

    def test_create_import_conflict_raises(self) -> None:
        transport = RecordingTransport([(409, {'code': 'import_conflict', 'detail': 'exists'})])

        with self.assertRaises(ClaudeMemImportError):
            create_import(
                transport=transport,
                server_url='https://engram.example',
                api_key=API_KEY,
                project_id=PROJECT_ID,
                source_store_id='cli:abc',
                manifest={'schema_version_head': 17, 'tables': {}},
            )

    def test_send_batch_retries_same_seq_on_transient(self) -> None:
        transport = RecordingTransport(
            [
                (503, {'code': 'server_unavailable'}),
                (200, {'accepted': True, 'seq': 3, 'created': 2, 'duplicates': 0, 'skipped': 0}),
            ],
        )
        sleeps: list[float] = []

        result = send_batch(
            transport=transport,
            server_url='https://engram.example',
            api_key=API_KEY,
            import_id='imp-1',
            seq=3,
            table='observations',
            rows=[{'id': 1}, {'id': 2}],
            sleep=sleeps.append,
        )

        self.assertEqual(2, result['created'])
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(3, transport.calls[0]['payload']['seq'])
        self.assertEqual(3, transport.calls[1]['payload']['seq'])
        self.assertEqual(1, len(sleeps))

    def test_send_batch_raises_on_client_error(self) -> None:
        transport = RecordingTransport([(400, {'code': 'bad_batch', 'detail': 'nope'})])

        with self.assertRaises(ClaudeMemImportError):
            send_batch(
                transport=transport,
                server_url='https://engram.example',
                api_key=API_KEY,
                import_id='imp-1',
                seq=0,
                table='sdk_sessions',
                rows=[{'id': 1}],
                sleep=lambda _seconds: None,
            )


class StreamPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        data_dir = Path(self._dir.name)
        _write_head_db(
            data_dir / 'claude-mem.db',
            [{'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'}],
        )
        self.reader = ClaudeMemReader.open(data_dir)
        self.addCleanup(self.reader.close)

    def test_streams_tables_in_order_with_increasing_seq(self) -> None:
        plan = build_plan(self.reader, project_name=None, skip_observations=False)
        transport = RecordingTransport(
            [
                (201, {'import_id': 'imp-1', 'status': 'created'}),
                (200, {'accepted': True, 'seq': 0, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 1, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 2, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 3, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'status': 'succeeded', 'report': {'created': {'memories': 2}}}),
            ],
        )
        stdout = io.StringIO()

        report = stream_plan(
            transport=transport,
            server_url='https://engram.example',
            api_key=API_KEY,
            plan=plan,
            project_id=PROJECT_ID,
            source_store_id='cli:abc',
            schema_version_head=17,
            batch_size=200,
            stdout=stdout,
            sleep=lambda _seconds: None,
        )

        batch_calls = [call for call in transport.calls if call['url'].endswith('/batches')]
        self.assertEqual(
            ['sdk_sessions', 'user_prompts', 'observations', 'session_summaries'],
            [call['payload']['table'] for call in batch_calls],
        )
        self.assertEqual([0, 1, 2, 3], [call['payload']['seq'] for call in batch_calls])
        self.assertTrue(transport.calls[0]['url'].endswith('/v1/imports/claude-mem'))
        self.assertTrue(transport.calls[-1]['url'].endswith('/finalize'))
        self.assertEqual('succeeded', report['status'])
        self.assertEqual({'created': {'memories': 2}}, report['report'])

    def test_finalize_sends_client_row_counts(self) -> None:
        transport = RecordingTransport(
            [(200, {'status': 'succeeded', 'report': {'ok': True}})],
        )

        body = finalize_import(
            transport=transport,
            server_url='https://engram.example',
            api_key=API_KEY,
            import_id='imp-1',
            client_row_counts={'sdk_sessions': 1},
        )

        self.assertEqual({'sdk_sessions': 1}, transport.calls[0]['payload']['client_row_counts'])
        self.assertEqual('succeeded', body['status'])
        self.assertEqual({'ok': True}, body['report'])


class RunImportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.data_dir = self.root / 'data'
        self.data_dir.mkdir()
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir()

    def _write_config(self) -> None:
        (self.config_dir / 'config.json').write_text(
            json.dumps({'server_url': 'https://engram.example', 'project_id': PROJECT_ID}),
            encoding='utf-8',
        )
        (self.config_dir / 'credentials.json').write_text(
            json.dumps({'api_key': API_KEY}),
            encoding='utf-8',
        )

    def _run(self, argv: list[str], transport: RecordingTransport) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main.main(argv, stdout=stdout, stderr=stderr, transport=transport)

        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_prints_per_project_counts_without_transport(self) -> None:
        _write_head_db(
            self.data_dir / 'claude-mem.db',
            [
                {'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'},
                {'content_session_id': 'c2', 'memory_session_id': 'm2', 'project': '/repo/two'},
            ],
        )
        transport = RecordingTransport([])

        code, stdout, _stderr = self._run(
            ['import', 'claude-mem', '--data-dir', str(self.data_dir)],
            transport,
        )

        self.assertEqual(0, code)
        self.assertEqual([], transport.calls)
        self.assertIn('/repo/one', stdout)
        self.assertIn('/repo/two', stdout)

    def test_apply_single_project_streams_and_finalizes(self) -> None:
        _write_head_db(
            self.data_dir / 'claude-mem.db',
            [{'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'}],
        )
        self._write_config()
        transport = RecordingTransport(
            [
                (201, {'import_id': 'imp-1', 'status': 'created'}),
                (200, {'accepted': True, 'seq': 0, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 1, 'created': 0, 'duplicates': 0, 'skipped': 1}),
                (200, {'accepted': True, 'seq': 2, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 3, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'status': 'succeeded', 'report': {'created': {'memories': 3}}}),
            ],
        )

        code, stdout, stderr = self._run(
            [
                'import',
                'claude-mem',
                '--data-dir',
                str(self.data_dir),
                '--config-dir',
                str(self.config_dir),
                '--apply',
            ],
            transport,
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual(6, len(transport.calls))
        create_payload = transport.calls[0]['payload']
        self.assertEqual(PROJECT_ID, create_payload['project_id'])
        self.assertEqual(17, create_payload['manifest']['schema_version_head'])
        self.assertIn('succeeded', stdout)

    def test_apply_skip_observations_omits_that_table(self) -> None:
        _write_head_db(
            self.data_dir / 'claude-mem.db',
            [{'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'}],
        )
        self._write_config()
        transport = RecordingTransport(
            [
                (201, {'import_id': 'imp-1', 'status': 'created'}),
                (200, {'accepted': True, 'seq': 0, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 1, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'accepted': True, 'seq': 2, 'created': 1, 'duplicates': 0, 'skipped': 0}),
                (200, {'status': 'succeeded', 'report': {}}),
            ],
        )

        code, _stdout, stderr = self._run(
            [
                'import',
                'claude-mem',
                '--data-dir',
                str(self.data_dir),
                '--config-dir',
                str(self.config_dir),
                '--apply',
                '--skip-observations',
            ],
            transport,
        )

        self.assertEqual(0, code, stderr)
        batch_tables = [
            call['payload']['table']
            for call in transport.calls
            if call['url'].endswith('/batches')
        ]
        self.assertEqual(['sdk_sessions', 'user_prompts', 'session_summaries'], batch_tables)

    def test_apply_multi_project_without_name_lists_and_exits(self) -> None:
        _write_head_db(
            self.data_dir / 'claude-mem.db',
            [
                {'content_session_id': 'c1', 'memory_session_id': 'm1', 'project': '/repo/one'},
                {'content_session_id': 'c2', 'memory_session_id': 'm2', 'project': '/repo/two'},
            ],
        )
        self._write_config()
        transport = RecordingTransport([])

        code, _stdout, stderr = self._run(
            [
                'import',
                'claude-mem',
                '--data-dir',
                str(self.data_dir),
                '--config-dir',
                str(self.config_dir),
                '--apply',
            ],
            transport,
        )

        self.assertEqual(1, code)
        self.assertEqual([], transport.calls)
        self.assertIn('/repo/one', stderr)
        self.assertIn('/repo/two', stderr)

    def test_missing_db_reports_error(self) -> None:
        transport = RecordingTransport([])

        code, _stdout, stderr = self._run(
            ['import', 'claude-mem', '--data-dir', str(self.data_dir)],
            transport,
        )

        self.assertEqual(1, code)
        self.assertIn('claude-mem', stderr)


if __name__ == '__main__':
    unittest.main()
